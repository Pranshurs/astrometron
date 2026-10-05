import copy
from pathlib import Path
from typing import Any

import pytest

from astrometron.canonical import digest
from astrometron.config import ConfigError
from astrometron.cost import PriceTable, case_cost
from astrometron.receipt import aggregate, percentile, seal, verify_receipt, wilson
from astrometron.runner import run_eval
from astrometron.store import Store
from conftest import make_config


def reseal(r: dict[str, Any]) -> dict[str, Any]:
    r["aggregate"] = aggregate(
        r["cases"],
        r["dataset"]["selected_case_ids"],
        [e["case_id"] for e in r["dataset"]["excluded"]],
    )
    return seal(r)


@pytest.fixture
def receipt(store: Store, data_file: Path) -> dict[str, Any]:
    return run_eval(make_config(data_file, provider={"fail_case_ids": ["c02"]}), store)


def test_selection_digest_binds_selected_ids(receipt: dict[str, Any]) -> None:
    f = copy.deepcopy(receipt)
    f["dataset"]["selected_case_ids"] = f["dataset"]["selected_case_ids"][:-1]
    f["cases"] = f["cases"][:-1]
    v = verify_receipt(reseal(f))
    assert any("do not match selected_digest" in p for p in v.problems)


def test_error_outcome_needs_error_record(receipt: dict[str, Any]) -> None:
    f = copy.deepcopy(receipt)
    c = next(c for c in f["cases"] if c["outcome"] == "INFRA_ERROR")
    c["error"] = None
    v = verify_receipt(reseal(f))
    assert any("INFRA_ERROR without an error record" in p for p in v.problems)


def test_error_outcome_cannot_carry_score(receipt: dict[str, Any]) -> None:
    f = copy.deepcopy(receipt)
    c = next(c for c in f["cases"] if c["outcome"] == "INFRA_ERROR")
    c["score"] = {"passed": False, "value": 0.0}
    assert any("carries a score" in p for p in verify_receipt(reseal(f)).problems)


def test_unknown_outcome_rejected(receipt: dict[str, Any]) -> None:
    f = copy.deepcopy(receipt)
    f["cases"][0]["outcome"] = "SORT_OF_PASS"
    assert any("unknown outcome" in p for p in verify_receipt(reseal(f)).problems)


def test_exclusion_rules(receipt: dict[str, Any]) -> None:
    f = copy.deepcopy(receipt)
    f["cases"][0]["outcome"] = "EXCLUDED"
    assert any(
        "EXCLUDED without a configured exclusion" in p for p in verify_receipt(reseal(f)).problems
    )


def test_inputs_digest_identifies_the_experiment(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file), store)
    same = run_eval(make_config(data_file), store)
    assert a["inputs_digest"] == same["inputs_digest"]
    assert a["receipt_digest"] != same["receipt_digest"]  # different run, same experiment
    for change in (
        {"scorer": {"kind": "normalized"}},
        {"provider": {"model": "m2"}},
        {"prompt": {"template": "Q: {input}"}},
        {"run": {"seed": 9}},
        {"params": {"temperature": 0.7}},
    ):
        assert (
            run_eval(make_config(data_file, **change), store)["inputs_digest"]
            != (a["inputs_digest"])
        ), change


def test_exclusion_without_reason_rejected_by_config(data_file: Path) -> None:
    with pytest.raises(ConfigError, match="non-empty reason"):
        make_config(data_file, dataset={"exclude": [{"case_id": "c01"}]})
    with pytest.raises(ConfigError, match="non-empty reason"):
        make_config(data_file, dataset={"exclude": [{"case_id": "c01", "reason": ""}]})


def test_cost_never_invented() -> None:
    table = PriceTable("t", "2026-01-01", {"p/m": {"input_per_mtok": 2.0, "output_per_mtok": 4.0}})
    assert case_cost("p/m", [{"input_tokens": 1_000_000, "output_tokens": 500_000}], table) == (
        4.0,
        "price_table",
    )
    assert case_cost("p/m", [{"input_tokens": None, "output_tokens": 5}], table) == (
        None,
        "UNKNOWN",
    )
    assert case_cost("p/other", [{"input_tokens": 1, "output_tokens": 1}], table) == (
        None,
        "UNKNOWN",
    )
    assert case_cost("p/m", [{"reported_cost_usd": 0.25}], None) == (0.25, "provider")
    assert case_cost("p/m", [{"input_tokens": 1, "output_tokens": 1}], None) == (None, "UNKNOWN")


def test_statistics() -> None:
    assert percentile([], 50) is None
    assert percentile([1, 2, 3, 4], 50) == 2 and percentile([1, 2, 3, 4], 100) == 4
    lo, hi = wilson(84, 100) or (0, 0)
    assert 0.75 < lo < 0.84 < hi < 0.91
    assert wilson(0, 0) is None


def test_digest_is_canonical(receipt: dict[str, Any]) -> None:
    body = {k: v for k, v in receipt.items() if k != "receipt_digest"}
    assert digest(body) == receipt["receipt_digest"]
