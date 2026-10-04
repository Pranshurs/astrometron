import pytest

from evalledger.integrity import SCENARIOS, run_suite


@pytest.mark.parametrize("sid", [s[0] for s in SCENARIOS])
def test_scenario(sid: str) -> None:
    (res,) = run_suite([sid])
    assert res["honest_ok"], f"{sid} control arm failed: {res['evidence']}"
    assert res["detected"], f"{sid} attack not detected: {res['evidence']}"


def test_suite_size() -> None:
    assert len(SCENARIOS) >= 41
    assert len({s[0] for s in SCENARIOS}) == len(SCENARIOS)
