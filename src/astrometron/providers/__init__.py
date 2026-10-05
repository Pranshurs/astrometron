"""Model providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .base import (
    Message,
    ModelProvider,
    ProviderError,
    ProviderIdentity,
    Request,
    Response,
    Usage,
)
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider
from .python_callable import PythonProvider

__all__ = [
    "Message",
    "MockProvider",
    "ModelProvider",
    "OpenAICompatProvider",
    "ProviderError",
    "ProviderIdentity",
    "PythonProvider",
    "Request",
    "Response",
    "Usage",
    "build_provider",
]


def build_provider(options: Mapping[str, Any]) -> ModelProvider:
    kind = options.get("kind")
    if kind == "mock":
        return MockProvider(options)
    if kind in ("openai-compat", "openai_compat", "local"):
        return OpenAICompatProvider(options)
    if kind == "python":
        return PythonProvider(options)
    raise ValueError(f"unknown provider kind {kind!r} (expected mock, openai-compat, python)")
