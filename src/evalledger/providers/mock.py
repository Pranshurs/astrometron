"""Deterministic mock provider with fault injection.

Behaviour is a pure function of ``(config, request.seed, case_id)``, so a mock run is
exactly reproducible. It needs an *answer key* (case_id -> expected) to answer
correctly; the runner binds the dataset's expected values automatically.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Any

from ..canonical import canonical_bytes, digest
from ..taxonomy import ErrorKind
from .base import ProviderError, ProviderIdentity, Request, Response, Usage

MOCK_VERSION = "1"


def _uniform(*parts: object) -> float:
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


class MockProvider:
    """``kind: mock`` provider.

    Options: ``model``, ``accuracy``, ``infra_error_rate``, ``model_error_rate``,
    ``fail_case_ids`` (forced INFRA errors), ``latency_ms`` (simulated sleep),
    ``served_model`` (pretend a different model answered), ``tokens_per_char``.
    """

    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = dict(options)
        self.model = str(self.options.get("model", "mock-v1"))
        self.accuracy = float(self.options.get("accuracy", 1.0))
        self.infra_rate = float(self.options.get("infra_error_rate", 0.0))
        self.model_rate = float(self.options.get("model_error_rate", 0.0))
        self.fail_ids = set(self.options.get("fail_case_ids", ()))
        self.latency_ms = float(self.options.get("latency_ms", 0.0))
        self.served_model = self.options.get("served_model")
        self._answers: dict[str, Any] = {}

    def bind_answer_key(self, answers: Mapping[str, Any]) -> None:
        self._answers = dict(answers)

    def identity(self) -> ProviderIdentity:
        public = {k: v for k, v in sorted(self.options.items()) if k != "kind"}
        return ProviderIdentity(
            provider="mock",
            model=self.model,
            revision=digest({"mock_version": MOCK_VERSION, "options": public})[:23],
            pinned=True,
        )

    def complete(self, request: Request) -> Response:
        start = time.perf_counter()
        cid = request.case_id or digest(request.to_dict())
        seed = request.seed if request.seed is not None else 0
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        if cid in self.fail_ids or _uniform(self.model, seed, cid, "infra") < self.infra_rate:
            raise ProviderError(ErrorKind.INFRA, "mock: provider unavailable (503)", retryable=True)
        if _uniform(self.model, seed, cid, "model") < self.model_rate:
            raise ProviderError(ErrorKind.MODEL, "mock: context length exceeded")
        expected = self._answers.get(cid)
        if expected is not None and _uniform(self.model, seed, cid, "acc") < self.accuracy:
            text = expected if isinstance(expected, str) else canonical_bytes(expected).decode()
        else:
            text = "wrong-" + digest([self.model, seed, cid])[7:15]
        prompt_chars = sum(len(m.content) for m in request.messages)
        return Response(
            text=text,
            usage=Usage(
                input_tokens=max(1, prompt_chars // 4), output_tokens=max(1, len(text) // 4)
            ),
            served_model=str(self.served_model) if self.served_model else self.model,
            latency_ms=(time.perf_counter() - start) * 1000.0,
        )
