import json
import shutil
from pathlib import Path

import pytest

from evalledger.cli import main

EXAMPLE = Path(__file__).parent.parent / "examples" / "support-routing"


@pytest.fixture
def example(tmp_path: Path) -> Path:
    dst = tmp_path / "ex"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns(".evalledger", "__pycache__"))
    return dst


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_end_to_end(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    st = str(example / ".evalledger")
    code, out = run(capsys, "--store", st, "run", str(example / "eval.yaml"))
    assert code == 0 and "No rows omitted." in out and "TOTAL" in out
    code, out = run(capsys, "--store", st, "run", str(example / "keyword.yaml"), "--json")
    b = json.loads(out)["run_id"]
    code, out = run(capsys, "--store", st, "runs", "--json")
    a = json.loads(out)[0]["run_id"]
    code, out = run(capsys, "--store", st, "compare", a, b)
    assert code == 0 and "STRONGLY_COMPARABLE" in out
    code, out = run(capsys, "--store", st, "reproduce", a)
    assert code == 0 and "Verdict: REPRODUCED" in out
    code, out = run(capsys, "--store", st, "show", a[:10], "--cases", "failed")
    assert code == 0 and "INFRA_ERROR" in out and "Integrity: VALID" in out
    code, out = run(capsys, "--store", st, "verify", a)
    assert code == 0 and "VALID" in out
    code, out = run(capsys, "--store", st, "datasets", "verify", "support-routing-v3")
    assert code == 0
    # mutate the source: verify flags it, run config is now stale
    data = example / "support-tickets.jsonl"
    data.write_text(data.read_text().replace('"billing"', '"technical"', 1))
    code, out = run(capsys, "--store", st, "datasets", "verify", "support-routing-v3")
    assert code == 1 and "CHANGED" in out
    code, out = run(capsys, "--store", st, "verify", a)
    assert code == 1 and "STALE" in out


def test_config_errors_exit_2(example: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = example / "bad.yaml"
    bad.write_text(
        "dataset: {path: x.jsonl}\nprovider: {kind: mock, api_key: sk-live}\n"
        "scorer: {kind: exact}\n"
    )
    assert main(["--store", str(example / ".s"), "run", str(bad)]) == 2
    assert "literal credentials" in capsys.readouterr().err
