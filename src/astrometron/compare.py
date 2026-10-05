"""Run comparison and comparability rules.

Comparability is decided deterministically from the two receipts. *Measurement* factors
decide the label; *treatment* factors are what you are trying to compare and are listed
as confounders when more than one changed.

==========================  ===============================================================
Label                       Rule
==========================  ===============================================================
STRONGLY_COMPARABLE         same dataset revision, same selected cases, same scorer
                            identity (incl. code digest), same judge (or both none)
PARTIALLY_COMPARABLE        same dataset id, and at least one of: different revision,
                            different selection, different scorer, different judge.
                            Paired analysis uses only cases whose row digest is identical.
NOT_DIRECTLY_COMPARABLE     different dataset id, no shared unchanged cases, or either
                            receipt fails verification
==========================  ===============================================================
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .canonical import digest
from .receipt import verify_receipt
from .taxonomy import SCORED

_SCORED = {o.value for o in SCORED}

STRONG = "STRONGLY_COMPARABLE"
PARTIAL = "PARTIALLY_COMPARABLE"
NOT = "NOT_DIRECTLY_COMPARABLE"

_TREATMENT: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "model": lambda r: {
        k: r["provider"]["identity"].get(k) for k in ("provider", "model", "revision", "endpoint")
    },
    "request params": lambda r: r["provider"]["params"],
    "prompt": lambda r: r["prompt"]["digest"],
    "solver": lambda r: r["solver"],
    "tools": lambda r: r["tools"],
    "seed": lambda r: r["run"]["seed"],
    "retries/timeout": lambda r: [r["run"]["retries"], r["run"]["timeout_s"]],
    "code revision": lambda r: r["code"]["git_commit"],
    "astrometron version": lambda r: r["code"]["astrometron_version"],
    "python": lambda r: r["environment"]["python"],
}


@dataclass
class Comparability:
    label: str
    reasons: list[str] = field(default_factory=list)
    changed_treatments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def comparability(a: Mapping[str, Any], b: Mapping[str, Any]) -> Comparability:
    reasons: list[str] = []
    warnings: list[str] = []
    for name, r in (("A", a), ("B", b)):
        v = verify_receipt(r)
        if not v.ok:
            return Comparability(NOT, [f"receipt {name} fails verification: {v.problems[0]}"])
        if r.get("status") != "COMPLETE":
            warnings.append(f"run {name} is {r.get('status')}: interrupted run")
            reasons.append(f"run {name} is {r.get('status')} (interrupted)")
    da, db = a["dataset"], b["dataset"]
    if da["id"] != db["id"]:
        return Comparability(NOT, [f"different datasets: {da['id']} vs {db['id']}"])
    label = PARTIAL if reasons else STRONG
    if da["revision"] != db["revision"]:
        label = PARTIAL
        reasons.append("dataset revision changed")
    if da["selected_digest"] != db["selected_digest"]:
        label = PARTIAL
        reasons.append("different case selection")
    if digest(a["scorer"]) != digest(b["scorer"]):
        label = PARTIAL
        sa, sb = a["scorer"], b["scorer"]
        what = (
            "scorer changed"
            if sa.get("name") != sb.get("name")
            else ("scorer version/params/code changed")
        )
        reasons.append(what)
    if digest(a.get("judge")) != digest(b.get("judge")):
        label = PARTIAL
        reasons.append("judge model changed")
    if da["excluded"] != db["excluded"]:
        label = PARTIAL
        reasons.append("explicit exclusions differ")
    changed = [n for n, f in _TREATMENT.items() if digest(f(a)) != digest(f(b))]
    substantive = [
        c for c in changed if c not in ("code revision", "astrometron version", "python")
    ]
    if len(substantive) > 1:
        warnings.append(
            f"more than one treatment changed ({', '.join(substantive)}): "
            "this is not a clean single-factor comparison"
        )
    if "judge model changed" in reasons:
        warnings.append("judge model also changed: not a clean model-only comparison")
    if "scorer changed" in reasons or "scorer version/params/code changed" in reasons:
        warnings.append("scorer changed: score differences may be measurement, not model")
    return Comparability(label, reasons, changed, warnings)


def _paired_ci(improved: int, regressed: int, n: int) -> tuple[float, float] | None:
    if n == 0:
        return None
    d = (improved - regressed) / n
    var = ((improved + regressed) - (improved - regressed) ** 2 / n) / (n * n)
    half = 1.959963984540054 * math.sqrt(max(var, 0.0))
    return (round(d - half, 6), round(d + half, 6))


def _mcnemar_exact(b: int, c: int) -> float | None:
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    tail: float = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return round(min(1.0, 2 * tail), 6)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: obj}


def compare(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    comp = comparability(a, b)
    out: dict[str, Any] = {
        "run_a": a.get("run_id"),
        "run_b": b.get("run_id"),
        "comparability": comp.label,
        "reasons": comp.reasons,
        "changed_treatments": comp.changed_treatments,
        "warnings": comp.warnings,
    }
    if comp.label == NOT and comp.reasons and "fails verification" in comp.reasons[0]:
        return out
    pa, pb = a["aggregate"]["pass_rate"], b["aggregate"]["pass_rate"]
    out["pass_rate"] = {
        "a": pa["value"],
        "b": pb["value"],
        "delta_pp": None
        if pa["value"] is None or pb["value"] is None
        else round((pb["value"] - pa["value"]) * 100, 3),
        "denominators": [pa["denominator"], pb["denominator"]],
    }
    ca = {c["case_id"]: c for c in a["cases"]}
    cb = {c["case_id"]: c for c in b["cases"]}
    shared = sorted(set(ca) & set(cb))
    same_rows = [cid for cid in shared if ca[cid]["row_digest"] == cb[cid]["row_digest"]]
    changed_rows = sorted(set(shared) - set(same_rows))
    excluded = {e["case_id"] for e in a["dataset"]["excluded"]} | {
        e["case_id"] for e in b["dataset"]["excluded"]
    }
    paired = [cid for cid in same_rows if cid not in excluded]
    if not paired:
        out["comparability"] = NOT
        out["reasons"] = [*comp.reasons, "no shared, unchanged, non-excluded cases"]
        return out
    # Statistics compare model verdicts only: a case enters the paired test when both
    # runs scored it. An outage or a cancelled case is not a regression of the model.
    transitions: Counter[str] = Counter()
    new_infra: list[str] = []
    infra = {"INFRA_ERROR", "MODEL_ERROR", "TIMEOUT"}
    both_scored: list[str] = []
    not_scored: Counter[str] = Counter()
    for cid in paired:
        oa, ob = ca[cid]["outcome"], cb[cid]["outcome"]
        if oa != ob:
            transitions[f"{oa} -> {ob}"] += 1
        if ob in infra and oa not in infra:
            new_infra.append(cid)
        if oa in _SCORED and ob in _SCORED:
            both_scored.append(cid)
        else:
            not_scored["A" if oa not in _SCORED else "B" if ob not in _SCORED else "?"] += 1
            if oa not in _SCORED and ob not in _SCORED:
                not_scored["both"] += 1
    improved = [c for c in both_scored if ca[c]["outcome"] != "PASS" == cb[c]["outcome"]]
    regressed = [c for c in both_scored if ca[c]["outcome"] == "PASS" != cb[c]["outcome"]]
    same_pass = sum(ca[c]["outcome"] == cb[c]["outcome"] == "PASS" for c in both_scored)
    same_fail = len(both_scored) - len(improved) - len(regressed) - same_pass
    n = len(both_scored)
    pa_paired = sum(ca[c]["outcome"] == "PASS" for c in both_scored) / n if n else None
    pb_paired = sum(cb[c]["outcome"] == "PASS" for c in both_scored) / n if n else None
    ci = _paired_ci(len(improved), len(regressed), n)
    out["paired"] = {
        "basis": "cases present in both runs, unchanged rows, not excluded, scored in both",
        "cases": n,
        "shared_unchanged": len(paired),
        "not_scored_in_both": len(paired) - n,
        "only_in_a": len(set(ca) - set(cb)),
        "only_in_b": len(set(cb) - set(ca)),
        "row_changed": len(changed_rows),
        "pass_rate_a": None if pa_paired is None else round(pa_paired, 6),
        "pass_rate_b": None if pb_paired is None else round(pb_paired, 6),
        "delta_pp": None
        if pa_paired is None or pb_paired is None
        else round((pb_paired - pa_paired) * 100, 3),
        "delta_ci95_pp": [round(x * 100, 3) for x in ci] if ci else None,
        "mcnemar_p": _mcnemar_exact(len(improved), len(regressed)),
        "improved": len(improved),
        "regressed": len(regressed),
        "unchanged_pass": same_pass,
        "unchanged_fail": same_fail,
        "improved_cases": improved[:50],
        "regressed_cases": regressed[:50],
        "new_infra_failures": len(new_infra),
        "transitions": dict(sorted(transitions.items())),
    }
    if n == 0:
        out["warnings"].append("no case was scored in both runs: no paired statistics")
    la, lb = a["aggregate"]["latency_ms"]["model"], b["aggregate"]["latency_ms"]["model"]
    out["latency_ms_model"] = {q: {"a": la.get(q), "b": lb.get(q)} for q in ("p50", "p95", "p99")}
    ka, kb = a["aggregate"]["cost"], b["aggregate"]["cost"]
    out["cost_per_case_usd"] = {
        "a": ka["per_case_usd"] if ka["complete"] else None,
        "b": kb["per_case_usd"] if kb["complete"] else None,
        "note": None
        if ka["complete"] and kb["complete"]
        else "cost UNKNOWN for at least one run (incomplete cost metadata)",
    }
    fa, fb = _flatten(a["config"]), _flatten(b["config"])
    out["config_changes"] = {
        k: [fa.get(k), fb.get(k)] for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)
    }
    return out


def compare_runs(store: Any, run_a: str, run_b: str) -> dict[str, Any]:
    """Compare two stored runs. Each must pass full store verification (receipt digest,
    index binding, dataset and selection binding); a receipt that is only self-consistent
    is not evidence."""
    for name, rid in (("A", run_a), ("B", run_b)):
        v = store.verify_run(rid)
        if not v.ok:
            return {
                "run_a": run_a,
                "run_b": run_b,
                "comparability": NOT,
                "reasons": [f"run {name} ({rid}) fails store verification: {v.problems[0]}"],
                "changed_treatments": [],
                "warnings": [],
            }
    return compare(store.load_receipt(run_a), store.load_receipt(run_b))
