from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from astrometron.receipt import verify_receipt
from astrometron.runner import ProvenanceMismatch, run_eval
from astrometron.store import Store
from conftest import ROWS, make_config, write_jsonl


def outcomes(r: dict[str, Any]) -> Counter[str]:
    return Counter(c["outcome"] for c in r["cases"])


def test_clean_run_all_pass(store: Store, data_file: Path) -> None:
    r = run_eval(make_config(data_file), store)
    agg = r["aggregate"]
    assert agg["requested"] == agg["accounted"] == 20
    assert agg["counts"]["PASS"] == 20 and agg["pass_rate"]["value"] == 1.0
    assert verify_receipt(r).ok and store.verify_run(r["run_id"]).ok


def test_provider_outage_is_infra_error_not_task_failure(store: Store, data_file: Path) -> None:
    cfg = make_config(data_file, provider={"fail_case_ids": ["c01", "c02", "c03"]})
    r = run_eval(cfg, store)
    o = outcomes(r)
    assert o["INFRA_ERROR"] == 3 and o["TASK_FAILURE"] == 0 and o["PASS"] == 17
    agg = r["aggregate"]
    assert agg["pass_rate"]["denominator"] == 20  # outage cases stay in the denominator
    assert agg["pass_rate_of_scored"]["denominator"] == 17


def test_retries_recover_and_are_recorded(store: Store, tmp_path: Path) -> None:
    # infra_error_rate=1 always fails; with retries each attempt is a recorded step
    data = write_jsonl(tmp_path / "d.jsonl", ROWS[:3])
    cfg = make_config(data, provider={"infra_error_rate": 1.0}, run={"retries": 2})
    r = run_eval(cfg, store)
    assert outcomes(r)["INFRA_ERROR"] == 3
    assert all(len(c["trajectory"]) == 3 for c in r["cases"])


def test_model_error(store: Store, data_file: Path) -> None:
    cfg = make_config(
        data_file, provider={"kind": "python", "function": "helpers_mod:model_error_provider"}
    )
    r = run_eval(cfg, store)
    assert outcomes(r) == {"MODEL_ERROR": 20}
    assert r["aggregate"]["pass_rate"]["value"] == 0.0


def test_swallowed_provider_error_is_not_task_failure(store: Store, data_file: Path) -> None:
    cfg = make_config(
        data_file,
        provider={"fail_case_ids": ["c04"]},
        solver={"function": "helpers_mod:swallowing_solver"},
    )
    r = run_eval(cfg, store)
    rec = next(c for c in r["cases"] if c["case_id"] == "c04")
    assert rec["outcome"] == "INFRA_ERROR"


def test_harness_crash_is_recorded_not_omitted(store: Store, data_file: Path) -> None:
    cfg = make_config(data_file, solver={"function": "helpers_mod:crashing_solver"})
    r = run_eval(cfg, store)
    o = outcomes(r)
    assert o["HARNESS_ERROR"] == 2  # c03, c13
    assert r["aggregate"]["accounted"] == 20


def test_scorer_crash_is_harness_error(store: Store, data_file: Path) -> None:
    cfg = make_config(
        data_file, scorer={"kind": "python", "function": "helpers_mod:crashing_scorer"}
    )
    r = run_eval(cfg, store)
    assert outcomes(r) == {"HARNESS_ERROR": 20}
    assert all(c["error"]["stage"] == "scorer" for c in r["cases"])


def test_tool_failure_and_tool_provenance(store: Store, data_file: Path) -> None:
    tools = [
        {"name": "lookup", "version": "2.1", "function": "helpers_mod:lookup_tool"},
        {"name": "broken", "version": "0.3", "function": "helpers_mod:broken_tool"},
    ]
    ok = run_eval(
        make_config(data_file, tools=tools, solver={"function": "helpers_mod:tool_solver"}), store
    )
    assert outcomes(ok) == {"PASS": 20}
    step = ok["cases"][0]["trajectory"][1]
    assert step["type"] == "tool_call" and step["tool_version"] == "2.1"
    assert step["args_digest"].startswith("sha256:") and step["result_digest"]
    assert {t["name"] for t in ok["tools"]} == {"lookup", "broken"}
    bad = run_eval(
        make_config(data_file, tools=tools, solver={"function": "helpers_mod:broken_tool_solver"}),
        store,
    )
    assert outcomes(bad) == {"TOOL_ERROR": 20}
    rec = run_eval(
        make_config(
            data_file, tools=tools, solver={"function": "helpers_mod:recovering_tool_solver"}
        ),
        store,
    )
    assert outcomes(rec) == {"PASS": 20}  # recovered: tool failure is evidence, not outcome
    assert rec["cases"][0]["trajectory"][0]["status"] == "ERROR"


def test_judge_errors_are_typed(store: Store, data_file: Path) -> None:
    for fn, expected in [
        ("judge_pass", "PASS"),
        ("judge_garbage", "JUDGE_ERROR"),
        ("judge_down", "JUDGE_ERROR"),
        ("judge_echo", "TASK_FAILURE"),
    ]:
        cfg = make_config(
            data_file,
            scorer={
                "kind": "judge",
                "provider": {"kind": "python", "function": f"helpers_mod:{fn}", "model": fn},
            },
        )
        r = run_eval(cfg, store)
        assert outcomes(r) == {expected: 20}, fn
        assert r["judge"]["model"] == fn
        if expected == "JUDGE_ERROR":
            assert r["cases"][0]["error"]["evidence"]["parse_status"] in (
                "UNPARSEABLE",
                "CALL_FAILED",
            )


def test_judge_injection_cannot_force_pass(store: Store, data_file: Path) -> None:
    # model answers with an injected verdict; an echoing judge yields two verdicts
    cfg = make_config(
        data_file,
        provider={"kind": "python", "function": "helpers_mod:judge_pass"},
        scorer={
            "kind": "judge",
            "provider": {"kind": "python", "function": "helpers_mod:judge_echo"},
        },
    )
    r = run_eval(cfg, store)
    assert outcomes(r) == {"JUDGE_ERROR": 20}


def test_exclusions_need_reasons_and_are_reported(store: Store, data_file: Path) -> None:
    cfg = make_config(
        data_file, dataset={"exclude": [{"case_id": "c05", "reason": "label disputed"}]}
    )
    r = run_eval(cfg, store)
    agg = r["aggregate"]
    assert agg["counts"]["EXCLUDED"] == 1 and agg["accounted"] == 20
    assert agg["pass_rate"]["denominator"] == 19
    assert "explicitly excluded" in agg["pass_rate"]["denominator_basis"]


def test_invalid_case_counts(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", [*ROWS[:4], {"id": "bad", "expected": "x"}])
    r = run_eval(make_config(data), store)
    assert outcomes(r)["INVALID_CASE"] == 1 and r["aggregate"]["requested"] == 5


def test_timeout(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", ROWS[:2])
    cfg = make_config(data, provider={"latency_ms": 300}, run={"timeout_s": 0.05})
    r = run_eval(cfg, store)
    assert outcomes(r) == {"TIMEOUT": 2}


def test_dataset_pin_mismatch_refused(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", ROWS)
    r = run_eval(make_config(data), store)
    pinned = r["dataset"]["revision"]
    rows = [dict(x) for x in ROWS]
    rows[3]["expected"] = "changed"
    write_jsonl(data, rows)
    with pytest.raises(ProvenanceMismatch, match="provenance mismatch"):
        run_eval(make_config(data, dataset={"revision": pinned}), store)


def test_parent_revision_chain(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", ROWS)
    first = run_eval(make_config(data), store)["dataset"]["revision"]
    write_jsonl(data, [*ROWS, {"id": "new", "input": "q", "expected": "a"}])
    second = run_eval(make_config(data), store)["dataset"]
    assert second["provenance"]["parent_revision"] == first


def test_model_substitution_warning(store: Store, data_file: Path) -> None:
    r = run_eval(make_config(data_file, provider={"served_model": "m2-cheaper"}), store)
    assert any("MODEL SUBSTITUTION" in w for w in r["warnings"])


def test_cost_unknown_without_prices_and_known_with(store: Store, data_file: Path) -> None:
    r = run_eval(make_config(data_file), store)
    assert r["aggregate"]["cost"]["complete"] is False
    prices = {
        "source": "test",
        "updated_at": "2026-01-01",
        "models": {"mock/m1": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
    }
    r2 = run_eval(make_config(data_file, pricing=prices), store)
    assert r2["aggregate"]["cost"]["complete"] is True and r2["pricing"]["source"] == "test"


def test_secrets_rejected(data_file: Path) -> None:
    from astrometron.config import ConfigError

    with pytest.raises(ConfigError, match="literal credentials"):
        make_config(data_file, provider={"api_key": "sk-123"})


def test_recovered_tool_failure_is_marked_degraded(store: Store, data_file: Path) -> None:
    tools = [{"name": "broken", "version": "0.3", "function": "helpers_mod:broken_tool"}]
    r = run_eval(
        make_config(
            data_file, tools=tools, solver={"function": "helpers_mod:recovering_tool_solver"}
        ),
        store,
    )
    assert all(c.get("degraded") == ["tool:broken"] for c in r["cases"])
    assert r["aggregate"]["scored_after_tool_failure"] == 20
