"""Eval-integrity adversarial suite (``evalledger selfcheck``).

Each scenario builds a deliberately broken evaluator, a forged receipt, or a changed
input, and checks that EvalLedger detects it by the *specific rule* expected - not by
any error at all. Every scenario also runs an honest control arm that must verify
cleanly, so a verifier that rejects everything cannot pass the suite.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compare import PARTIAL, STRONG, compare
from .config import EvalConfig, from_dict
from .providers import ProviderError, Request
from .receipt import aggregate, seal, verify_receipt
from .reproduce import INPUT_MISMATCH, REFUSED, REPRODUCED, reproduce, staleness
from .runner import ProvenanceMismatch, run_eval
from .solver import SolverContext
from .store import Store
from .taxonomy import ErrorKind

N_CASES = 30


# -- deliberately broken components (referenced by import path) -----------------


def crashing_solver(ctx: SolverContext) -> str:
    if ctx.case.case_id.endswith(("3", "7")):
        raise RuntimeError("harness bug: unexpected None")
    return ctx.model().text


def outage_hiding_solver(ctx: SolverContext) -> str:
    try:
        return ctx.model().text
    except ProviderError:
        return ""  # a buggy harness that turns outages into empty answers


def flaky_tool(order: str) -> str:
    if order.endswith("5"):
        raise TimeoutError("inventory service timed out")
    return "ok"


def tool_solver(ctx: SolverContext) -> str:
    ctx.tool("inventory", order=ctx.case.case_id)
    return ctx.model().text


def judge_ok(request: Request) -> str:
    return "Looks right.\nVERDICT: PASS"


def judge_unparseable(request: Request) -> str:
    return "Probably fine, 8/10"


def judge_down(request: Request) -> str:
    raise ProviderError(ErrorKind.INFRA, "judge endpoint 503")


# -- harness ---------------------------------------------------------------------


@dataclass
class Result:
    id: str
    title: str
    expected: str
    honest_ok: bool
    detected: bool
    evidence: str

    @property
    def passed(self) -> bool:
        return self.honest_ok and self.detected

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "expected": self.expected,
            "honest_ok": self.honest_ok,
            "detected": self.detected,
            "passed": self.passed,
            "evidence": self.evidence,
        }


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = Store(root / ".evalledger")
        self.data = root / "cases.jsonl"
        self.write_rows(self.rows())

    @staticmethod
    def rows() -> list[dict[str, Any]]:
        return [
            {"id": f"case-{i:02d}", "input": f"q{i}", "expected": f"a{i}"} for i in range(N_CASES)
        ]

    def write_rows(self, rows: list[dict[str, Any]]) -> None:
        self.data.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def config(self, **over: Any) -> EvalConfig:
        raw: dict[str, Any] = {
            "name": "integrity",
            "task": "integrity",
            "dataset": {"path": str(self.data), "id": "integrity-ds"},
            "provider": {"kind": "mock", "model": "model-a", "accuracy": 0.8},
            "scorer": {"kind": "exact"},
            "run": {"seed": 1, "concurrency": 2},
        }
        for k, v in over.items():
            raw[k] = {**raw[k], **v} if isinstance(v, dict) and isinstance(raw.get(k), dict) else v
        return from_dict(raw, self.root)

    def run(self, **over: Any) -> dict[str, Any]:
        return run_eval(self.config(**over), self.store)

    def receipt_path(self, run_id: str) -> Path:
        return self.store.root / "runs" / run_id / "receipt.json"

    def overwrite(self, receipt: Mapping[str, Any]) -> None:
        self.receipt_path(receipt["run_id"]).write_text(json.dumps(receipt))


@contextmanager
def _env() -> Iterator[Env]:
    d = Path(tempfile.mkdtemp(prefix="evalledger-selfcheck-"))
    try:
        yield Env(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _has(problems: list[str], rule: str) -> bool:
    return any(rule in p for p in problems)


Scenario = Callable[[Env], Result]
SCENARIOS: list[tuple[str, str, Scenario]] = []


def scenario(sid: str, title: str) -> Callable[[Scenario], Scenario]:
    def deco(fn: Scenario) -> Scenario:
        SCENARIOS.append((sid, title, fn))
        return fn

    return deco


def _drop_errors(r: dict[str, Any]) -> dict[str, Any]:
    f = copy.deepcopy(r)
    f["cases"] = [c for c in f["cases"] if c["outcome"] in ("PASS", "TASK_FAILURE")]
    return f


# -- scenarios -------------------------------------------------------------------


@scenario("A1", "failed rows deleted from a receipt")
def a1(env: Env) -> Result:
    r = env.run(provider={"infra_error_rate": 0.2})
    honest = verify_receipt(r).ok
    forged = _drop_errors(r)
    v = verify_receipt(forged)
    rule = "requested != accounted"
    return Result("A1", "", rule, honest, _has(v.problems, rule), "; ".join(v.problems[:2]))


@scenario("A2", "failed rows deleted and aggregate recomputed + resealed")
def a2(env: Env) -> Result:
    r = env.run(provider={"infra_error_rate": 0.2})
    forged = _drop_errors(r)
    forged["aggregate"] = aggregate(
        forged["cases"], [c["case_id"] for c in forged["cases"]], []
    )  # a 'success on valid rows' number
    forged = seal(forged)
    v = verify_receipt(forged)
    rule = "requested != accounted"
    return Result(
        "A2", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:2])
    )


@scenario("A3", "failed rows deleted from cases AND selection, fully resealed")
def a3(env: Env) -> Result:
    r = env.run(provider={"infra_error_rate": 0.2})
    honest = env.store.verify_run(r["run_id"]).ok
    from .canonical import digest

    forged = _drop_errors(r)
    kept = [c["case_id"] for c in forged["cases"]]
    forged["dataset"]["selected_case_ids"] = kept
    forged["dataset"]["selected_digest"] = digest(kept)
    forged["aggregate"] = aggregate(forged["cases"], kept, [])
    forged = seal(forged)  # self-consistent file; only the bindings can catch it
    self_ok = verify_receipt(forged).ok
    env.overwrite(forged)
    v = env.store.verify_run(r["run_id"])
    rule = "selection binding"
    detected = _has(v.problems, rule) and _has(v.problems, "indexed")
    return Result(
        "A3",
        "",
        f"{rule} + index digest",
        honest and self_ok,
        detected,
        "; ".join(v.problems[:2]),
    )


@scenario("A4", "runner that silently skips error rows")
def a4(env: Env) -> Result:
    # The real runner, then the receipt a skipping runner would emit: nothing for errors.
    r = env.run(provider={"fail_case_ids": ["case-04", "case-09"]})
    honest = r["aggregate"]["counts"]["INFRA_ERROR"] == 2 and verify_receipt(r).ok
    forged = copy.deepcopy(r)
    forged["cases"] = [c for c in forged["cases"] if c["outcome"] != "INFRA_ERROR"]
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    v = verify_receipt(seal(forged))
    rule = "selected case(s) have no record"
    return Result("A4", "", rule, honest, _has(v.problems, rule), "; ".join(v.problems[:2]))


@scenario("A5", "duplicate PASS record to inflate the score")
def a5(env: Env) -> Result:
    r = env.run()
    forged = copy.deepcopy(r)
    passing = next(c for c in forged["cases"] if c["outcome"] == "PASS")
    failing = next(c for c in forged["cases"] if c["outcome"] != "PASS")
    forged["cases"] = [c for c in forged["cases"] if c is not failing] + [copy.deepcopy(passing)]
    v = verify_receipt(seal(forged))
    rule = "duplicate case records"
    return Result(
        "A5", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:2])
    )


@scenario("A6", "headline pass rate edited, receipt resealed")
def a6(env: Env) -> Result:
    r = env.run()
    forged = copy.deepcopy(r)
    forged["aggregate"]["pass_rate"]["value"] = 0.97
    v = verify_receipt(seal(forged))
    rule = "aggregate does not match per-case records"
    return Result(
        "A6", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:1])
    )


@scenario("B1", "dataset changed while config pins the old revision")
def b1(env: Env) -> Result:
    r = env.run()
    pinned = r["dataset"]["revision"]
    honest = env.run(dataset={"revision": pinned})["dataset"]["revision"] == pinned
    rows = env.rows()
    rows[7]["expected"] = "a-changed"
    env.write_rows(rows)
    try:
        env.run(dataset={"revision": pinned})
        detected, ev = False, "run accepted the changed dataset"
    except ProvenanceMismatch as exc:
        detected, ev = "provenance mismatch" in str(exc), str(exc)[:160]
    return Result("B1", "", "provenance mismatch", honest, detected, ev)


@scenario("B2", "stored dataset snapshot tampered after the run")
def b2(env: Env) -> Result:
    r = env.run()
    honest = env.store.verify_run(r["run_id"]).ok
    ptr = env.store._snapshot_pointer(r["dataset"]["revision"])
    obj = env.store._object_path(ptr.read_text().strip())
    snap = json.loads(obj.read_text())
    snap["rows"][3]["expected"] = "forged"
    obj.write_text(json.dumps(snap))
    v = env.store.verify_run(r["run_id"])
    rule = "corrupted"
    return Result("B2", "", rule, honest, _has(v.problems, rule), "; ".join(v.problems[:1]))


@scenario("B3", "receipt row digest swapped to claim a different case content")
def b3(env: Env) -> Result:
    r = env.run()
    from .canonical import digest

    forged = copy.deepcopy(r)
    forged["cases"][0]["row_digest"] = digest({"case": "something else"})
    forged = seal(forged)
    env.overwrite(forged)
    v = env.store.verify_run(r["run_id"])
    rule = "row digest differs from snapshot"
    return Result("B3", "", rule, True, _has(v.problems, rule), "; ".join(v.problems[:2]))


@scenario("C1", "judge model substituted between runs")
def c1(env: Env) -> Result:
    def judge(model: str) -> dict[str, Any]:
        return {
            "kind": "judge",
            "provider": {"kind": "python", "function": f"{__name__}:judge_ok", "model": model},
        }

    a = env.run(scorer=judge("judge-1"))
    same = env.run(scorer=judge("judge-1"), provider={"model": "model-b"})
    b = env.run(scorer=judge("judge-2"), provider={"model": "model-b"})
    honest = compare(a, same)["comparability"] == STRONG
    res = compare(a, b)
    detected = res["comparability"] == PARTIAL and any(
        "not a clean model-only comparison" in w for w in res["warnings"]
    )
    return Result(
        "C1",
        "",
        "PARTIALLY_COMPARABLE + judge warning",
        honest,
        detected,
        f"{res['comparability']}: {res['reasons']}",
    )


@scenario("C2", "scorer changed between runs")
def c2(env: Env) -> Result:
    a = env.run()
    honest = compare(a, env.run())["comparability"] == STRONG
    b = env.run(scorer={"kind": "regex", "pattern": "a.*"})
    res = compare(a, b)
    return Result(
        "C2",
        "",
        "PARTIALLY_COMPARABLE (scorer changed)",
        honest,
        res["comparability"] == PARTIAL and "scorer changed" in res["reasons"],
        str(res["reasons"]),
    )


@scenario("C3", "scorer code edited without a version bump, then reproduced")
def c3(env: Env) -> Result:
    mod = env.root / "lenient_scorer.py"
    mod.write_text("def score(output, case):\n    return output == case.expected\n")
    cfg = {"scorer": {"kind": "python", "function": "lenient_scorer:score"}}
    import importlib
    import sys

    sys.path.insert(0, str(env.root))
    try:
        r = env.run(**cfg)
        honest = reproduce(env.store, r["run_id"]).verdict == REPRODUCED
        mod.write_text("def score(output, case):\n    return True\n")
        importlib.reload(sys.modules["lenient_scorer"])
        rep = reproduce(env.store, r["run_id"], execute=False)
    finally:
        sys.path.remove(str(env.root))
        sys.modules.pop("lenient_scorer", None)
    return Result(
        "C3",
        "",
        "INPUT_MISMATCH (scorer changed)",
        honest,
        rep.verdict == INPUT_MISMATCH and any("scorer" in m for m in rep.mismatches),
        str(rep.mismatches[:1]),
    )


@scenario("D1", "model name edited in a receipt")
def d1(env: Env) -> Result:
    r = env.run()
    honest = env.store.verify_run(r["run_id"]).ok
    forged = copy.deepcopy(r)
    forged["provider"]["identity"]["model"] = "frontier-model-xl"
    env.overwrite(forged)
    v = env.store.verify_run(r["run_id"])
    rule = "receipt digest mismatch"
    detected = _has(v.problems, rule) and reproduce(env.store, r["run_id"]).verdict == REFUSED
    return Result("D1", "", rule, honest, detected, "; ".join(v.problems[:1]))


@scenario("D2", "provider served a different model than configured")
def d2(env: Env) -> Result:
    honest = not any("MODEL SUBSTITUTION" in w for w in env.run()["warnings"])
    r = env.run(provider={"served_model": "model-a-quantized"})
    return Result(
        "D2",
        "",
        "MODEL SUBSTITUTION warning",
        honest,
        any("MODEL SUBSTITUTION" in w for w in r["warnings"]),
        str(r["warnings"][:1]),
    )


@scenario("D3", "provider implementation swapped, run re-presented as the same")
def d3(env: Env) -> Result:
    mod = env.root / "swap_model.py"
    mod.write_text("def answer(req):\n    return 'a1'\n")
    import importlib
    import sys

    sys.path.insert(0, str(env.root))
    try:
        r = env.run(
            provider={
                "kind": "python",
                "function": "swap_model:answer",
                "model": "m",
                "pinned": True,
            }
        )
        honest = reproduce(env.store, r["run_id"]).verdict == REPRODUCED
        mod.write_text("def answer(req):\n    return 'a1'  # now a different model\n\n")
        importlib.reload(sys.modules["swap_model"])
        rep = reproduce(env.store, r["run_id"], execute=False)
    finally:
        sys.path.remove(str(env.root))
        sys.modules.pop("swap_model", None)
    return Result(
        "D3",
        "",
        "INPUT_MISMATCH (provider revision)",
        honest,
        rep.verdict == INPUT_MISMATCH and any("provider revision" in m for m in rep.mismatches),
        str(rep.mismatches[:1]),
    )


@scenario("E1", "harness crashes on some cases")
def e1(env: Env) -> Result:
    r = env.run(solver={"function": f"{__name__}:crashing_solver"})
    agg = r["aggregate"]
    detected = agg["counts"]["HARNESS_ERROR"] == 6 and agg["accounted"] == N_CASES
    honest = env.run()["aggregate"]["counts"]["HARNESS_ERROR"] == 0
    return Result(
        "E1",
        "",
        "HARNESS_ERROR, kept in denominator",
        honest,
        detected and agg["pass_rate"]["denominator"] == N_CASES,
        f"counts={ {k: v for k, v in agg['counts'].items() if v} }",
    )


@scenario("E2", "harness crash relabelled as TASK_FAILURE in the receipt")
def e2(env: Env) -> Result:
    r = env.run(solver={"function": f"{__name__}:crashing_solver"})
    forged = copy.deepcopy(r)
    for c in forged["cases"]:
        if c["outcome"] == "HARNESS_ERROR":
            c["outcome"] = "TASK_FAILURE"
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    v = verify_receipt(seal(forged))
    rule = "TASK_FAILURE without a score"
    return Result(
        "E2", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:1])
    )


@scenario("F1", "provider outage")
def f1(env: Env) -> Result:
    r = env.run(provider={"fail_case_ids": [f"case-{i:02d}" for i in range(10)]})
    c = r["aggregate"]["counts"]
    honest = env.run()["aggregate"]["counts"]["INFRA_ERROR"] == 0
    detected = c["INFRA_ERROR"] == 10 and c["TASK_FAILURE"] + c["PASS"] == N_CASES - 10
    return Result(
        "F1",
        "",
        "INFRA_ERROR, never TASK_FAILURE",
        honest,
        detected,
        f"INFRA_ERROR={c['INFRA_ERROR']} TASK_FAILURE={c['TASK_FAILURE']}",
    )


@scenario("F2", "harness swallows the outage and returns an empty answer")
def f2(env: Env) -> Result:
    r = env.run(
        provider={"fail_case_ids": ["case-01", "case-02"]},
        solver={"function": f"{__name__}:outage_hiding_solver"},
    )
    outs = {c["case_id"]: c["outcome"] for c in r["cases"]}
    detected = outs["case-01"] == outs["case-02"] == "INFRA_ERROR"
    return Result(
        "F2",
        "",
        "INFRA_ERROR despite swallowed exception",
        verify_receipt(r).ok,
        detected,
        f"case-01={outs['case-01']}",
    )


@scenario("F3", "outage relabelled as TASK_FAILURE with a fabricated score")
def f3(env: Env) -> Result:
    r = env.run(provider={"fail_case_ids": ["case-05"]})
    forged = copy.deepcopy(r)
    c = next(c for c in forged["cases"] if c["case_id"] == "case-05")
    c.update(outcome="TASK_FAILURE", error=None, score={"passed": False, "value": 0.0})
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    v = verify_receipt(seal(forged))
    rule = "final model call failed"
    return Result(
        "F3", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:1])
    )


@scenario("F4", "outage relabelled as PASS")
def f4(env: Env) -> Result:
    r = env.run(provider={"fail_case_ids": ["case-05"]})
    forged = copy.deepcopy(r)
    c = next(c for c in forged["cases"] if c["case_id"] == "case-05")
    c.update(outcome="PASS", error=None, score={"passed": True, "value": 1.0})
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    v = verify_receipt(seal(forged))
    rule = "final model call failed"
    return Result(
        "F4", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:1])
    )


@scenario("G1", "agent tool crashes")
def g1(env: Env) -> Result:
    tools = [{"name": "inventory", "version": "3.1", "function": f"{__name__}:flaky_tool"}]
    r = env.run(tools=tools, solver={"function": f"{__name__}:tool_solver"})
    c = r["aggregate"]["counts"]
    steps = [s for x in r["cases"] for s in x["trajectory"] if s["type"] == "tool_call"]
    detected = c["TOOL_ERROR"] == 3 and all(s.get("tool_version") == "3.1" for s in steps)
    return Result(
        "G1",
        "",
        "TOOL_ERROR with tool version evidence",
        verify_receipt(r).ok,
        detected,
        f"TOOL_ERROR={c['TOOL_ERROR']}",
    )


@scenario("J1", "judge fails or returns an unparseable verdict")
def j1(env: Env) -> Result:
    base: dict[str, Any] = {"kind": "judge", "provider": {"kind": "python", "model": "j"}}
    ok = env.run(
        scorer={**base, "provider": {**base["provider"], "function": f"{__name__}:judge_ok"}}
    )
    bad = env.run(
        scorer={
            **base,
            "provider": {**base["provider"], "function": f"{__name__}:judge_unparseable"},
        }
    )
    down = env.run(
        scorer={**base, "provider": {**base["provider"], "function": f"{__name__}:judge_down"}}
    )
    honest = ok["aggregate"]["counts"]["JUDGE_ERROR"] == 0
    scored = ok["aggregate"]["scored"]
    detected = all(
        x["aggregate"]["counts"]["JUDGE_ERROR"] == scored
        and x["aggregate"]["pass_rate"]["denominator"] == N_CASES
        for x in (bad, down)
    )
    return Result(
        "J1",
        "",
        "JUDGE_ERROR, never score 0",
        honest,
        detected,
        f"unparseable={bad['aggregate']['counts']['JUDGE_ERROR']} "
        f"down={down['aggregate']['counts']['JUDGE_ERROR']}",
    )


@scenario("J2", "judge failures relabelled as TASK_FAILURE")
def j2(env: Env) -> Result:
    judge = {
        "kind": "judge",
        "provider": {"kind": "python", "model": "j", "function": f"{__name__}:judge_unparseable"},
    }
    r = env.run(scorer=judge)
    forged = copy.deepcopy(r)
    for c in forged["cases"]:
        if c["outcome"] == "JUDGE_ERROR":
            c.update(outcome="TASK_FAILURE", error=None, score={"passed": False, "value": 0.0})
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    v = verify_receipt(seal(forged))
    rule = "without a parsed judge verdict"
    return Result(
        "J2", "", rule, verify_receipt(r).ok, _has(v.problems, rule), "; ".join(v.problems[:1])
    )


@scenario("H1", "config edited after the receipt was generated")
def h1(env: Env) -> Result:
    cfg_path = env.root / "eval.yaml"
    cfg_path.write_text(
        f"dataset: {{path: {env.data.name}, id: integrity-ds}}\n"
        "provider: {kind: mock, model: model-a}\nscorer: {kind: exact}\n"
    )
    from .config import load_config

    r = run_eval(load_config(cfg_path), env.store)
    honest = staleness(r) == []
    cfg_path.write_text(cfg_path.read_text().replace("model-a", "model-b"))
    s = staleness(r)
    return Result(
        "H1",
        "",
        "STALE (config changed)",
        honest,
        any("config file" in x and "changed" in x for x in s),
        str(s[:1]),
    )


@scenario("H2", "dataset file edited after the receipt was generated")
def h2(env: Env) -> Result:
    r = env.run()
    honest = staleness(r) == []
    rows = env.rows()
    rows[0]["input"] = "edited"
    env.write_rows(rows)
    s = staleness(r)
    return Result(
        "H2",
        "",
        "STALE (dataset changed)",
        honest,
        any("dataset source" in x for x in s),
        str(s[:1]),
    )


@scenario("H3", "crashed run presented as evidence")
def h3(env: Env) -> Result:
    r = env.run()
    honest = env.store.is_trustworthy(r["run_id"])
    env.store.begin_run(
        "01CRASHEDRUN0000000000000",
        {
            "started_at": "2026-01-01T00:00:00Z",
            "dataset_id": "integrity-ds",
            "dataset_revision": r["dataset"]["revision"],
            "provider": "mock",
            "model": "model-a",
        },
    )
    v = env.store.verify_run("01CRASHEDRUN0000000000000")
    rule = "never finished"
    return Result("H3", "", rule, honest, _has(v.problems, rule), "; ".join(v.problems[:1]))


@scenario("I1", "legitimate deterministic repeat")
def i1(env: Env) -> Result:
    r = env.run(provider={"infra_error_rate": 0.1, "model_error_rate": 0.05})
    rep = reproduce(env.store, r["run_id"])
    # control: a changed seed must NOT reproduce identically
    other = env.run(provider={"infra_error_rate": 0.1, "model_error_rate": 0.05}, run={"seed": 2})
    honest = other["outcomes_digest"] != r["outcomes_digest"]
    return Result(
        "I1",
        "",
        "REPRODUCED",
        honest,
        rep.verdict == REPRODUCED and rep.agreement["identical"] == N_CASES,
        f"{rep.verdict} {rep.agreement.get('identical')}/{N_CASES}",
    )


def _policy(cands: list[dict[str, Any]], **req: Any) -> dict[str, Any]:
    from .router import validate_policy

    base = {"min_success_rate": 0.5, "quality_bound": "point", "min_cases": 10}
    return validate_policy(
        {"task": "integrity", "requirements": {**base, **req}, "candidates": cands}
    )


_PRICES = {
    "source": "selfcheck",
    "updated_at": "2026-01-01",
    "models": {"mock/model-a": {"input_per_mtok": 1.0, "output_per_mtok": 1.0}},
}


@scenario("R1", "stale routing evidence")
def r1(env: Env) -> Result:
    from datetime import datetime, timedelta, timezone

    from .router import route

    env.run()
    cand = [{"kind": "mock", "model": "model-a", "accuracy": 0.8}]
    honest = route(env.store, _policy(cand, max_evidence_age_days=30))["status"] == "SELECTED"
    later = datetime.now(timezone.utc) + timedelta(days=60)
    d = route(env.store, _policy(cand, max_evidence_age_days=30), now=later)
    drift = route(env.store, _policy([{**cand[0], "accuracy": 0.95}]))
    detected = d["status"] == "ROUTING_EVIDENCE_STALE" and drift["status"] == (
        "ROUTING_EVIDENCE_STALE"
    )
    return Result(
        "R1",
        "",
        "ROUTING_EVIDENCE_STALE (age, revision drift)",
        honest,
        detected,
        f"age: {d['status']}; drift: {drift['status']}",
    )


@scenario("R2", "no model meets the constraints")
def r2(env: Env) -> Result:
    from .router import route

    env.run()
    cand = [{"kind": "mock", "model": "model-a", "accuracy": 0.8}]
    honest = route(env.store, _policy(cand))["status"] == "SELECTED"
    d = route(env.store, _policy(cand, min_success_rate=0.99))
    return Result(
        "R2",
        "",
        "NO_ELIGIBLE_MODEL",
        honest,
        d["status"] == "NO_ELIGIBLE_MODEL" and d["selected"] is None,
        f"{d['status']}: {d['candidates'][0]['reasons'][:1]}",
    )


@scenario("R3", "forged better-looking evidence offered to the router")
def r3(env: Env) -> Result:
    from .router import route

    r = env.run()
    cand = [{"kind": "mock", "model": "model-a", "accuracy": 0.8}]
    honest = route(env.store, _policy(cand))["selected"] == "mock/model-a"
    forged = copy.deepcopy(r)
    for c in forged["cases"]:
        c["outcome"] = "PASS"
        c["score"] = {"passed": True, "value": 1.0}
        c["error"] = None
    forged["aggregate"] = aggregate(forged["cases"], forged["dataset"]["selected_case_ids"], [])
    env.overwrite(seal(forged))
    d = route(env.store, _policy(cand, min_success_rate=0.95))
    detected = d["status"] == "NO_ELIGIBLE_MODEL" and any(
        s["run_id"] == r["run_id"] for s in d["skipped_invalid_evidence"]
    )
    return Result(
        "R3",
        "",
        "forged receipt skipped as invalid evidence",
        honest,
        detected,
        str(d["skipped_invalid_evidence"][:1]),
    )


@scenario("R4", "cost limit with missing cost metadata")
def r4(env: Env) -> Result:
    from .router import route

    cand = [{"kind": "mock", "model": "model-a", "accuracy": 0.8}]
    env.run(pricing=_PRICES)
    honest = route(env.store, _policy(cand, max_cost_per_case_usd=0.01))["status"] == "SELECTED"
    env.run()  # newer evidence without any price table
    d = route(env.store, _policy(cand, max_cost_per_case_usd=0.01))
    return Result(
        "R4",
        "",
        "COST_UNKNOWN fails closed",
        honest,
        d["candidates"][0]["status"] == "COST_UNKNOWN",
        d["candidates"][0]["status"],
    )


# -- scenarios added after the independent review --------------------------------


def _cost_provider(request: Request, poison: bool) -> Any:
    from .providers import Response, Usage

    cost = float("nan") if poison and (request.case_id or "").endswith("3") else 0.001
    return Response(text="x", usage=Usage(1, 1, cost), served_model="cost")


def finite_cost_provider(request: Request) -> Any:
    return _cost_provider(request, poison=False)


def nan_cost_provider(request: Request) -> Any:
    return _cost_provider(request, poison=True)


def tool_swallowing_solver(ctx: SolverContext) -> str:
    from .solver import ToolFailure

    ctx.model()
    try:
        return str(ctx.tool("inventory", order=ctx.case.case_id))
    except ToolFailure:
        return ctx.case.expected if isinstance(ctx.case.expected, str) else ""


@scenario("A7", "index-only tamper: run re-labelled as evidence for another task/model")
def a7(env: Env) -> Result:
    import sqlite3

    r = env.run()
    honest = env.store.verify_run(r["run_id"]).ok
    con = sqlite3.connect(env.store.db_path)
    con.execute(
        "UPDATE runs SET task='medical', model='frontier', pass_rate=0.99 WHERE run_id=?",
        (r["run_id"],),
    )
    con.commit()
    con.close()
    v = env.store.verify_run(r["run_id"])
    rule = "index binding"
    return Result("A7", "", rule, honest, _has(v.problems, rule), "; ".join(v.problems[:2]))


@scenario("C4", "compare offered a re-sealed forged receipt (index untouched)")
def c4(env: Env) -> Result:
    from .compare import NOT, compare_runs

    a = env.run()
    b = env.run(provider={"model": "model-b"})
    honest = compare_runs(env.store, a["run_id"], b["run_id"])["comparability"] == STRONG
    forged = copy.deepcopy(b)
    for c in forged["cases"]:
        if c["outcome"] == "TASK_FAILURE":
            c["outcome"] = "PASS"
            c["score"] = {"passed": True, "value": 1.0}
    env.overwrite(reseal_for_selfcheck(forged))
    res = compare_runs(env.store, a["run_id"], b["run_id"])
    detected = res["comparability"] == NOT and "fails store verification" in res["reasons"][0]
    return Result(
        "C4",
        "",
        "NOT_DIRECTLY_COMPARABLE (store verification)",
        honest,
        detected,
        str(res["reasons"][:1]),
    )


@scenario("C5", "interrupted run compared as if complete")
def c5(env: Env) -> Result:
    a = env.run()
    honest = compare(a, env.run(provider={"model": "model-b"}))["comparability"] == STRONG
    b = copy.deepcopy(env.run(provider={"model": "model-b"}))
    b["status"] = "INCOMPLETE"
    for c in b["cases"][10:]:
        c.update(
            outcome="CANCELLED",
            score=None,
            output_digest=None,
            trajectory=[],
            error={"stage": "run", "message": "interrupted"},
            latency_ms={},
            usage=[],
            cost_usd=None,
            cost_basis="none",
        )
        c.pop("output", None)
    res = compare(a, reseal_for_selfcheck(b))
    p = res.get("paired") or {}
    detected = res["comparability"] == PARTIAL and p.get("cases") == 10
    return Result(
        "C5",
        "",
        "PARTIALLY_COMPARABLE, cancelled cases not paired",
        honest,
        detected,
        f"{res['comparability']} paired={p.get('cases')}",
    )


@scenario("E3", "provider returns a non-finite cost for one case")
def e3(env: Env) -> Result:
    ok = env.run(
        provider={"kind": "python", "function": f"{__name__}:finite_cost_provider", "model": "cost"}
    )
    honest = ok["aggregate"]["counts"]["HARNESS_ERROR"] == 0 and ok["aggregate"]["cost"]["complete"]
    r = env.run(
        provider={"kind": "python", "function": f"{__name__}:nan_cost_provider", "model": "cost"}
    )
    c = r["aggregate"]["counts"]
    bad = [x for x in r["cases"] if x["outcome"] == "HARNESS_ERROR"]
    detected = (
        r["status"] == "COMPLETE"
        and c["HARNESS_ERROR"] == 3
        and all(x["error"]["stage"] == "record" for x in bad)
        and env.store.verify_run(r["run_id"]).ok
    )
    return Result(
        "E3",
        "",
        "HARNESS_ERROR for that case; run completes",
        honest,
        detected,
        f"status={r['status']} HARNESS_ERROR={c['HARNESS_ERROR']}",
    )


@scenario("G2", "solver swallows a tool failure and answers without the model")
def g2(env: Env) -> Result:
    tools = [{"name": "inventory", "version": "3.1", "function": f"{__name__}:flaky_tool"}]
    honest_r = env.run(tools=tools, solver={"function": f"{__name__}:tool_solver"})
    honest = honest_r["aggregate"]["counts"]["TOOL_ERROR"] == 3
    r = env.run(tools=tools, solver={"function": f"{__name__}:tool_swallowing_solver"})
    c = r["aggregate"]["counts"]
    forged = copy.deepcopy(r)
    for x in forged["cases"]:
        if x["outcome"] == "TOOL_ERROR":
            x.update(outcome="PASS", error=None, score={"passed": True, "value": 1.0})
    v = verify_receipt(reseal_for_selfcheck(forged))
    detected = c["TOOL_ERROR"] == 3 and _has(v.problems, "tool failure counted as a model verdict")
    return Result(
        "G2",
        "",
        "TOOL_ERROR; relabelling rejected",
        honest,
        detected,
        f"TOOL_ERROR={c['TOOL_ERROR']}; {v.problems[:1]}",
    )


@scenario("I2", "rerun of a run in which every case was an outage")
def i2(env: Env) -> Result:
    from .reproduce import NO_SCORED_CASES

    honest = reproduce(env.store, env.run()["run_id"]).verdict == REPRODUCED
    r = env.run(provider={"infra_error_rate": 1.0})
    rep = reproduce(env.store, r["run_id"])
    return Result(
        "I2",
        "",
        "NO_SCORED_CASES (not REPRODUCED)",
        honest,
        rep.verdict == NO_SCORED_CASES,
        rep.verdict,
    )


@scenario("R5", "failing cases cherry-picked into exclusions to pass the router")
def r5(env: Env) -> Result:
    from .router import route

    cand = [{"kind": "mock", "model": "model-a", "accuracy": 0.8}]
    honest_run = env.run()
    honest = route(env.store, _policy(cand))["status"] == "SELECTED"
    failing = [c["case_id"] for c in honest_run["cases"] if c["outcome"] != "PASS"]
    env.run(dataset={"exclude": [{"case_id": f, "reason": "ambiguous label"} for f in failing]})
    d = route(env.store, _policy(cand, min_success_rate=0.95))
    st = d["candidates"][0]
    return Result(
        "R5",
        "",
        "EXCESSIVE_EXCLUSIONS",
        honest,
        "EXCESSIVE_EXCLUSIONS" in (st.get("failed_gates") or []),
        st["status"],
    )


@scenario("R6", "evidence from one endpoint offered for another deployment")
def r6(env: Env) -> Result:
    from .providers import OpenAICompatProvider

    a = OpenAICompatProvider({"model": "x", "base_url": "https://api.example/v1"}).identity()
    b = OpenAICompatProvider({"model": "x", "base_url": "http://127.0.0.1:8080/v1"}).identity()
    c = OpenAICompatProvider({"model": "x", "base_url": "https://u:p@api.example/v1/"}).identity()
    detected = a.endpoint != b.endpoint and a.key == b.key and "u:p" not in str(c.endpoint)
    same = OpenAICompatProvider({"model": "x", "base_url": "https://api.example/v1/"}).identity()
    return Result(
        "R6",
        "",
        "endpoint is part of identity (credential-free)",
        a == same,
        detected,
        f"{a.endpoint} vs {b.endpoint}; {c.endpoint}",
    )


def reseal_for_selfcheck(r: dict[str, Any]) -> dict[str, Any]:
    r["aggregate"] = aggregate(
        r["cases"],
        r["dataset"]["selected_case_ids"],
        [e["case_id"] for e in r["dataset"]["excluded"]],
    )
    return seal(r)


def run_suite(only: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for sid, title, fn in SCENARIOS:
        if only and sid not in only:
            continue
        with _env() as env:
            try:
                res = fn(env)
            except Exception as exc:  # a scenario crash is a failed scenario
                res = Result(sid, title, "?", False, False, f"scenario crashed: {exc!r}")
        res.title = title
        out.append(res.to_dict())
    return out


def render(results: list[dict[str, Any]]) -> str:
    lines = ["EVALLEDGER SELFCHECK - eval-integrity adversarial suite", ""]
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"[{mark}] {r['id']:<3} {r['title']}")
        lines.append(f"       expected: {r['expected']}")
        if not r["honest_ok"]:
            lines.append("       CONTROL ARM FAILED (honest run was not accepted)")
        if not r["detected"]:
            lines.append("       ATTACK NOT DETECTED")
        lines.append(f"       evidence: {r['evidence'][:150]}")
    n = sum(r["passed"] for r in results)
    lines += ["", f"{n}/{len(results)} scenarios passed"]
    return "\n".join(lines)
