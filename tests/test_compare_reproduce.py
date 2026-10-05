import json
import sqlite3
from pathlib import Path

import pytest

from astrometron.compare import NOT, PARTIAL, STRONG, compare
from astrometron.reproduce import (
    DIVERGED,
    INPUT_MISMATCH,
    NOT_EXACT,
    REFUSED,
    REPRODUCED,
    reproduce,
    staleness,
)
from astrometron.runner import run_eval
from astrometron.store import SCHEMA_VERSION, Store, StoreError, migrate
from conftest import ROWS, make_config, write_jsonl


def test_strong_comparison_model_change_only(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file, provider={"accuracy": 0.5}), store)
    b = run_eval(make_config(data_file, provider={"accuracy": 0.9, "model": "m2"}), store)
    res = compare(a, b)
    assert res["comparability"] == STRONG
    p = res["paired"]
    assert p["cases"] == 20
    assert p["improved"] + p["regressed"] + p["unchanged_pass"] + p["unchanged_fail"] == 20
    assert "model" in res["changed_treatments"]
    assert not any("single-factor" in w for w in res["warnings"])


def test_judge_change_is_partial_and_flagged(store: Store, data_file: Path) -> None:
    j1 = {
        "kind": "judge",
        "provider": {"kind": "python", "function": "helpers_mod:judge_pass", "model": "judge-a"},
    }
    j2 = {
        "kind": "judge",
        "provider": {"kind": "python", "function": "helpers_mod:judge_pass", "model": "judge-b"},
    }
    a = run_eval(make_config(data_file, scorer=j1), store)
    b = run_eval(make_config(data_file, scorer=j2, provider={"model": "m2"}), store)
    res = compare(a, b)
    assert res["comparability"] == PARTIAL
    assert "judge model changed" in res["reasons"]
    assert any("not a clean model-only comparison" in w for w in res["warnings"])


def test_scorer_change_is_partial(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file), store)
    b = run_eval(make_config(data_file, scorer={"kind": "normalized"}), store)
    assert compare(a, b)["comparability"] == PARTIAL


def test_different_dataset_not_comparable(store: Store, tmp_path: Path, data_file: Path) -> None:
    a = run_eval(make_config(data_file), store)
    other = write_jsonl(tmp_path / "o.jsonl", ROWS)
    b = run_eval(make_config(other, dataset={"id": "other"}), store)
    assert compare(a, b)["comparability"] == NOT


def test_dataset_revision_change_pairs_only_unchanged_rows(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", ROWS)
    a = run_eval(make_config(data), store)
    rows = [dict(r) for r in ROWS]
    rows[0]["expected"] = "edited"
    write_jsonl(data, rows)
    b = run_eval(make_config(data), store)
    res = compare(a, b)
    assert res["comparability"] == PARTIAL and res["paired"]["row_changed"] == 1
    assert res["paired"]["cases"] == 19


def test_tampered_receipt_not_comparable(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file), store)
    b = json.loads(json.dumps(a))
    b["aggregate"]["counts"]["PASS"] = 19
    assert compare(a, b)["comparability"] == NOT


def test_deterministic_rerun_reproduces(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file, provider={"accuracy": 0.7, "infra_error_rate": 0.1}), store)
    rep = reproduce(store, a["run_id"])
    assert rep.verdict == REPRODUCED, rep.to_dict()
    assert rep.agreement["identical"] == 20


def test_unpinned_provider_is_not_exact(store: Store, data_file: Path) -> None:
    a = run_eval(
        make_config(
            data_file,
            provider={"kind": "python", "pinned": False, "function": "helpers_mod:echo_expected"},
        ),
        store,
    )
    rep = reproduce(store, a["run_id"])
    assert rep.verdict == NOT_EXACT
    assert any("EXACT REPRODUCTION NOT POSSIBLE" in n for n in rep.notes)


def test_changed_scorer_code_is_input_mismatch(
    store: Store, data_file: Path, tmp_path: Path
) -> None:
    mod = tmp_path / "myscorer.py"
    mod.write_text("def s(output, case):\n    return output == case.expected\n")
    cfg = make_config(
        data_file, scorer={"kind": "python", "function": "myscorer:s"}, python_path=[str(tmp_path)]
    )
    a = run_eval(cfg, store)
    mod.write_text("def s(output, case):\n    return True  # lenient\n")
    import importlib
    import sys

    importlib.reload(sys.modules["myscorer"])
    rep = reproduce(store, a["run_id"])
    assert rep.verdict == INPUT_MISMATCH
    assert any("scorer" in m for m in rep.mismatches)


def test_hidden_nondeterminism_diverges(store: Store, data_file: Path, tmp_path: Path) -> None:
    mod = tmp_path / "flaky.py"
    mod.write_text(
        "import itertools\nC = itertools.count()\n"
        "def f(req):\n    return 'answer 1' if next(C) >= 20 else 'nope'\n"
    )
    cfg = make_config(
        data_file,
        provider={"kind": "python", "function": "flaky:f", "pinned": True},
        python_path=[str(tmp_path)],
        run={"concurrency": 1},
    )
    a = run_eval(cfg, store)
    rep = reproduce(store, a["run_id"])
    assert rep.verdict == DIVERGED


def test_tampered_receipt_refused(store: Store, data_file: Path) -> None:
    a = run_eval(make_config(data_file), store)
    path = store.root / "runs" / a["run_id"] / "receipt.json"
    data = json.loads(path.read_text())
    data["provider"]["identity"]["model"] = "gpt-big"
    path.write_text(json.dumps(data))
    assert not store.verify_run(a["run_id"]).ok
    assert reproduce(store, a["run_id"]).verdict == REFUSED


def test_resealed_forgery_caught_by_index(store: Store, data_file: Path) -> None:
    from astrometron.receipt import seal

    a = run_eval(make_config(data_file), store)
    path = store.root / "runs" / a["run_id"] / "receipt.json"
    data = json.loads(path.read_text())
    data["provider"]["identity"]["model"] = "gpt-big"
    forged = seal(data)  # attacker recomputes every digest in the file
    path.write_text(json.dumps(forged))
    v = store.verify_run(a["run_id"])
    assert not v.ok and any("indexed" in p for p in v.problems)


def test_staleness(store: Store, tmp_path: Path) -> None:
    data = write_jsonl(tmp_path / "d.jsonl", ROWS)
    cfg_path = tmp_path / "eval.yaml"
    cfg_path.write_text(
        "dataset: {path: d.jsonl, id: ds}\nprovider: {kind: mock, model: m}\n"
        "scorer: {kind: exact}\n"
    )
    from astrometron.config import load_config

    r = run_eval(load_config(cfg_path), store)
    assert staleness(r) == []
    cfg_path.write_text(cfg_path.read_text() + "run: {seed: 1}\n")
    assert any("config file" in s for s in staleness(r))
    write_jsonl(data, ROWS[:5])
    assert any("dataset source" in s for s in staleness(r))


def test_crashed_run_is_never_complete(store: Store, data_file: Path) -> None:
    store.begin_run(
        "01CRASHED",
        {
            "started_at": "2026-01-01T00:00:00Z",
            "dataset_id": "ds",
            "dataset_revision": "sha256:" + "0" * 64,
            "provider": "mock",
            "model": "m",
        },
    )
    v = store.verify_run("01CRASHED")
    assert not v.ok and "never finished" in v.problems[0]
    assert not store.is_trustworthy("01CRASHED")
    with pytest.raises(StoreError, match="did not finish"):
        store.load_receipt("01CRASHED")


def test_store_migrations(tmp_path: Path) -> None:
    db = tmp_path / "x.sqlite"
    con = sqlite3.connect(db)
    assert migrate(con) == SCHEMA_VERSION
    assert migrate(con) == SCHEMA_VERSION  # idempotent
    con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    con.commit()
    with pytest.raises(StoreError, match="newer"):
        migrate(con)
    con.close()
    foreign = sqlite3.connect(tmp_path / "f.sqlite")
    foreign.execute("CREATE TABLE t(x)")
    with pytest.raises(StoreError, match="no schema metadata"):
        migrate(foreign)


def test_object_corruption_detected(store: Store) -> None:
    d = store.put_object({"a": 1})
    path = store._object_path(d)
    path.write_text('{"a":2}')
    with pytest.raises(StoreError, match="corrupted"):
        store.get_object(d)


def test_v1_store_migrates_to_v2_preserving_runs(tmp_path: Path, data_file: Path) -> None:
    from astrometron.store import _migrate_0_to_1

    root = tmp_path / ".astrometron"
    root.mkdir()
    con = sqlite3.connect(root / "index.sqlite")
    _migrate_0_to_1(con)
    con.execute("INSERT INTO meta VALUES('schema_version','1')")
    con.execute(
        "INSERT INTO runs(run_id,status,started_at,dataset_id,dataset_revision,provider,"
        "model,pinned) VALUES('01OLD','COMPLETE','2026-01-01','ds','r','mock','m',1)"
    )
    con.commit()
    con.close()
    st = Store(root)
    assert st.run_row("01OLD")["model"] == "m"
    con = sqlite3.connect(root / "index.sqlite")
    assert con.execute("SELECT value FROM meta").fetchone()[0] == str(SCHEMA_VERSION) == "2"
    assert con.execute("SELECT count(*) FROM routing_decisions").fetchone()[0] == 0
