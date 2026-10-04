"""The EvaluationReceipt: a versioned, self-verifying record of one run.

``receipt_digest`` is the digest of the receipt body with ``receipt_digest`` removed,
so any edit to any field (a case outcome, a count, the model name) is detectable.

:func:`verify_receipt` re-derives everything derivable instead of trusting stored
values: the digest, the accounting identity (every selected case exactly once), every
aggregate number, the ``inputs_digest`` and ``outcomes_digest``, and per-case outcome
consistency (a PASS needs a passing score; an error outcome needs an error and no score;
a scored outcome needs a successful final model call).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .canonical import digest
from .taxonomy import NOT_ATTEMPTED, ORDER, SCORED, Outcome

RECEIPT_SCHEMA = "evalledger.receipt/v1"
STATUS_COMPLETE = "COMPLETE"
STATUS_INCOMPLETE = "INCOMPLETE"


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0,100]); None for an empty sample."""
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q / 100.0 * len(s)))
    return float(s[k - 1])


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": _r(percentile(values, 50)),
        "p95": _r(percentile(values, 95)),
        "p99": _r(percentile(values, 99)),
        "max": _r(max(values) if values else None),
    }


def _r(v: float | None, nd: int = 3) -> float | None:
    return None if v is None else round(v, nd)


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6))


def aggregate(
    cases: Sequence[Mapping[str, Any]], selected: Sequence[str], excluded: Iterable[str]
) -> dict[str, Any]:
    """Compute the aggregate block purely from per-case records."""
    counts = Counter(c["outcome"] for c in cases)
    full = {o.value: counts.get(o.value, 0) for o in ORDER}
    requested = len(selected)
    n_excluded = len(set(excluded))
    passes = full[Outcome.PASS.value]
    scored = sum(full[o.value] for o in SCORED)
    attempted = requested - sum(full[o.value] for o in NOT_ATTEMPTED)
    denom = requested - n_excluded
    model_lat = [
        c["latency_ms"]["model"]
        for c in cases
        if c.get("latency_ms", {}).get("model") is not None
        and c["outcome"] not in {o.value for o in NOT_ATTEMPTED}
    ]
    total_lat = [
        c["latency_ms"]["total"]
        for c in cases
        if c.get("latency_ms", {}).get("total") is not None
        and c["outcome"] not in {o.value for o in NOT_ATTEMPTED}
    ]
    overhead = [
        c["latency_ms"]["overhead"]
        for c in cases
        if c.get("latency_ms", {}).get("overhead") is not None
        and c["outcome"] not in {o.value for o in NOT_ATTEMPTED}
    ]
    attempted_cases = [c for c in cases if c["outcome"] not in {o.value for o in NOT_ATTEMPTED}]
    known = [c["cost_usd"] for c in attempted_cases if c.get("cost_usd") is not None]
    unknown = sum(1 for c in attempted_cases if c.get("cost_usd") is None)
    total_cost = round(sum(known), 10) if known else (0.0 if not attempted_cases else None)
    labels: dict[str, dict[str, int]] = {}
    for c in cases:
        score = c.get("score") or {}
        if "label" in score:
            exp = c.get("expected_label")
            lab = labels.setdefault(score["label"], {"predicted": 0, "correct": 0})
            lab["predicted"] += 1
            if score.get("passed"):
                lab["correct"] += 1
            if exp is not None:
                labels.setdefault(exp, {"predicted": 0, "correct": 0})
    return {
        "requested": requested,
        "attempted": attempted,
        "scored": scored,
        "excluded": n_excluded,
        "counts": full,
        "accounted": sum(full.values()),
        "pass_rate": {
            "value": _r(passes / denom, 6) if denom else None,
            "numerator": passes,
            "denominator": denom,
            "denominator_basis": "requested"
            if not n_excluded
            else f"requested minus {n_excluded} explicitly excluded",
            "ci95_wilson": list(w) if (w := wilson(passes, denom)) else None,
        },
        "pass_rate_of_scored": {
            "value": _r(passes / scored, 6) if scored else None,
            "numerator": passes,
            "denominator": scored,
            "denominator_basis": "conditional: PASS / (PASS + TASK_FAILURE)",
        },
        "latency_ms": {
            "model": latency_summary(model_lat),
            "total": latency_summary(total_lat),
            "ledger_overhead": latency_summary(overhead),
        },
        "cost": {
            "total_usd_known": total_cost,
            "known_cases": len(known),
            "unknown_cases": unknown,
            "per_case_usd": _r(sum(known) / len(known), 10) if known else None,
            "complete": unknown == 0,
        },
        "labels": labels or None,
    }


def inputs_block(r: Mapping[str, Any]) -> dict[str, Any]:
    """The controlled inputs that determine outcomes (for a pinned provider)."""
    ident = r["provider"]["identity"]
    return {
        "dataset_revision": r["dataset"]["revision"],
        "selected_digest": r["dataset"]["selected_digest"],
        "excluded": r["dataset"]["excluded"],
        "provider": {k: ident.get(k) for k in ("provider", "model", "revision")},
        "params": r["provider"]["params"],
        "prompt_digest": r["prompt"]["digest"],
        "solver": r["solver"],
        "tools": r["tools"],
        "scorer": r["scorer"],
        "seed": r["run"]["seed"],
        "timeout_s": r["run"]["timeout_s"],
        "retries": r["run"]["retries"],
    }


def outcomes_block(cases: Sequence[Mapping[str, Any]]) -> list[list[Any]]:
    return [
        [c["case_id"], c["outcome"], c.get("output_digest"), (c.get("score") or {}).get("passed")]
        for c in sorted(cases, key=lambda c: c["case_id"])
    ]


def seal(body: dict[str, Any]) -> dict[str, Any]:
    """Compute derived digests and the receipt digest. Returns a new dict."""
    out = {k: v for k, v in body.items() if k != "receipt_digest"}
    out["inputs_digest"] = digest(inputs_block(out))
    out["outcomes_digest"] = digest(outcomes_block(out["cases"]))
    out["receipt_digest"] = digest({k: v for k, v in out.items() if k != "receipt_digest"})
    return out


@dataclass
class Verification:
    ok: bool
    problems: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)


_ERROR_OUTCOMES = (
    {o.value for o in Outcome}
    - {o.value for o in SCORED}
    - {Outcome.EXCLUDED.value, Outcome.CANCELLED.value}
)


def verify_receipt(r: Mapping[str, Any]) -> Verification:
    v = Verification(ok=True)
    if r.get("schema") != RECEIPT_SCHEMA:
        v.add(f"unsupported receipt schema {r.get('schema')!r}")
        return v
    body = {k: val for k, val in r.items() if k != "receipt_digest"}
    if digest(body) != r.get("receipt_digest"):
        v.add("receipt digest mismatch: receipt was modified after sealing")
    try:
        if digest(inputs_block(r)) != r.get("inputs_digest"):
            v.add("inputs_digest does not match recorded inputs")
    except (KeyError, TypeError) as exc:
        v.add(f"receipt missing input field: {exc}")
        return v
    cases = r.get("cases") or []
    if digest(outcomes_block(cases)) != r.get("outcomes_digest"):
        v.add("outcomes_digest does not match per-case outcomes")

    ds = r["dataset"]
    selected = list(ds.get("selected_case_ids") or [])
    if digest(selected) != ds.get("selected_digest"):
        v.add("selected_case_ids do not match selected_digest")
    ids = [c["case_id"] for c in cases]
    dupes = sorted(k for k, n in Counter(ids).items() if n > 1)
    if dupes:
        v.add(f"duplicate case records: {dupes[:5]}")
    missing = sorted(set(selected) - set(ids))
    extra = sorted(set(ids) - set(selected))
    if missing:
        v.add(
            f"requested != accounted: {len(missing)} selected case(s) have no record "
            f"(e.g. {missing[:3]})"
        )
    if extra:
        v.add(f"records for unselected case(s): {extra[:3]}")
    excluded_cfg = {e["case_id"]: e.get("reason") for e in ds.get("excluded") or []}
    for cid, reason in excluded_cfg.items():
        if not reason:
            v.add(f"exclusion of {cid} has no reason")
        if cid not in selected:
            v.add(f"excluded case {cid} was not in the selection")

    valid = {o.value for o in Outcome}
    for c in cases:
        cid, outcome = c.get("case_id"), c.get("outcome")
        if outcome not in valid:
            v.add(f"{cid}: unknown outcome {outcome!r}")
            continue
        score = c.get("score")
        if outcome == Outcome.EXCLUDED.value and cid not in excluded_cfg:
            v.add(f"{cid}: EXCLUDED without a configured exclusion")
        if cid in excluded_cfg and outcome != Outcome.EXCLUDED.value:
            v.add(f"{cid}: configured exclusion but outcome {outcome}")
        if outcome in (Outcome.PASS.value, Outcome.TASK_FAILURE.value):
            if not isinstance(score, Mapping) or "passed" not in score:
                v.add(f"{cid}: {outcome} without a score")
            elif bool(score["passed"]) != (outcome == Outcome.PASS.value):
                v.add(f"{cid}: outcome {outcome} contradicts score.passed={score['passed']}")
            if c.get("error"):
                v.add(f"{cid}: {outcome} but an error is recorded ({c['error'].get('stage')})")
            calls = [s for s in c.get("trajectory") or [] if s.get("type") == "model_call"]
            if calls and calls[-1].get("status") != "OK":
                v.add(
                    f"{cid}: {outcome} but the final model call failed "
                    "(provider failure counted as a model verdict)"
                )
        elif outcome in _ERROR_OUTCOMES:
            if not c.get("error"):
                v.add(f"{cid}: {outcome} without an error record")
            if score is not None and outcome != Outcome.JUDGE_ERROR.value:
                v.add(f"{cid}: {outcome} carries a score")

    stored = r.get("aggregate") or {}
    recomputed = aggregate(cases, selected, excluded_cfg)
    if stored != recomputed:
        diffs = sorted(
            k for k in set(stored) | set(recomputed) if stored.get(k) != recomputed.get(k)
        )
        v.add(f"aggregate does not match per-case records (fields: {diffs})")
    if recomputed["accounted"] != recomputed["requested"]:
        v.add(
            f"requested != accounted: requested {recomputed['requested']}, "
            f"accounted {recomputed['accounted']}"
        )
    if r.get("status") == STATUS_COMPLETE and not r.get("timing", {}).get("finished_at"):
        v.add("COMPLETE receipt without finished_at")
    return v
