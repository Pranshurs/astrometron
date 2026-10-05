"""Structured logging: one JSON object per line on stderr.

Off by default (level WARNING). Enable with ``ASTROMETRON_LOG=info`` or ``debug``.
Logs carry ids, counts, outcomes and error *classes*. They never carry prompts, model
outputs, dataset contents or credentials. Any field whose name looks like a credential
is redacted, and so is any string value that looks like a bearer token or API key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

_LOGGER_NAME = "astrometron"
_SECRET_KEY = re.compile(r"(api[_-]?key|authorization|password|secret|token|bearer)", re.I)
_SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|[A-Za-z0-9_-]{32,})\b")
REDACTED = "[REDACTED]"


def redact(value: Any, key: str = "") -> Any:
    if key and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **redact(fields),
        }
        return json.dumps(payload, sort_keys=True, default=str)


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever ``sys.stderr`` is at emit time (not at creation time)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = _StderrHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False
        level = os.environ.get("ASTROMETRON_LOG", "warning").upper()
        logger.setLevel(getattr(logging, level, logging.WARNING))
    return logger


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log a structured event. Pass ids and counts, never content."""
    get_logger().log(level, name, extra={"fields": fields})
