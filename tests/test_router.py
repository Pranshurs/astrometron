import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from conftest import ROWS, make_config, write_jsonl
from evalledger.router import route, validate_policy
from evalledger.runner import run_eval
from evalledger.store import Store

PRICES = {
    "source": "test",
    "updated_at": "2026-01-01",
    "models": {
        "mock/cheap": {"input_per_mtok": 0.1, "output_per_mtok": 0.1},
        "mock/good": {"input_per_mtok": 1.0, "output_per_mtok": 1.0},
        "mock/slow": {"input_per_mtok": 0.5, "output_per_mtok": 0.5},
    },
}

CHEAP = {"kind": "mock", "model": "cheap", "accuracy": 0.6}
GOOD = {"kind": "mock", "model": "good", "accuracy": 1.0}
SLOW = {"kind": "mock", "model": "slow", "accuracy": 1.0, "latency_ms": 30}


def evaluate(store: Store, data: Path, prov: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return run_eval(
        make_config(data, provider=prov, pricing=kw.pop("pricing", PRICES), **kw), store
    )


def policy(cands: list[dict[str, Any]], **req: Any) -> dict[str, Any]:
    base = {"min_success_rate": 0.8, "quality_bound": "point", "min_cases": 10}
    return validate_policy({"task": "qa", "requirements": {**base, **req}, "candidates": cands})


def by(decision: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in decision["candidates"] if c["candidate"] == key)


def test_cheapest_fails_quality_floor(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, CHEAP)
    evaluate(store, data_file, GOOD)
    d = route(store, policy([CHEAP, GOOD]))
    assert d["status"] == "SELECTED" and d["selected"] == "mock/good"
    assert by(d, "mock/cheap")["status"] == "BELOW_QUALITY_FLOOR"
    ev = by(d, "mock/good")["evidence"]
    assert ev["run_id"] and ev["receipt_digest"].startswith("sha256:")


def test_best_model_exceeds_latency(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, SLOW)
    evaluate(store, data_file, GOOD)
    d = route(store, policy([SLOW, GOOD], max_p95_ms=20))
    assert by(d, "mock/slow")["status"] == "TOO_SLOW" and d["selected"] == "mock/good"


def test_provider_unavailable(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    d = route(store, policy([{**GOOD, "unavailable": True}]))
    assert d["status"] == "NO_ELIGIBLE_MODEL" and by(d, "mock/good")["status"] == "UNAVAILABLE"


def test_live_probe_unavailable(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    d = route(store, policy([{**GOOD, "probe": True, "infra_error_rate": 1.0}]))
    # changing the mock's options also changes its identity revision -> revision drift too
    assert "UNAVAILABLE" in by(d, "mock/good")["failed_gates"]


def test_stale_evidence(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    later = datetime.now(timezone.utc) + timedelta(days=45)
    d = route(store, policy([GOOD], max_evidence_age_days=30), now=later)
    assert d["status"] == "ROUTING_EVIDENCE_STALE"
    assert by(d, "mock/good")["status"] == "STALE_EVIDENCE"


def test_model_revision_changed(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    d = route(store, policy([{**GOOD, "accuracy": 0.99}]))  # same name, different model
    assert d["status"] == "ROUTING_EVIDENCE_STALE"
    assert by(d, "mock/good")["status"] == "MODEL_REVISION_CHANGED"


def test_incomparable_evidence(store: Store, data_file: Path, tmp_path: Path) -> None:
    evaluate(store, data_file, CHEAP)
    other = write_jsonl(tmp_path / "v2.jsonl", [*ROWS, {"id": "x", "input": "q", "expected": "a"}])
    evaluate(store, other, GOOD)  # newer evidence on a different revision becomes the basis
    d = route(store, policy([CHEAP, GOOD], min_success_rate=0.1))
    assert by(d, "mock/cheap")["status"] == "INCOMPARABLE_EVIDENCE"
    assert d["selected"] == "mock/good"


def test_no_candidate_satisfies(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, CHEAP)
    evaluate(store, data_file, SLOW)
    d = route(store, policy([CHEAP, SLOW], max_p95_ms=5))
    assert d["status"] == "NO_ELIGIBLE_MODEL" and d["selected"] is None


def test_cost_metadata_missing_fails_closed(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD, pricing=None)
    d = route(store, policy([GOOD], max_cost_per_case_usd=1.0))
    assert by(d, "mock/good")["status"] == "COST_UNKNOWN" and d["status"] == "NO_ELIGIBLE_MODEL"
    assert route(store, policy([GOOD]))["status"] == "SELECTED"  # no cost limit: fine


def test_too_expensive(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    d = route(store, policy([GOOD], max_cost_per_case_usd=1e-12))
    assert by(d, "mock/good")["status"] == "TOO_EXPENSIVE"


def test_insufficient_sample(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    d = route(store, policy([GOOD], min_cases=1000))
    assert by(d, "mock/good")["status"] == "INSUFFICIENT_SAMPLE"


def test_wilson_bound_is_stricter_than_point(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)  # 20/20 -> Wilson lower ~0.84
    assert route(store, policy([GOOD], min_success_rate=0.9))["status"] == "SELECTED"
    d = route(store, policy([GOOD], min_success_rate=0.9, quality_bound="wilson_lower"))
    assert d["status"] == "NO_ELIGIBLE_MODEL"


def test_unreliable_provider(store: Store, data_file: Path) -> None:
    flaky = {**GOOD, "infra_error_rate": 0.3}
    evaluate(store, data_file, flaky)
    d = route(store, policy([flaky], min_success_rate=0.1, max_error_rate=0.05))
    assert "UNRELIABLE" in by(d, "mock/good")["failed_gates"]


def test_tampered_evidence_is_ignored(store: Store, data_file: Path) -> None:
    r = evaluate(store, data_file, CHEAP)
    path = store.root / "runs" / r["run_id"] / "receipt.json"
    data = json.loads(path.read_text())
    for c in data["cases"]:
        c["outcome"] = "PASS"
    path.write_text(json.dumps(data))
    d = route(store, policy([CHEAP]))
    assert by(d, "mock/cheap")["status"] == "NO_EVIDENCE"
    assert d["skipped_invalid_evidence"][0]["run_id"] == r["run_id"]


def test_no_evidence(store: Store) -> None:
    d = route(store, policy([GOOD]))
    assert d["status"] == "NO_ELIGIBLE_MODEL" and by(d, "mock/good")["status"] == "NO_EVIDENCE"


def test_objectives_and_decision_recorded(store: Store, data_file: Path) -> None:
    evaluate(store, data_file, GOOD)
    evaluate(store, data_file, {**CHEAP, "accuracy": 0.95})
    cheapest = route(store, policy([GOOD, {**CHEAP, "accuracy": 0.95}], min_success_rate=0.5))
    best = route(
        store,
        validate_policy(
            {
                **policy([GOOD, {**CHEAP, "accuracy": 0.95}], min_success_rate=0.5),
                "objective": "best_quality",
            }
        ),
    )
    assert cheapest["selected"] == "mock/cheap" and best["selected"] == "mock/good"
    assert store.get_object(cheapest["decision_digest"])["decision_id"] == cheapest["decision_id"]


def test_degraded_evidence_limit(store: Store, data_file: Path) -> None:
    tools = [{"name": "broken", "version": "1", "function": "helpers_mod:broken_tool"}]
    run_eval(
        make_config(
            data_file,
            provider=GOOD,
            pricing=PRICES,
            tools=tools,
            solver={"function": "helpers_mod:recovering_tool_solver"},
        ),
        store,
    )
    assert route(store, policy([GOOD]))["status"] == "SELECTED"  # reported, not enforced
    d = route(store, policy([GOOD], max_degraded_fraction=0.1))
    assert by(d, "mock/good")["status"] == "DEGRADED_EVIDENCE"
    assert by(d, "mock/good")["evidence"]["scored_after_tool_failure"] == 20
