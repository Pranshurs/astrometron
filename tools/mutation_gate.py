"""Mutation gate for EvalLedger's critical invariants.

Each mutant breaks one invariant on purpose. The test suite must fail (kill it).
Rules of the gate:

* the unmutated baseline must pass first - a failing baseline makes every kill
  meaningless;
* the mutated tree must be the one imported (checked via ``evalledger.__file__``);
* a mutant whose FIND text is not present exactly once is INVALID, which fails the gate
  (the rule lost its attacker), never silently skipped;
* bytecode caching is disabled so a restored file cannot be shadowed by a stale .pyc;
* any SURVIVED mutant fails the gate unless listed in EQUIVALENT with a justification.

Usage: python tools/mutation_gate.py [--only M01,M02] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".mutation" / "work"


@dataclass(frozen=True)
class Mutant:
    id: str
    invariant: str
    file: str
    find: str
    replace: str


S = "src/evalledger/"
MUTANTS: list[Mutant] = [
    # -- denominator / accounting -------------------------------------------------
    Mutant(
        "M01",
        "pass rate denominator excludes failed cases",
        S + "receipt.py",
        "    denom = requested - n_excluded\n",
        "    denom = scored - n_excluded\n",
    ),
    Mutant(
        "M02",
        "runner drops error rows before aggregation",
        S + "runner.py",
        "    case_records = [records[cid] for cid in selected]\n",
        "    case_records = [records[cid] for cid in selected\n"
        "                    if records[cid]['outcome'] in ('PASS', 'TASK_FAILURE')]\n",
    ),
    Mutant(
        "M03",
        "verifier ignores selected cases with no record",
        S + "receipt.py",
        "    if missing:\n",
        "    if False:\n",
    ),
    Mutant(
        "M04",
        "verifier ignores requested != accounted in aggregate",
        S + "receipt.py",
        '    if recomputed["accounted"] != recomputed["requested"]:\n',
        "    if False:\n",
    ),
    Mutant(
        "M05",
        "verifier ignores duplicate case records",
        S + "receipt.py",
        "    if dupes:\n",
        "    if False:\n",
    ),
    Mutant(
        "M06",
        "verifier trusts stored aggregate",
        S + "receipt.py",
        "    if stored != recomputed:\n",
        "    if False:\n",
    ),
    Mutant(
        "M07",
        "exclusion accepted without a reason",
        S + "config.py",
        ' or not ex.get("reason"):',
        ":",
    ),
    # -- dataset provenance ---------------------------------------------------------
    Mutant(
        "M08",
        "pinned dataset revision not enforced",
        S + "runner.py",
        "    if pinned and pinned != ds.revision:\n",
        "    if False:\n",
    ),
    Mutant(
        "M09",
        "snapshot accepted with wrong revision",
        S + "dataset.py",
        '        if ds.revision != snap.get("revision"):\n',
        "        if False:\n",
    ),
    Mutant(
        "M10",
        "corrupted store object accepted",
        S + "store.py",
        "        if digest_bytes(data) != d:\n",
        "        if False:\n",
    ),
    Mutant(
        "M11",
        "dataset revision depends on row order",
        S + "dataset.py",
        "        self._cases: dict[str, Case] = dict(sorted(by_id.items()))\n",
        "        self._cases: dict[str, Case] = dict(by_id.items())\n",
    ),
    Mutant(
        "M12",
        "row digest ignores expected output",
        S + "dataset.py",
        '            "expected": self.expected,\n',
        "",
    ),
    Mutant(
        "M13",
        "receipt case row digest not bound to snapshot",
        S + "store.py",
        '                elif ds.row_digest(cid) != c.get("row_digest"):\n',
        "                elif False:\n",
    ),
    # -- selection binding --------------------------------------------------------
    Mutant(
        "M14",
        "selected_case_ids not bound to selected_digest",
        S + "receipt.py",
        '    if digest(selected) != ds.get("selected_digest"):\n',
        "    if False:\n",
    ),
    Mutant(
        "M15",
        "selection not re-derived from snapshot",
        S + "store.py",
        "            if v_sel:\n",
        "            if False:\n",
    ),
    # -- receipt integrity ----------------------------------------------------------
    Mutant(
        "M16",
        "receipt digest not checked",
        S + "receipt.py",
        '    if digest(body) != r.get("receipt_digest"):\n',
        "    if False:\n",
    ),
    Mutant(
        "M17",
        "index digest binding not checked",
        S + "store.py",
        '        if row["receipt_digest"] != receipt.get("receipt_digest"):\n',
        "        if False:\n",
    ),
    Mutant(
        "M18",
        "crashed (RUNNING) run accepted",
        S + "store.py",
        '        if row["status"] == "RUNNING":\n',
        "        if False:\n",
    ),
    Mutant(
        "M19",
        "inputs digest ignores scorer",
        S + "receipt.py",
        '        "scorer": r["scorer"],\n',
        "",
    ),
    # -- failure taxonomy ---------------------------------------------------------
    Mutant(
        "M20",
        "harness crash becomes TASK_FAILURE",
        S + "runner.py",
        "                Outcome.HARNESS_ERROR,\n"
        '                {"stage": "solver", "message": f"{type(exc).__name__}: {exc}"[:500]},\n',
        "                Outcome.TASK_FAILURE,\n"
        '                {"stage": "solver", "message": f"{type(exc).__name__}: {exc}"[:500]},\n',
    ),
    Mutant(
        "M21",
        "judge failure becomes TASK_FAILURE",
        S + "runner.py",
        "                        Outcome.JUDGE_ERROR,\n",
        "                        Outcome.TASK_FAILURE,\n",
    ),
    Mutant(
        "M22",
        "provider infra failure treated as PASS",
        S + "taxonomy.py",
        "    if kind is ErrorKind.INFRA:\n        return Outcome.INFRA_ERROR\n",
        "    if kind is ErrorKind.INFRA:\n        return Outcome.PASS\n",
    ),
    Mutant(
        "M23",
        "provider failure swallowed by solver is not attributed",
        S + "runner.py",
        "            if unrecovered is not None:\n",
        "            if False:\n",
    ),
    Mutant(
        "M24",
        "verifier accepts verdict after failed final model call",
        S + "receipt.py",
        '            if calls and calls[-1].get("status") != "OK":\n',
        "            if False:\n",
    ),
    Mutant(
        "M25",
        "verifier accepts error outcomes without error record",
        S + "receipt.py",
        '            if not c.get("error"):\n',
        "            if False:\n",
    ),
    Mutant(
        "M26",
        "verifier accepts judge verdict without parsed judge evidence",
        S + "receipt.py",
        '                or (score.get("evidence") or {}).get("parse_status") != "OK"\n',
        "                and False\n",
    ),
    Mutant(
        "M27",
        "judge accepts more than one verdict line",
        S + "judge.py",
        "    if len(hits) != 1:\n",
        "    if len(hits) < 1:\n",
    ),
    Mutant(
        "M48",
        "scorer crash becomes TASK_FAILURE",
        S + "runner.py",
        "                        Outcome.HARNESS_ERROR,\n"
        '                        {"stage": "scorer",',
        "                        Outcome.TASK_FAILURE,\n"
        '                        {"stage": "scorer",',
    ),
    Mutant(
        "M28",
        "tool failure becomes HARNESS_ERROR",
        S + "runner.py",
        "                Outcome.TOOL_ERROR,\n",
        "                Outcome.HARNESS_ERROR,\n",
    ),
    Mutant(
        "M29",
        "tool version not recorded in trajectory",
        S + "solver.py",
        '        step["tool_version"] = t.version\n',
        "",
    ),
    # -- reproduction / staleness ---------------------------------------------------
    Mutant(
        "M30",
        "reproduce ignores scorer changes",
        S + "reproduce.py",
        '    if digest(receipt["scorer"]) != digest(current["scorer"]):\n',
        "    if False:\n",
    ),
    Mutant(
        "M31",
        "reproduce ignores provider revision changes",
        S + "reproduce.py",
        "        if rp.get(k) != cp.get(k):\n",
        "        if False:\n",
    ),
    Mutant(
        "M32",
        "unpinned provider claimed as exactly reproduced",
        S + "reproduce.py",
        "    elif not pinned:\n        rep.verdict = NOT_EXACT\n",
        "    elif False:\n        rep.verdict = NOT_EXACT\n",
    ),
    Mutant(
        "M33",
        "divergent rerun reported as reproduced",
        S + "reproduce.py",
        "    elif differing:\n        rep.verdict = DIVERGED\n",
        "    elif False:\n        rep.verdict = DIVERGED\n",
    ),
    Mutant(
        "M34",
        "stale config accepted",
        S + "reproduce.py",
        "        elif digest_bytes(p.read_bytes()) != recorded:\n",
        "        elif False:\n",
    ),
    Mutant(
        "M35",
        "reproduction from an invalid receipt not refused",
        S + "reproduce.py",
        "    if not check.ok:\n        rep.mismatches = check.problems\n        return rep\n",
        "",
    ),
    # -- comparison ---------------------------------------------------------------
    Mutant(
        "M36",
        "scorer change ignored by comparability",
        S + "compare.py",
        '    if digest(a["scorer"]) != digest(b["scorer"]):\n',
        "    if False:\n",
    ),
    Mutant(
        "M37",
        "judge change ignored by comparability",
        S + "compare.py",
        '    if digest(a.get("judge")) != digest(b.get("judge")):\n',
        "    if False:\n",
    ),
    Mutant(
        "M38",
        "different datasets compared directly",
        S + "compare.py",
        '    if da["id"] != db["id"]:\n',
        "    if False:\n",
    ),
    Mutant(
        "M39",
        "changed rows paired as identical",
        S + "compare.py",
        "    same_rows = [cid for cid in shared if ca[cid]"
        '["row_digest"] == cb[cid]["row_digest"]]\n',
        "    same_rows = list(shared)\n",
    ),
    # -- router ---------------------------------------------------------------------
    Mutant(
        "M40",
        "router uses unverified evidence",
        S + "router.py",
        "        if not v.ok:\n            skipped.append",
        "        if False:\n            skipped.append",
    ),
    Mutant(
        "M41",
        "router ignores evidence age",
        S + "router.py",
        "        if max_age is not None and age > float(max_age):\n",
        "        if False:\n",
    ),
    Mutant(
        "M42",
        "router ignores model revision drift",
        S + "router.py",
        '        if ev["revision"] != ident.revision:\n',
        "        if False:\n",
    ),
    Mutant(
        "M43",
        "router treats unknown cost as within limit",
        S + "router.py",
        '            if ev["cost_per_case_usd"] is None:\n',
        "            if False:\n",
    ),
    Mutant(
        "M44",
        "router ignores incomparable evidence",
        S + "router.py",
        "        on_basis = [r for r in mine if basis is not None and _basis(r) == basis]\n",
        "        on_basis = list(mine)\n",
    ),
    Mutant(
        "M45",
        "router quality floor uses point estimate under wilson_lower",
        S + "router.py",
        '            if bound_kind == "wilson_lower"\n',
        '            if bound_kind == "never"\n',
    ),
    Mutant(
        "M46",
        "cost invented when usage tokens are missing",
        S + "cost.py",
        '        if it is None or ot is None:\n            return None, "UNKNOWN"\n',
        "        if it is None or ot is None:\n            it, ot = 0, 0\n",
    ),
    Mutant(
        "M47",
        "literal credentials accepted in config",
        S + "config.py",
        "    if secrets:\n",
        "    if False:\n",
    ),
]

#: id -> written justification for a mutant that cannot change observable behaviour
EQUIVALENT: dict[str, str] = {
    "M04": (
        "verify_receipt's summary check 'accounted != requested' is implied by the per-record "
        "checks it follows: accounted == len(records with a valid outcome) and requested == "
        "len(selected). Any inequality requires a missing record, an extra record, a duplicate "
        "record, or an unknown outcome - each already adds its own problem. Removing it never "
        "changes Verification.ok, only drops a redundant summary line. Kept as a readable "
        "summary for humans."
    ),
}


def run(
    cmd: list[str], env: dict[str, str], timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=WORK, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated mutant ids")
    ap.add_argument("--json", help="write results here")
    args = ap.parse_args()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    for d in ("src", "tests", "examples"):
        shutil.copytree(
            ROOT / d, WORK / d, ignore=shutil.ignore_patterns("__pycache__", ".evalledger", "*.pyc")
        )
    env = {
        **os.environ,
        "PYTHONPATH": str(WORK / "src") + os.pathsep + str(WORK / "tests"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    py = sys.executable
    probe = run([py, "-c", "import evalledger; print(evalledger.__file__)"], env)
    loaded = probe.stdout.strip()
    if not loaded.startswith(str(WORK / "src")):
        print(f"GATE ERROR: evalledger imported from {loaded!r}, not the mutation tree")
        return 2
    pytest_cmd = [py, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "tests"]
    t0 = time.time()
    base = run(pytest_cmd, env)
    if base.returncode != 0:
        print("GATE ERROR: baseline test suite fails; mutation scores would be meaningless")
        print(base.stdout[-2000:])
        return 2
    print(f"baseline: PASS ({time.time() - t0:.1f}s), module {loaded}")
    selected = MUTANTS
    if args.only:
        want = set(args.only.split(","))
        selected = [m for m in MUTANTS if m.id in want]
    results = []
    for m in selected:
        path = WORK / m.file
        original = path.read_text()
        n = original.count(m.find)
        if n != 1:
            status, killer = "INVALID", f"FIND occurs {n} times"
        else:
            path.write_text(original.replace(m.find, m.replace, 1))
            try:
                res = run(pytest_cmd, env)
            finally:
                path.write_text(original)
            if res.returncode == 0:
                status, killer = "SURVIVED", ""
            else:
                status = "KILLED"
                failed = [ln for ln in res.stdout.splitlines() if ln.startswith("FAILED")]
                killer = failed[0][7:120] if failed else res.stdout.strip().splitlines()[-1][:120]
        if status == "SURVIVED" and m.id in EQUIVALENT:
            status = "EQUIVALENT"
        results.append(
            {"id": m.id, "invariant": m.invariant, "status": status, "killed_by": killer}
        )
        print(f"{m.id} {status:<10} {m.invariant}" + (f"  <- {killer}" if killer else ""))
        if status == "KILLED":
            # restore check: the restored tree must pass again for at least the first mutant
            pass
    post = run(pytest_cmd, env)
    restored_ok = post.returncode == 0
    counts = {
        s: sum(r["status"] == s for r in results)
        for s in ("KILLED", "SURVIVED", "INVALID", "EQUIVALENT")
    }
    ok = counts["SURVIVED"] == 0 and counts["INVALID"] == 0 and restored_ok
    print(
        f"\nmutants: {len(results)}  killed {counts['KILLED']}  survived {counts['SURVIVED']}  "
        f"invalid {counts['INVALID']}  equivalent {counts['EQUIVALENT']}  "
        f"post-restore baseline {'PASS' if restored_ok else 'FAIL'}"
    )
    print("GATE: " + ("PASS" if ok else "FAIL"))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "results": results,
                    "counts": counts,
                    "post_restore_baseline": restored_ok,
                    "equivalent_justifications": EQUIVALENT,
                },
                indent=2,
            )
            + "\n"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
