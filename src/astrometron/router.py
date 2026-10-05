"""Evidence-backed model routing.

The router never asks a model which model is best. It selects among *candidate
providers* using only stored, verified evaluation receipts, and every decision cites
the receipts that justified it.

Pipeline for each candidate (first failing gate decides its status):

1. identity    - build the candidate from config and compute its *current* identity
2. evidence    - latest COMPLETE run for (task, provider/model) on the routing basis,
                 that passes full store verification (tampered/crashed runs are skipped)
3. comparable  - evidence must share the basis: dataset revision + scorer + judge
4. fresh       - evidence age <= max_evidence_age_days; model revision unchanged
5. sample      - requested cases >= min_cases
6. available   - not marked unavailable; optional live probe succeeds
7. quality     - success bound >= min_success_rate (default bound: Wilson 95% lower)
8. reliable    - (INFRA+MODEL+TIMEOUT errors) / attempted <= max_error_rate
9. latency     - model p95 <= max_p95_ms
10. cost       - known per-case cost <= max_cost_per_case_usd; UNKNOWN cost fails
                 closed whenever a cost limit is set

Decision: ``SELECTED`` | ``NO_ELIGIBLE_MODEL`` | ``ROUTING_EVIDENCE_STALE`` (nothing is
eligible and at least one candidate failed *only* on freshness).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .canonical import digest
from .config import ConfigError, find_secrets
from .providers import Message, ProviderError, Request, build_provider
from .runner import new_run_id, utcnow
from .store import Store

POLICY_KEYS = {
    "task",
    "dataset",
    "dataset_revision",
    "requirements",
    "objective",
    "candidates",
    "python_path",
    "description",
}
REQ_KEYS = {
    "min_success_rate",
    "quality_bound",
    "max_p95_ms",
    "max_cost_per_case_usd",
    "min_cases",
    "max_evidence_age_days",
    "max_error_rate",
    "require_pinned",
    "max_excluded_fraction",
    "max_evaluation_error_rate",
    "max_degraded_fraction",
}
#: Evidence is never authoritative forever: applied when the policy does not set an age.
#: Set ``max_evidence_age_days: null`` explicitly to disable.
DEFAULT_MAX_AGE_DAYS = 30
OBJECTIVES = ("cheapest", "best_quality", "fastest")

ELIGIBLE = "ELIGIBLE"
STALE_STATUSES = {"STALE_EVIDENCE", "MODEL_REVISION_CHANGED"}


def load_policy(path: str | Path) -> dict[str, Any]:
    p = Path(path).resolve()
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return validate_policy(raw, p.parent)


def validate_policy(raw: Any, base_dir: Path | str = ".") -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError("routing policy must be a mapping")
    raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    unknown = set(raw) - POLICY_KEYS
    if unknown:
        raise ConfigError(f"unknown policy key(s): {sorted(unknown)}")
    if find_secrets(raw):
        raise ConfigError("literal credentials are not allowed in a routing policy")
    req = dict(raw.get("requirements") or {})
    bad = set(req) - REQ_KEYS
    if bad:
        raise ConfigError(f"unknown requirement(s): {sorted(bad)}")
    if req.get("quality_bound", "wilson_lower") not in ("wilson_lower", "point"):
        raise ConfigError("quality_bound must be wilson_lower or point")
    if raw.get("objective", "cheapest") not in OBJECTIVES:
        raise ConfigError(f"objective must be one of {OBJECTIVES}")
    cands = raw.get("candidates")
    if not cands or not isinstance(cands, list):
        raise ConfigError("policy needs a non-empty candidates list")
    if not raw.get("task"):
        raise ConfigError("policy needs a task")
    return {**dict(raw), "requirements": req, "_base_dir": str(Path(base_dir).resolve())}


def _age_days(ts: str, now: datetime) -> float:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (now - t).total_seconds() / 86400.0


def _basis(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": r["dataset"]["id"],
        "dataset_revision": r["dataset"]["revision"],
        "selected_digest": r["dataset"]["selected_digest"],
        "scorer": digest(r["scorer"]),
        "judge": digest(r.get("judge")),
        "excluded": digest(r["dataset"]["excluded"]),
    }


def _evidence_summary(r: Mapping[str, Any], age: float) -> dict[str, Any]:
    agg = r["aggregate"]
    c = agg["counts"]
    errors = c["INFRA_ERROR"] + c["MODEL_ERROR"] + c["TIMEOUT"]
    return {
        "run_id": r["run_id"],
        "receipt_digest": r["receipt_digest"],
        "finished_at": r["timing"]["finished_at"],
        "age_days": round(age, 3),
        "cases": agg["pass_rate"]["denominator"],
        "requested": agg["requested"],
        "excluded": agg["excluded"],
        "prompt_digest": r["prompt"]["digest"],
        "params": r["provider"]["params"],
        "endpoint": r["provider"]["identity"].get("endpoint"),
        "success_rate": agg["pass_rate"]["value"],
        "success_ci95": agg["pass_rate"]["ci95_wilson"],
        "error_rate": round(errors / agg["attempted"], 6) if agg["attempted"] else None,
        # Evaluation-side failures: the measurement itself was broken for these cases.
        "evaluation_error_rate": round(
            (c["HARNESS_ERROR"] + c["JUDGE_ERROR"] + c["INVALID_CASE"]) / agg["requested"], 6
        )
        if agg["requested"]
        else None,
        "scored_after_tool_failure": agg.get("scored_after_tool_failure", 0),
        "p95_ms": agg["latency_ms"]["model"]["p95"],
        "cost_per_case_usd": agg["cost"]["per_case_usd"] if agg["cost"]["complete"] else None,
        "revision": r["provider"]["identity"].get("revision"),
        "dataset_revision": r["dataset"]["revision"],
    }


def route(
    store: Store,
    policy: Mapping[str, Any],
    *,
    task: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    import sys

    now = now or datetime.now(timezone.utc)
    task = task or str(policy["task"])
    req = policy["requirements"]
    base_dir = Path(policy.get("_base_dir", "."))
    for p in policy.get("python_path") or []:
        full = str((base_dir / p).resolve())
        if full not in sys.path:
            sys.path.insert(0, full)

    # 1) all trustworthy evidence for the task
    skipped: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for row in reversed(store.list_runs(task=task)):  # newest first
        if row["status"] != "COMPLETE":
            continue
        v = store.verify_run(row["run_id"])
        if not v.ok:
            skipped.append({"run_id": row["run_id"], "reason": v.problems[0]})
            continue
        r = store.load_receipt(row["run_id"])
        # (the index task column is bound to the receipt by verify_run above)
        if policy.get("dataset") and r["dataset"]["id"] != policy["dataset"]:
            continue
        evidence.append(r)

    # 2) the routing basis: pinned dataset revision, else the newest evidence's basis
    basis: dict[str, Any] | None = None
    if evidence:
        pool = evidence
        if policy.get("dataset_revision"):
            pool = [r for r in evidence if r["dataset"]["revision"] == policy["dataset_revision"]]
        basis = _basis(pool[0]) if pool else None

    candidates: list[dict[str, Any]] = []
    for spec in policy["candidates"]:
        spec = dict(spec)
        unavailable = bool(spec.pop("unavailable", False))
        probe = bool(spec.pop("probe", False))
        entry: dict[str, Any] = {"reasons": []}
        try:
            provider = build_provider(spec)
            ident = provider.identity()
        except Exception as exc:
            entry.update(
                candidate=str(spec.get("model")),
                status="CONFIG_ERROR",
                reasons=[f"cannot build candidate: {exc}"],
            )
            candidates.append(entry)
            continue
        entry["candidate"] = ident.key
        entry["current_revision"] = ident.revision
        same_key = [
            r
            for r in evidence
            if f"{r['provider']['identity']['provider']}/{r['provider']['identity']['model']}"
            == ident.key
        ]
        # Evidence from another deployment (different endpoint) says nothing about this one.
        mine = [r for r in same_key if r["provider"]["identity"].get("endpoint") == ident.endpoint]
        if same_key and not mine:
            entry.update(
                status="DIFFERENT_DEPLOYMENT",
                reasons=[
                    "evidence exists only for endpoint "
                    f"{same_key[0]['provider']['identity'].get('endpoint')!r}, "
                    f"candidate is served from {ident.endpoint!r}"
                ],
            )
            candidates.append(entry)
            continue
        if not mine:
            entry.update(
                status="NO_EVIDENCE", reasons=[f"no verified COMPLETE evaluation for task {task!r}"]
            )
            candidates.append(entry)
            continue
        on_basis = [r for r in mine if basis is not None and _basis(r) == basis]
        if not on_basis:
            entry.update(
                status="INCOMPARABLE_EVIDENCE",
                reasons=[
                    "evidence exists only on a different dataset revision / selection / "
                    "scorer / judge than the routing basis"
                ],
                evidence=_evidence_summary(
                    mine[0], _age_days(mine[0]["timing"]["finished_at"], now)
                ),
            )
            candidates.append(entry)
            continue
        r = on_basis[0]
        age = _age_days(r["timing"]["finished_at"], now)
        ev = _evidence_summary(r, age)
        entry["evidence"] = ev
        failures: list[tuple[str, str]] = []
        max_age = req.get("max_evidence_age_days", DEFAULT_MAX_AGE_DAYS)
        if max_age is not None and age > float(max_age):
            failures.append(("STALE_EVIDENCE", f"evidence is {age:.1f} days old (max {max_age})"))
        if ev["revision"] != ident.revision:
            failures.append(
                (
                    "MODEL_REVISION_CHANGED",
                    f"evaluated revision {ev['revision']} != current {ident.revision}",
                )
            )
        if req.get("require_pinned") and not ident.pinned:
            failures.append(("UNPINNED_MODEL", "candidate model revision is not pinned"))
        max_excl = float(req.get("max_excluded_fraction", 0.0))
        if ev["requested"] and ev["excluded"] / ev["requested"] > max_excl:
            failures.append(
                (
                    "EXCESSIVE_EXCLUSIONS",
                    f"{ev['excluded']}/{ev['requested']} cases explicitly excluded "
                    f"(max_excluded_fraction {max_excl})",
                )
            )
        min_cases = int(req.get("min_cases", 0))
        if ev["cases"] < min_cases:
            failures.append(("INSUFFICIENT_SAMPLE", f"{ev['cases']} cases < min_cases {min_cases}"))
        if unavailable:
            failures.append(("UNAVAILABLE", "candidate marked unavailable"))
        elif probe:
            try:
                provider.complete(
                    Request(messages=(Message("user", "ping"),), params={"max_tokens": 1})
                )
            except ProviderError as exc:
                failures.append(("UNAVAILABLE", f"probe failed: {exc}"))
        bound_kind = req.get("quality_bound", "wilson_lower")
        bound = (
            (ev["success_ci95"][0] if ev["success_ci95"] else None)
            if bound_kind == "wilson_lower"
            else ev["success_rate"]
        )
        entry["quality_bound"] = {"kind": bound_kind, "value": bound}
        min_q = req.get("min_success_rate")
        if min_q is not None and (bound is None or bound < float(min_q)):
            failures.append(("BELOW_QUALITY_FLOOR", f"success {bound_kind} {bound} < {min_q}"))
        max_err = req.get("max_error_rate")
        if max_err is not None and (ev["error_rate"] is None or ev["error_rate"] > float(max_err)):
            failures.append(("UNRELIABLE", f"error rate {ev['error_rate']} > {max_err}"))
        max_eval_err = float(req.get("max_evaluation_error_rate", 0.0))
        if ev["evaluation_error_rate"] is None or ev["evaluation_error_rate"] > max_eval_err:
            failures.append(
                (
                    "BROKEN_EVALUATION",
                    f"harness/judge/invalid-case error rate {ev['evaluation_error_rate']} > "
                    f"{max_eval_err}: the evidence itself is unreliable",
                )
            )
        max_degraded = req.get("max_degraded_fraction")
        if max_degraded is not None and ev["cases"]:
            frac = ev["scored_after_tool_failure"] / ev["cases"]
            if frac > float(max_degraded):
                failures.append(
                    (
                        "DEGRADED_EVIDENCE",
                        f"{ev['scored_after_tool_failure']} case(s) scored after a tool failure "
                        f"({frac:.3f} > {max_degraded})",
                    )
                )
        max_p95 = req.get("max_p95_ms")
        if max_p95 is not None and (ev["p95_ms"] is None or ev["p95_ms"] > float(max_p95)):
            failures.append(("TOO_SLOW", f"p95 {ev['p95_ms']} ms > {max_p95} ms"))
        max_cost = req.get("max_cost_per_case_usd")
        if max_cost is not None:
            if ev["cost_per_case_usd"] is None:
                failures.append(
                    ("COST_UNKNOWN", "cost metadata missing; cannot prove the cost limit is met")
                )
            elif ev["cost_per_case_usd"] > float(max_cost):
                failures.append(("TOO_EXPENSIVE", f"${ev['cost_per_case_usd']}/case > ${max_cost}"))
        entry["status"] = failures[0][0] if failures else ELIGIBLE
        entry["reasons"] = [f"{code}: {msg}" for code, msg in failures]
        entry["failed_gates"] = [code for code, _ in failures]
        candidates.append(entry)

    eligible = [c for c in candidates if c["status"] == ELIGIBLE]
    objective = policy.get("objective", "cheapest")

    def rank(c: dict[str, Any]) -> tuple[Any, ...]:
        ev = c["evidence"]
        cost = ev["cost_per_case_usd"]
        q = -(c["quality_bound"]["value"] or 0.0)
        lat = ev["p95_ms"] if ev["p95_ms"] is not None else float("inf")
        cost_key = cost if cost is not None else float("inf")
        if objective == "best_quality":
            return (q, cost_key, lat, c["candidate"])
        if objective == "fastest":
            return (lat, q, cost_key, c["candidate"])
        return (cost_key, q, lat, c["candidate"])

    eligible.sort(key=rank)
    if eligible:
        status, selected = "SELECTED", eligible[0]["candidate"]
    else:
        selected = None
        only_stale = [
            c
            for c in candidates
            if c.get("failed_gates") and set(c["failed_gates"]) <= STALE_STATUSES
        ]
        status = "ROUTING_EVIDENCE_STALE" if only_stale else "NO_ELIGIBLE_MODEL"
    decision: dict[str, Any] = {
        "schema": "astrometron.routing-decision/v1",
        "decision_id": new_run_id(),
        "created_at": utcnow(),
        "task": task,
        "status": status,
        "selected": selected,
        "objective": objective,
        "requirements": req,
        "basis": basis,
        "candidates": candidates,
        "skipped_invalid_evidence": skipped,
        "policy_digest": digest({k: v for k, v in policy.items() if not k.startswith("_")}),
    }
    decision["decision_digest"] = store.record_decision(decision)
    return decision


def render_decision(d: Mapping[str, Any]) -> str:
    lines = [f"ROUTE task: {d['task']}   objective: {d['objective']}", ""]
    req = d["requirements"]
    if req:
        lines.append("requirements:")
        lines += [f"  {k}: {v}" for k, v in req.items()]
        lines.append("")
    if d["status"] == "SELECTED":
        sel = next(c for c in d["candidates"] if c["candidate"] == d["selected"])
        ev = sel["evidence"]
        cost = ev["cost_per_case_usd"]
        lines += [
            f"selected: {d['selected']}",
            "",
            "evidence:",
            f"  run {ev['run_id']}  ({ev['age_days']:.1f} days old)",
            f"  receipt {ev['receipt_digest']}",
            f"  success: {ev['success_rate'] * 100:.1f}%  "
            f"(95% CI {ev['success_ci95'][0] * 100:.1f}-{ev['success_ci95'][1] * 100:.1f}%, "
            f"{sel['quality_bound']['kind']} used)",
            f"  p95: {ev['p95_ms']} ms",
            f"  cost: {'UNKNOWN' if cost is None else f'${cost:.6f}/case'}",
            f"  cases: {ev['cases']}",
        ]
    elif d["status"] == "ROUTING_EVIDENCE_STALE":
        lines.append("ROUTING EVIDENCE STALE - re-evaluate before routing")
    else:
        lines.append("NO ELIGIBLE MODEL")
    lines += ["", "candidates:"]
    for c in d["candidates"]:
        mark = "*" if c["candidate"] == d.get("selected") else " "
        lines.append(f" {mark} {c['candidate']:<34}{c['status']}")
        ev = c.get("evidence")
        if ev:
            cost = ev["cost_per_case_usd"]
            sr = ev["success_rate"]
            lines.append(
                f"      success {'n/a' if sr is None else f'{sr * 100:.1f}%'}  "
                f"p95 {ev['p95_ms']} ms  "
                f"cost {'UNKNOWN' if cost is None else f'${cost:.6f}'}  "
                f"cases {ev['cases']}  run {ev['run_id']}"
            )
        for r in c["reasons"]:
            lines.append(f"      - {r}")
    for s in d.get("skipped_invalid_evidence") or []:
        lines.append(f"  skipped invalid evidence {s['run_id']}: {s['reason']}")
    lines += ["", f"decision {d['decision_id']}  digest {d['decision_digest']}"]
    return "\n".join(lines)
