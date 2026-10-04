"""Callables referenced by test configs (python providers, solvers, tools, scorers)."""

from __future__ import annotations

import contextlib

from evalledger.providers import ProviderError, Request, Response
from evalledger.solver import SolverContext, ToolFailure
from evalledger.taxonomy import ErrorKind


def echo_expected(request: Request) -> str:
    return f"answer {int((request.case_id or 'c0')[1:])}"


def broken_tool(**kwargs: object) -> str:
    raise RuntimeError("tool backend down")


def lookup_tool(key: str) -> str:
    return f"answer {int(key)}"


def tool_solver(ctx: SolverContext) -> str:
    n = ctx.case.case_id[1:]
    ctx.model()
    return str(ctx.tool("lookup", key=n))


def broken_tool_solver(ctx: SolverContext) -> str:
    ctx.model()
    return str(ctx.tool("broken"))


def recovering_tool_solver(ctx: SolverContext) -> str:
    with contextlib.suppress(ToolFailure):
        ctx.tool("broken")
    return ctx.model().text


def swallowing_solver(ctx: SolverContext) -> str:
    """Bad solver: hides provider errors and returns junk. Must not become TASK_FAILURE."""
    try:
        return ctx.model().text
    except ProviderError:
        return "I don't know"


def crashing_solver(ctx: SolverContext) -> str:
    if ctx.case.case_id.endswith("3"):
        raise ZeroDivisionError("harness bug")
    return ctx.model().text


def crashing_scorer(output: str, case: object) -> bool:
    raise KeyError("scorer bug")


def model_error_provider(request: Request) -> Response:
    raise ProviderError(ErrorKind.MODEL, "context length exceeded")


def judge_pass(request: Request) -> str:
    return "Reasoning...\nVERDICT: PASS"


def judge_garbage(request: Request) -> str:
    return "I think it is fine"


def judge_echo(request: Request) -> str:
    """Naive judge that echoes the candidate answer then votes FAIL."""
    body = request.messages[-1].content
    return body + "\nVERDICT: FAIL"


def judge_down(request: Request) -> str:
    raise ProviderError(ErrorKind.INFRA, "judge 503")
