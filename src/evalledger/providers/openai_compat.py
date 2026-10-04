"""OpenAI-compatible chat-completions provider (stdlib only).

Works with local servers (llama.cpp ``llama-server``, Ollama, LM Studio, vLLM) and with
hosted OpenAI-compatible APIs. The API key is read from the environment variable named
by ``api_key_env``; a literal key in config is rejected by config validation.

Such a model is **not pinned** unless the config supplies ``revision`` (e.g. a GGUF
file digest) *and* ``pinned: true``. Reproduction reports this honestly.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..taxonomy import ErrorKind
from .base import ProviderError, ProviderIdentity, Request, Response, Usage

_MODEL_SIDE_HINTS = ("context_length", "context length", "content_filter", "maximum context")


def _public_endpoint(url: str) -> str:
    """scheme://host[:port]/path with any userinfo, query and fragment removed."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


class OpenAICompatProvider:
    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = dict(options)
        self.base_url = str(self.options.get("base_url", "http://127.0.0.1:8080/v1")).rstrip("/")
        self.model = str(self.options["model"])
        self.revision = self.options.get("revision")
        self.pinned = bool(self.options.get("pinned", False)) and self.revision is not None
        self.timeout_s = float(self.options.get("request_timeout_s", 60))
        self.provider_name = str(self.options.get("name", "openai-compat"))
        env = self.options.get("api_key_env")
        self._api_key = os.environ.get(str(env)) if env else None
        self.extra_headers = dict(self.options.get("headers", {}))

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider=self.provider_name,
            model=self.model,
            revision=str(self.revision) if self.revision else None,
            pinned=self.pinned,
            endpoint=_public_endpoint(self.base_url),
        )

    def complete(self, request: Request) -> Response:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            **request.params,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if exc.code in (400, 413, 422) and any(h in detail.lower() for h in _MODEL_SIDE_HINTS):
                raise ProviderError(ErrorKind.MODEL, f"HTTP {exc.code}: {detail}") from None
            if exc.code in (400, 404, 413, 422):
                raise ProviderError(ErrorKind.MODEL, f"HTTP {exc.code}: {detail}") from None
            # 401/403 (auth), 408, 429 (rate limit), 5xx: availability, not capability.
            raise ProviderError(
                ErrorKind.INFRA,
                f"HTTP {exc.code}: {detail}",
                retryable=exc.code in (408, 429) or exc.code >= 500,
            ) from None
        except TimeoutError as exc:
            raise ProviderError(ErrorKind.TIMEOUT, f"request timed out: {exc}") from None
        except (urllib.error.URLError, ConnectionError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(
                ErrorKind.INFRA, f"connection failed: {exc}", retryable=True
            ) from None
        latency = (time.perf_counter() - start) * 1000.0
        try:
            data = json.loads(payload)
        except ValueError:
            # Not an API response at all (e.g. a proxy's HTML error page): availability.
            raise ProviderError(
                ErrorKind.INFRA, "non-JSON response from endpoint", retryable=True
            ) from None
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(ErrorKind.MODEL, f"malformed provider response: {exc}") from None
        if choice.get("finish_reason") == "content_filter":
            raise ProviderError(ErrorKind.MODEL, "response blocked by content filter")
        tool_calls = tuple(message.get("tool_calls") or ())
        if text is None and not tool_calls:
            raise ProviderError(ErrorKind.MODEL, "provider returned no content")
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return Response(
            text=text or "",
            usage=Usage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                reported_cost_usd=float(cost)
                if isinstance(cost, (int, float)) and math.isfinite(cost)
                else None,
            ),
            served_model=data.get("model"),
            latency_ms=latency,
            tool_calls=tool_calls,
        )
