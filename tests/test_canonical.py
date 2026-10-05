import math

import pytest

from astrometron.canonical import CanonicalizationError, canonical_bytes, digest, is_digest


def test_key_order_does_not_matter() -> None:
    assert digest({"a": 1, "b": [1, 2]}) == digest({"b": [1, 2], "a": 1})


def test_value_change_changes_digest() -> None:
    assert digest({"a": 1}) != digest({"a": 2})
    assert digest("1") != digest(1)


def test_rejects_non_finite_and_unsupported() -> None:
    for bad in (math.nan, math.inf, {1: "x"}, {"s": {1, 2}}, b"bytes"):
        with pytest.raises(CanonicalizationError):
            canonical_bytes(bad)


def test_unicode_is_stable() -> None:
    assert canonical_bytes({"k": "é"}) == '{"k":"é"}'.encode()
    assert is_digest(digest("x"))
    assert not is_digest("sha256:xyz")
