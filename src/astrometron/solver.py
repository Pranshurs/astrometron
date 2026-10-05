"""Solvers and trajectory evidence.

A solver turns a case into an answer using a :class:`SolverContext`. The context is
the *only* route to the model and to tools, and it records every call as a trajectory
step: request/response digests, served model, usage, latency, retries, tool identity,
tool version, argument digest, result digest, status and failure class.

This is deliberately not an agent framework: it records the evidence needed to
attribute and reproduce an agent's trajectory and nothing more.

Attribution rules applied by the runner after a solver returns:

* the last model call failed and was never followed by a success -> the case takes
  that failure's outcome (MODEL_ERROR / INFRA_ERROR / TIMEOUT), even if the solver
  swallowed the exception and returned some text. A provider outage cannot be laundered
  into a TASK_FAILURE.
* an uncaught :class:`ToolFailure` -> TOOL_ERROR.
* any other uncaught exception -> HARNESS_ERROR.
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_bytes, digest, digest_text
from .dataset import Case
from .providers import Message, ModelProvider, ProviderError, Request, Response
from .taxonomy import ErrorKind


class ToolFailure(Exception):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"tool {tool!r} failed: {message}")
        self.tool = tool


class UnknownTool(ToolFailure):
    """The solver asked for a tool that is not registered (a harness/solver defect)."""


class TemplateError(Exception):
    """The prompt template could not be rendered for this case."""


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    fn: Callable[..., Any]
    description: str = ""

    def identity(self) -> dict[str, Any]:
        try:
            code = digest_text(inspect.getsource(self.fn))
        except (OSError, TypeError):
            code = None
        return {"name": self.name, "version": self.version, "code_digest": code}


@dataclass(frozen=True)
class PromptTemplate:
    template: str = "{input}"
    system: str | None = None

    @property
    def digest(self) -> str:
        return digest({"template": self.template, "system": self.system})

    def render(self, case: Case) -> tuple[Message, ...]:
        values: dict[str, Any] = {}
        if isinstance(case.input, Mapping):
            values.update({k: _as_text(v) for k, v in case.input.items()})
        values["input"] = _as_text(case.input)
        try:
            user = self.template.format_map(values)
        except (KeyError, IndexError, ValueError) as exc:
            raise TemplateError(f"template cannot render case {case.case_id}: {exc!r}") from exc
        msgs: list[Message] = []
        if self.system:
            msgs.append(Message("system", self.system))
        msgs.append(Message("user", user))
        return tuple(msgs)


def _as_text(v: Any) -> str:
    return v if isinstance(v, str) else canonical_bytes(v).decode()


@dataclass
class SolverContext:
    case: Case
    provider: ModelProvider
    template: PromptTemplate
    params: dict[str, Any]
    seed: int | None
    tools: Mapping[str, Tool] = field(default_factory=dict)
    max_retries: int = 0
    retry_backoff_s: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    model_latency_ms: float = 0.0
    tool_latency_ms: float = 0.0
    usages: list[dict[str, Any]] = field(default_factory=list)
    served_models: set[str] = field(default_factory=set)

    # -- model ----------------------------------------------------------------
    def prompt(self) -> tuple[Message, ...]:
        return self.template.render(self.case)

    def model(self, messages: Sequence[Message] | None = None, **params: Any) -> Response:
        msgs = tuple(messages) if messages is not None else self.prompt()
        request = Request(
            messages=msgs,
            params={**self.params, **params},
            seed=self.seed,
            case_id=self.case.case_id,
        )
        attempt = 0
        while True:
            attempt += 1
            start = time.perf_counter()
            step: dict[str, Any] = {
                "type": "model_call",
                "index": len(self.steps),
                "attempt": attempt,
                "request_digest": request.digest,
            }
            try:
                resp = self.provider.complete(request)
            except ProviderError as exc:
                elapsed = (time.perf_counter() - start) * 1000.0
                self.model_latency_ms += elapsed
                step.update(
                    status="ERROR",
                    error_kind=exc.kind.value,
                    error=str(exc)[:500],
                    latency_ms=round(elapsed, 3),
                )
                self.steps.append(step)
                if exc.retryable and attempt <= self.max_retries:
                    if self.retry_backoff_s:
                        time.sleep(self.retry_backoff_s * attempt)
                    continue
                raise
            elapsed = (
                resp.latency_ms
                if resp.latency_ms is not None
                else ((time.perf_counter() - start) * 1000.0)
            )
            self.model_latency_ms += elapsed
            if resp.served_model:
                self.served_models.add(resp.served_model)
            usage = resp.usage.to_dict()
            self.usages.append(usage)
            step.update(
                status="OK",
                response_digest=digest_text(resp.text),
                served_model=resp.served_model,
                usage=usage,
                latency_ms=round(elapsed, 3),
            )
            if resp.tool_calls:
                step["tool_calls_digest"] = digest(list(resp.tool_calls))
            self.steps.append(step)
            return resp

    # -- tools ----------------------------------------------------------------
    def tool(self, name: str, **args: Any) -> Any:
        step: dict[str, Any] = {
            "type": "tool_call",
            "index": len(self.steps),
            "tool": name,
            "args_digest": digest(args) if _canon_ok(args) else digest_text(repr(args)),
        }
        t = self.tools.get(name)
        if t is None:
            step.update(status="UNKNOWN_TOOL", error=f"no tool named {name!r}")
            self.steps.append(step)
            raise UnknownTool(name, "not registered")
        step["tool_version"] = t.version
        step["tool_code_digest"] = t.identity()["code_digest"]
        start = time.perf_counter()
        try:
            result = t.fn(**args)
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            self.tool_latency_ms += elapsed
            step.update(
                status="ERROR",
                error=f"{type(exc).__name__}: {exc}"[:500],
                latency_ms=round(elapsed, 3),
            )
            self.steps.append(step)
            raise ToolFailure(name, f"{type(exc).__name__}: {exc}") from exc
        elapsed = (time.perf_counter() - start) * 1000.0
        self.tool_latency_ms += elapsed
        step.update(
            status="OK",
            result_digest=digest(result) if _canon_ok(result) else digest_text(repr(result)),
            latency_ms=round(elapsed, 3),
        )
        self.steps.append(step)
        return result

    # -- attribution ----------------------------------------------------------
    def unrecovered_model_error(self) -> ErrorKind | None:
        """Kind of the last model call's error if no later model call succeeded."""
        found = unattributed_failure(self.steps)
        return ErrorKind(found[1]) if found and found[0] == "model" else None


def unattributed_failure(steps: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """The single attribution rule for a solver that returned normally.

    Walk the trajectory backwards. If a failed step is reached before any successful
    model call, the final answer cannot be the model's verdict on a working pipeline:
    return ``("model", error_kind)`` or ``("tool", tool_name)``. A failure followed by a
    successful model call was recovered from and is evidence only.
    """
    for step in reversed(steps):
        if step.get("type") == "model_call":
            if step.get("status") == "OK":
                return None
            return ("model", str(step.get("error_kind")))
        if step.get("type") == "tool_call" and step.get("status") != "OK":
            return ("tool", str(step.get("tool")))
    return None


def _canon_ok(v: Any) -> bool:
    try:
        canonical_bytes(v)
        return True
    except (ValueError, TypeError):
        return False


# -- solvers ------------------------------------------------------------------

SolverFn = Callable[[SolverContext], str]


def generate(ctx: SolverContext) -> str:
    """Single model call with the rendered prompt."""
    return ctx.model().text


def tool_loop(ctx: SolverContext, max_steps: int = 6) -> str:
    """Minimal tool-use loop over OpenAI-style ``tool_calls`` or a text protocol where
    the model replies ``CALL <tool> <json-args>``. Returns the first plain answer."""
    messages = list(ctx.prompt())
    for _ in range(max_steps):
        resp = ctx.model(messages)
        calls: list[tuple[str, dict[str, Any]]] = []
        for tc in resp.tool_calls:
            fn = tc.get("function", {})
            try:
                calls.append((str(fn.get("name")), json.loads(fn.get("arguments") or "{}")))
            except ValueError:
                return resp.text  # model produced malformed tool args: its answer stands
        text = resp.text.strip()
        if not calls and text.startswith("CALL "):
            parts = text.split(" ", 2)
            try:
                calls.append((parts[1], json.loads(parts[2]) if len(parts) > 2 else {}))
            except ValueError:
                return resp.text
        if not calls:
            return resp.text
        messages.append(Message("assistant", resp.text))
        for name, args in calls:
            result = ctx.tool(name, **args)
            messages.append(Message("user", f"TOOL RESULT {name}: {_as_text(result)}"))
    return ""


BUILTIN_SOLVERS: dict[str, SolverFn] = {"generate": generate, "tool_loop": tool_loop}


def solver_identity(name: str, fn: Callable[..., Any]) -> dict[str, Any]:
    try:
        code = digest_text(inspect.getsource(fn))
    except (OSError, TypeError):
        code = None
    return {"name": name, "code_digest": code}
