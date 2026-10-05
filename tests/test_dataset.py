import json
from pathlib import Path

import pytest

from astrometron.dataset import Case, Dataset, DatasetError, load_file, load_rows
from conftest import ROWS, write_jsonl


def test_reordering_keeps_revision_and_case_identity(tmp_path: Path) -> None:
    a = load_file(write_jsonl(tmp_path / "a.jsonl", ROWS), "ds")
    b = load_file(write_jsonl(tmp_path / "b.jsonl", list(reversed(ROWS))), "ds")
    assert a.revision == b.revision
    assert a.case_ids == b.case_ids
    assert all(a.row_digest(c) == b.row_digest(c) for c in a.case_ids)


def test_any_mutation_changes_revision(tmp_path: Path) -> None:
    base = load_file(write_jsonl(tmp_path / "a.jsonl", ROWS), "ds").revision
    for field, value in [
        ("input", "tampered"),
        ("expected", "tampered"),
        ("tags", ["x"]),
        ("split", "dev"),
        ("metadata", {"k": 1}),
    ]:
        rows = [dict(r) for r in ROWS]
        rows[5][field] = value
        ds = load_file(write_jsonl(tmp_path / f"m-{field}.jsonl", rows), "ds")
        assert ds.revision != base, field


def test_provenance_is_not_part_of_revision(tmp_path: Path) -> None:
    a = load_file(write_jsonl(tmp_path / "a.jsonl", ROWS), "ds", description="one")
    b = load_file(write_jsonl(tmp_path / "b.jsonl", ROWS), "ds", description="two")
    assert a.revision == b.revision


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(DatasetError, match="duplicate"):
        Dataset("ds", [Case("x", "a"), Case("x", "b")])


def test_auto_ids_are_content_derived_and_order_independent() -> None:
    rows = [{"input": "alpha"}, {"input": "beta"}]
    a = load_rows("ds", rows)
    b = load_rows("ds", list(reversed(rows)))
    assert a.case_ids == b.case_ids and all(c.startswith("auto-") for c in a.case_ids)


def test_row_missing_input_is_kept_as_invalid() -> None:
    ds = load_rows("ds", [{"id": "a", "input": "x"}, {"id": "b", "expected": "y"}])
    assert len(ds) == 2
    assert ds.case("b").invalid_reason


def test_snapshot_roundtrip_and_tamper_detection() -> None:
    ds = load_rows("ds", ROWS)
    snap = ds.to_snapshot()
    assert Dataset.from_snapshot(snap).revision == ds.revision
    snap["rows"][0]["expected"] = "forged"
    with pytest.raises(DatasetError, match="revision mismatch"):
        Dataset.from_snapshot(snap)


def test_selection_is_deterministic_and_strict() -> None:
    ds = load_rows("ds", ROWS)
    assert ds.select(sample=5, seed=1) == ds.select(sample=5, seed=1)
    assert ds.select(sample=5, seed=1) != ds.select(sample=5, seed=2)
    assert ds.select(limit=3) == ["c00", "c01", "c02"]
    with pytest.raises(DatasetError, match="not in dataset"):
        ds.select(case_ids=["c00", "nope"])


def test_csv_loader(tmp_path: Path) -> None:
    p = tmp_path / "d.csv"
    p.write_text("id,input,expected,tags,source\n1,hi,there,a;b,web\n")
    ds = load_file(p)
    c = ds.case("1")
    assert c.tags == ("a", "b") and c.metadata == {"source": "web"}
    assert ds.provenance.format == "csv"


def test_invalid_json_line_reports_location(tmp_path: Path) -> None:
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps({"id": 1, "input": "x"}) + "\n{oops\n")
    with pytest.raises(DatasetError, match=":2:"):
        load_file(p)


def test_invalid_rows_do_not_depend_on_order_or_file_name(tmp_path: Path) -> None:
    rows = [{"id": "a", "input": "x"}, {"id": "b", "expected": "y"}]
    a = load_file(write_jsonl(tmp_path / "one.jsonl", rows), "ds")
    b = load_file(write_jsonl(tmp_path / "two.jsonl", list(reversed(rows))), "ds")
    assert a.revision == b.revision


def test_unmapped_reserved_columns_are_hashed(tmp_path: Path) -> None:
    fields = {"id": "ticket", "input": "text", "expected": "label"}
    base = {"ticket": "t1", "text": "hi", "label": "x", "id": "1", "input": "a", "expected": "b"}
    a = load_file(write_jsonl(tmp_path / "a.jsonl", [base]), "ds", fields=fields)
    b = load_file(
        write_jsonl(tmp_path / "b.jsonl", [{**base, "input": "CHANGED"}]), "ds", fields=fields
    )
    assert a.revision != b.revision
    assert a.case("t1").metadata == {"id": "1", "input": "a", "expected": "b"}
