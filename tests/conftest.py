from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evalledger.config import EvalConfig, from_dict
from evalledger.store import Store

HERE = Path(__file__).parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


ROWS = [{"id": f"c{i:02d}", "input": f"question {i}", "expected": f"answer {i}"} for i in range(20)]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / ".evalledger")


@pytest.fixture
def data_file(tmp_path: Path) -> Path:
    return write_jsonl(tmp_path / "data.jsonl", ROWS)


def make_config(data_file: Path, **overrides: Any) -> EvalConfig:
    raw: dict[str, Any] = {
        "name": "t",
        "task": "qa",
        "dataset": {"path": str(data_file), "id": "ds"},
        "provider": {"kind": "mock", "model": "m1", "accuracy": 1.0},
        "scorer": {"kind": "exact"},
        "run": {"seed": 0, "concurrency": 2},
        "python_path": [str(HERE)],
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k] = {**raw[k], **v}
        else:
            raw[k] = v
    return from_dict(raw, data_file.parent)
