"""Regression tests for findings of the pre-publication review (2026-10-04)."""

import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path

import pytest

from astrometron.dataset import Case
from astrometron.receipt import seal, verify_receipt
from astrometron.router import route, validate_policy
from astrometron.runner import run_eval
from astrometron.scorers import NumericTolerance
from astrometron.store import Store, StoreError
from conftest import ROWS, make_config


def test_router_requires_receipt_task_to_match(store: Store, data_file: Path) -> None:
    import sqlite3

    r = run_eval(make_config(data_file), store)
    con = sqlite3.connect(store.db_path)
    con.execute("UPDATE runs SET task='medical' WHERE run_id=?", (r["run_id"],))
    con.commit()
    con.close()
    pol = validate_policy(
        {
            "task": "medical",
            "requirements": {"min_success_rate": 0.0},
            "candidates": [{"kind": "mock", "model": "m1", "accuracy": 1.0}],
        }
    )
    d = route(store, pol)
    assert d["status"] == "NO_ELIGIBLE_MODEL"
    assert d["skipped_invalid_evidence"][0]["run_id"] == r["run_id"]


def test_default_evidence_age_applies(store: Store, data_file: Path) -> None:
    run_eval(make_config(data_file), store)
    pol = validate_policy(
        {
            "task": "qa",
            "requirements": {"min_success_rate": 0.0},
            "candidates": [{"kind": "mock", "model": "m1", "accuracy": 1.0}],
        }
    )
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert route(store, pol, now=later)["status"] == "ROUTING_EVIDENCE_STALE"
    pol_off = validate_policy(
        {**pol, "requirements": {"min_success_rate": 0.0, "max_evidence_age_days": None}}
    )
    assert route(store, pol_off, now=later)["status"] == "SELECTED"


def test_router_rejects_evidence_from_other_endpoint(store: Store, data_file: Path) -> None:
    from test_providers import BEHAVIOUR, Handler

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        BEHAVIOUR.update(mode="ok", served="x")
        run_eval(
            make_config(
                data_file, provider={"kind": "openai-compat", "base_url": url, "model": "x"}
            ),
            store,
        )
    finally:
        srv.shutdown()
    cand = {"kind": "openai-compat", "base_url": "https://elsewhere.example/v1", "model": "x"}
    pol = validate_policy(
        {"task": "qa", "requirements": {"min_success_rate": 0.0}, "candidates": [cand]}
    )
    d = route(store, pol)
    assert d["candidates"][0]["status"] == "DIFFERENT_DEPLOYMENT"


def test_numeric_scorer_is_strict() -> None:
    s = NumericTolerance({})
    case = Case("c", "q", "10")
    assert s.score("10", case).passed and s.score(" 1e1 ", case).passed
    for junk in ("1_0", "\u0661\u0660", "inf", "nan", "10 apples"):
        assert not s.score(junk, case).passed, junk


def test_run_prefix_is_literal(store: Store, data_file: Path) -> None:
    r = run_eval(make_config(data_file), store)
    assert store.resolve(r["run_id"][:8]) == r["run_id"]
    with pytest.raises(StoreError):
        store.resolve("%")
    with pytest.raises(StoreError):
        store.resolve("_" * 4)


def test_tool_failure_without_later_model_call_is_rejected_by_verifier(
    store: Store, data_file: Path
) -> None:
    tools = [{"name": "broken", "version": "1", "function": "helpers_mod:broken_tool"}]
    r = run_eval(
        make_config(
            data_file, tools=tools, solver={"function": "helpers_mod:recovering_tool_solver"}
        ),
        store,
    )
    assert verify_receipt(r).ok  # tool failed, then the model answered: recovered
    steps = r["cases"][0]["trajectory"]
    forged = dict(r)
    forged["cases"] = [dict(r["cases"][0], trajectory=[steps[1], steps[0]]), *r["cases"][1:]]
    assert any(
        "tool failure counted as a model verdict" in p
        for p in verify_receipt(seal(forged)).problems
    )


def test_reproduce_repeats(store: Store, data_file: Path) -> None:
    from astrometron.reproduce import REPRODUCED, reproduce

    r = run_eval(make_config(data_file, provider={"accuracy": 0.6}), store)
    rep = reproduce(store, r["run_id"], repeats=3)
    assert rep.verdict == REPRODUCED and rep.agreement["repeats"] == 3


def test_rows_fixture_unchanged() -> None:
    assert len(ROWS) == 20


def test_python_provider_unpinned_unless_declared() -> None:
    from astrometron.providers import PythonProvider

    spec = {"function": "helpers_mod:echo_expected"}
    assert not PythonProvider(spec).identity().pinned
    assert PythonProvider({**spec, "pinned": True}).identity().pinned


def test_reproduce_flags_served_model_change(store: Store, data_file: Path, tmp_path: Path) -> None:
    import importlib
    import sys

    from astrometron.reproduce import INPUT_MISMATCH, reproduce

    mod = tmp_path / "served.py"
    mod.write_text(
        "from astrometron.providers import Response\nSERVED = ['v1']\n"
        "def f(req):\n    return Response(text='x', served_model=SERVED[0])\n"
    )
    cfg = make_config(
        data_file,
        provider={"kind": "python", "function": "served:f", "model": "v1", "pinned": True},
        python_path=[str(tmp_path)],
    )
    r = run_eval(cfg, store)
    sys.modules["served"].SERVED[0] = "v2-quantized"  # same code, provider now serves another
    rep = reproduce(store, r["run_id"])
    assert rep.verdict == INPUT_MISMATCH
    assert any("served" in m for m in rep.mismatches)
    importlib.reload(sys.modules["served"])
