import json
import logging
from pathlib import Path

import pytest

from astrometron import log
from astrometron.runner import run_eval
from astrometron.store import Store
from conftest import make_config


def test_logs_are_json_and_leak_nothing(
    store: Store,
    data_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_TEST_KEY", "sk-FAKE-synthetic-test-value-0000")
    logger = log.get_logger()
    logger.setLevel(logging.DEBUG)
    try:
        run_eval(make_config(data_file, provider={"fail_case_ids": ["c03"]}), store)
        log.event(
            "probe",
            api_key="sk-FAKE-synthetic-test-value-0000",
            note="Authorization: Bearer abc.def.ghi",
        )
    finally:
        logger.setLevel(logging.WARNING)
    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    names = [e["event"] for e in events]
    assert "run_started" in names and "run_finished" in names and "case_not_scored" in names
    assert "sk-FAKE-synthetic" not in err and "abc.def.ghi" not in err
    assert "question 3" not in err and "answer 3" not in err  # no dataset content


def test_redact() -> None:
    assert log.redact({"headers": {"Authorization": "x"}}) == {
        "headers": {"Authorization": "[REDACTED]"}
    }
    assert log.redact("key sk-FAKE-synthetic") == "key [REDACTED]"
