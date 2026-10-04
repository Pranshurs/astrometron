"""Canonical serialization and content digests.

Every digest in EvalLedger is ``sha256:<hex>`` over the *canonical JSON* form of a
value: UTF-8, object keys sorted, no insignificant whitespace, no NaN/Infinity.
Two values with the same canonical form are the same evidence; anything else is not.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

DIGEST_PREFIX = "sha256:"


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented canonically (e.g. NaN, sets, bytes)."""


def _check(value: Any, path: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalizationError(f"non-finite float at {path or '<root>'}")
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key {key!r} at {path or '<root>'}")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalizationError(f"unsupported type {type(value).__name__} at {path or '<root>'}")


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical JSON encoding of ``value``."""
    _check(value, "")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return DIGEST_PREFIX + hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """Content digest of ``value``'s canonical form."""
    return digest_bytes(canonical_bytes(value))


def digest_text(text: str) -> str:
    """Digest of a raw string (not JSON-encoded); used for prompts and source code."""
    return digest_bytes(text.encode("utf-8"))


def is_digest(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith(DIGEST_PREFIX):
        return False
    hexpart = value[len(DIGEST_PREFIX) :]
    return len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart)
