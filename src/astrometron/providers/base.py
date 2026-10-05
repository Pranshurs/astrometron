"""Provider interface: the only way an evaluation talks to a model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..canonical import digest
from ..taxonomy import ErrorKind


@dataclass(frozen=True)
class ProviderIdentity:
    """Who answered. ``pinned`` is True only when ``revision`` names immutable weights or
    deterministic code, so a later call with the same request must give the same answer."""

    provider: str
    model: str
    revision: str | None = None
    pinned: bool = False
    #: Where the model is served (credential-free URL), for network providers.
    endpoint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "revision": self.revision,
            "pinned": self.pinned,
            "endpoint": self.endpoint,
        }

    @property
    def key(self) -> str:
        """Stable candidate key used by comparison and routing: ``provider/model``."""
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Request:
    messages: tuple[Message, ...]
    params: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    #: Opaque hint for deterministic providers (the case id). Never sent to real APIs.
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "params": self.params,
            "seed": self.seed,
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Cost reported by the provider itself (e.g. OpenRouter), in USD.
    reported_cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reported_cost_usd": self.reported_cost_usd,
        }


@dataclass(frozen=True)
class Response:
    text: str
    usage: Usage = field(default_factory=Usage)
    #: Model name the provider says served the request, if it says.
    served_model: str | None = None
    #: Pure provider latency, measured by the provider adapter.
    latency_ms: float | None = None
    #: Structured tool-call requests, for agent solvers.
    tool_calls: tuple[dict[str, Any], ...] = ()


class ProviderError(Exception):
    """A typed provider failure. ``kind`` decides the outcome; never a TASK_FAILURE."""

    def __init__(self, kind: ErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@runtime_checkable
class ModelProvider(Protocol):
    def identity(self) -> ProviderIdentity: ...

    def complete(self, request: Request) -> Response: ...
