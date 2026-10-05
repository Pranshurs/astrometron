"""Reproduction and staleness.

``reproduce`` re-executes a stored run from its receipt: the config recorded in the
receipt, the dataset snapshot recorded in the store (verified against its revision),
and the currently importable provider/scorer/solver/tool code. Before running, every
controlled component is re-identified and compared with what the receipt recorded.

Verdicts
--------
REPRODUCED                 controlled inputs identical, provider pinned, every case
                           outcome and output digest identical.
NOT_EXACTLY_REPRODUCIBLE   the provider's model revision is not pinned, so exactness
                           cannot be claimed; agreement statistics are still reported.
INPUT_MISMATCH             a controlled component changed (scorer code, tool version,
                           prompt, provider identity...). Listed explicitly.
DIVERGED                   inputs identical and pinned, but outcomes differ: hidden
                           non-determinism or an undeclared dependency.
REFUSED                    the original receipt does not verify; nothing is reproduced
                           from invalid evidence.
VERIFIED_ONLY              ``execute=False``: inputs were checked, nothing was run.
NO_SCORED_CASES            every re-run matched, but the original scored no case
                           (e.g. all outages): there is no model behaviour to reproduce.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import digest, digest_bytes
from .config import from_dict
from .providers import build_provider
from .receipt import inputs_block
from .runner import _add_python_path, _solver, _tools, run_eval
from .scorers import build_scorer
from .solver import PromptTemplate
from .store import Store, StoreError

REPRODUCED = "REPRODUCED"
NOT_EXACT = "NOT_EXACTLY_REPRODUCIBLE"
INPUT_MISMATCH = "INPUT_MISMATCH"
DIVERGED = "DIVERGED"
REFUSED = "REFUSED"
VERIFIED_ONLY = "VERIFIED_ONLY"
NO_SCORED_CASES = "NO_SCORED_CASES"


def staleness(receipt: Mapping[str, Any]) -> list[str]:
    """Reasons the receipt no longer describes the current files on disk."""
    out: list[str] = []
    src = receipt.get("config_source") or {}
    path, recorded = src.get("path"), src.get("file_digest")
    if path and recorded:
        p = Path(path)
        if not p.exists():
            out.append(f"config file {path} no longer exists")
        elif digest_bytes(p.read_bytes()) != recorded:
            out.append(f"config file {path} changed after the receipt was generated")
    prov = receipt.get("dataset", {}).get("provenance") or {}
    dpath, ddig = prov.get("source"), prov.get("source_digest")
    if dpath and ddig:
        p = Path(dpath)
        if not p.exists():
            out.append(f"dataset source {dpath} no longer exists")
        elif digest_bytes(p.read_bytes()) != ddig:
            out.append(f"dataset source {dpath} changed after the receipt was generated")
    return out


@dataclass
class Reproduction:
    verdict: str
    original: str
    new_run: str | None = None
    mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    agreement: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "original": self.original,
            "new_run": self.new_run,
            "mismatches": self.mismatches,
            "notes": self.notes,
            "agreement": self.agreement,
        }


def _current_identities(receipt: Mapping[str, Any], base_dir: Path) -> dict[str, Any]:
    cfg = from_dict(receipt["config"], base_dir)
    _add_python_path(cfg)
    provider = build_provider(cfg.raw["provider"])
    scorer = build_scorer(cfg.raw["scorer"])
    prompt = cfg.raw.get("prompt") or {}
    if isinstance(prompt, str):
        prompt = {"template": prompt}
    tools = _tools(cfg)
    _, solver_ident = _solver(cfg)
    return {
        "provider": provider.identity().to_dict(),
        "scorer": scorer.identity(),
        "prompt": PromptTemplate(
            str(prompt.get("template", "{input}")), prompt.get("system")
        ).digest,
        "tools": [t.identity() for t in sorted(tools.values(), key=lambda t: t.name)],
        "solver": solver_ident,
    }


def check_inputs(receipt: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    mism: list[str] = []
    rp, cp = receipt["provider"]["identity"], current["provider"]
    for k in ("provider", "model", "revision", "endpoint"):
        if rp.get(k) != cp.get(k):
            mism.append(f"provider {k}: recorded {rp.get(k)!r}, now {cp.get(k)!r}")
    if digest(receipt["scorer"]) != digest(current["scorer"]):
        rs, cs = receipt["scorer"], current["scorer"]
        keys = sorted(k for k in set(rs) | set(cs) if rs.get(k) != cs.get(k))
        mism.append(f"scorer changed ({', '.join(keys)})")
    if receipt["prompt"]["digest"] != current["prompt"]:
        mism.append("prompt template changed")
    if digest(receipt["tools"]) != digest(current["tools"]):
        mism.append("tool identities/versions changed")
    if digest(receipt["solver"]) != digest(current["solver"]):
        mism.append("solver code changed")
    return mism


def reproduce(store: Store, run_id: str, *, execute: bool = True, repeats: int = 1) -> Reproduction:
    rep = Reproduction(verdict=REFUSED, original=run_id)
    check = store.verify_run(run_id)
    if not check.ok:
        rep.mismatches = check.problems
        return rep
    receipt = store.load_receipt(run_id)
    try:
        dataset = store.get_dataset(receipt["dataset"]["revision"])
    except StoreError as exc:
        rep.mismatches = [str(exc)]
        return rep
    rep.notes.extend(staleness(receipt))
    if rep.notes:
        rep.notes.append("reproducing from the stored dataset snapshot and recorded config")
    base_dir = Path(receipt["config_source"]["base_dir"])
    current = _current_identities(receipt, base_dir)
    rep.mismatches = check_inputs(receipt, current)
    pinned = bool(receipt["provider"]["identity"].get("pinned"))
    if not pinned:
        rep.notes.append(
            "EXACT REPRODUCTION NOT POSSIBLE: provider model revision is not externally pinned"
        )
    env = receipt["environment"]
    from .runner import environment

    now = environment()
    for k in ("python", "implementation", "machine"):
        if env.get(k) != now.get(k):
            rep.notes.append(f"environment {k}: recorded {env.get(k)}, now {now.get(k)}")
    if not execute:
        rep.verdict = INPUT_MISMATCH if rep.mismatches else VERIFIED_ONLY
        return rep
    cfg = from_dict(receipt["config"], base_dir)
    old_cases = {c["case_id"]: c for c in receipt["cases"]}
    scored_original = sum(c["outcome"] in ("PASS", "TASK_FAILURE") for c in receipt["cases"])
    differing: set[str] = set()
    new_runs: list[str] = []
    new: dict[str, Any] = {}
    for _ in range(max(1, repeats)):
        new = run_eval(cfg, store, dataset=dataset, reproduces=run_id)
        new_runs.append(new["run_id"])
        if new["inputs_digest"] != receipt["inputs_digest"]:
            a, b = inputs_block(receipt), inputs_block(new)
            msg = "inputs differ: " + ", ".join(
                sorted(k for k in a if digest(a[k]) != digest(b[k]))
            )
            if msg not in rep.mismatches:
                rep.mismatches.append(msg)
        served_old = sorted(receipt["provider"].get("served_models") or [])
        served_new = sorted(new["provider"].get("served_models") or [])
        if served_old != served_new:
            msg = f"provider served {served_new}, original run was served {served_old}"
            if msg not in rep.mismatches:
                rep.mismatches.append(msg)
        differing |= {
            c["case_id"]
            for c in new["cases"]
            if (c["outcome"], c.get("output_digest"))
            != (old_cases[c["case_id"]]["outcome"], old_cases[c["case_id"]].get("output_digest"))
        }
    rep.new_run = new_runs[-1]
    n = len(receipt["cases"])
    infra = {"INFRA_ERROR", "TIMEOUT", "CANCELLED"}
    rep.agreement = {
        "cases": n,
        "repeats": len(new_runs),
        "new_runs": new_runs,
        "scored_in_original": scored_original,
        "identical": n - len(differing),
        "differing": len(differing),
        "differing_cases": sorted(differing)[:50],
        "infra_only": bool(differing)
        and all(old_cases[cid]["outcome"] in infra for cid in differing),
        "outcomes_digest_equal": new["outcomes_digest"] == receipt["outcomes_digest"],
        "pass_rate": [
            receipt["aggregate"]["pass_rate"]["value"],
            new["aggregate"]["pass_rate"]["value"],
        ],
    }
    if rep.mismatches:
        rep.verdict = INPUT_MISMATCH
    elif not pinned:
        rep.verdict = NOT_EXACT
    elif differing:
        rep.verdict = DIVERGED
    elif scored_original == 0:
        # Identical outages reproduce nothing about the model.
        rep.verdict = NO_SCORED_CASES
        rep.notes.append("no case was scored in the original run: nothing to reproduce")
    else:
        rep.verdict = REPRODUCED
        if len(new_runs) == 1:
            rep.notes.append(
                "one re-run agreed on every case; use --repeats N for stronger evidence of "
                "determinism"
            )
    return rep
