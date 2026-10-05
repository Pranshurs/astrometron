"""Provider backed by a user Python function: ``kind: python, function: pkg.mod:fn``.

The function receives the :class:`Request` and returns a ``str`` or a :class:`Response`.
Its identity revision is the digest of the function's source, so editing the function
is visible in the receipt. It is pinned (deterministic) only with ``pinned: true``.
Raise :class:`ProviderError` to signal typed failures; any other exception is a
HARNESS_ERROR (the function is harness code).
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from ..canonical import digest_text
from .base import ProviderIdentity, Request, Response


def resolve_callable(spec: str) -> Callable[..., Any]:
    if ":" not in spec:
        raise ValueError(f"callable spec must be 'module:attr', got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    obj: Any = importlib.import_module(mod_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise ValueError(f"{spec} is not callable")
    return obj  # type: ignore[no-any-return]


def source_digest(obj: Any) -> str | None:
    try:
        return digest_text(inspect.getsource(obj))
    except (OSError, TypeError):
        return None


class PythonProvider:
    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = dict(options)
        self.spec = str(self.options["function"])
        self.fn = resolve_callable(self.spec)
        self.model = str(self.options.get("model", self.spec))
        # Source digest pins the code, not its behaviour: the function may call a remote
        # model or use randomness. Exactness is claimed only when the config says so.
        self.pinned = bool(self.options.get("pinned", False))
        self._revision = source_digest(self.fn)

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider="python",
            model=self.model,
            revision=self._revision,
            pinned=self.pinned and self._revision is not None,
        )

    def complete(self, request: Request) -> Response:
        out = self.fn(request)
        if isinstance(out, Response):
            return out
        if isinstance(out, str):
            return Response(text=out, served_model=self.model)
        raise TypeError(f"{self.spec} returned {type(out).__name__}, expected str or Response")
