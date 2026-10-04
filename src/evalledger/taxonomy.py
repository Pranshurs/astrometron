"""Typed outcome taxonomy.

Every requested case resolves to exactly one :class:`Outcome`. The taxonomy separates
*what the model did* from *what went wrong around it*:

==============  ==========================================================  ============
Outcome         Meaning                                                     Blames model
==============  ==========================================================  ============
PASS            Model answered; scorer judged it correct.                   n/a
TASK_FAILURE    Model answered; scorer judged it wrong (incl. malformed     yes
                or unparseable answers - the model produced them).
MODEL_ERROR     Provider reached, but the request was rejected for a        no*
                model-side reason (context too long, content filter,
                invalid/empty response payload).
INFRA_ERROR     Provider unreachable/unavailable: network, DNS, 5xx,        no
                overload, rate limit, auth failure.
TOOL_ERROR      A tool used by the solver failed and the solver did not     no
                recover.
HARNESS_ERROR   EvalLedger-side or user harness code crashed (solver bug,   no
                scorer exception, bad template).
JUDGE_ERROR     The model-as-judge call failed or its verdict could not     no
                be parsed.
TIMEOUT         The case exceeded its deadline; cause unattributed.         no
CANCELLED       The run was interrupted before the case finished.          no
INVALID_CASE    The dataset row itself is unusable (e.g. missing input).    no
EXCLUDED        Explicitly excluded by configuration, with a reason.        no
==============  ==========================================================  ============

\\* MODEL_ERROR is reported separately because some of it (e.g. context limits) is a
real capability limit; it is never folded into TASK_FAILURE silently.

None of the non-PASS outcomes disappears: all count toward ``requested``.
"""

from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    PASS = "PASS"
    TASK_FAILURE = "TASK_FAILURE"
    MODEL_ERROR = "MODEL_ERROR"
    INFRA_ERROR = "INFRA_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    HARNESS_ERROR = "HARNESS_ERROR"
    JUDGE_ERROR = "JUDGE_ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    INVALID_CASE = "INVALID_CASE"
    EXCLUDED = "EXCLUDED"

    @property
    def scored(self) -> bool:
        """True if a scorer produced a verdict on a model answer."""
        return self in SCORED

    @property
    def attempted(self) -> bool:
        """True if execution of the case was started."""
        return self not in NOT_ATTEMPTED


#: Outcomes where a verdict on the model's answer exists.
SCORED = frozenset({Outcome.PASS, Outcome.TASK_FAILURE})
#: Outcomes where the case was never started.
NOT_ATTEMPTED = frozenset({Outcome.EXCLUDED, Outcome.INVALID_CASE, Outcome.CANCELLED})
#: Display order.
ORDER: tuple[Outcome, ...] = tuple(Outcome)


class ErrorKind(str, Enum):
    """Kinds a provider may report; mapped onto outcomes by :func:`outcome_for_provider`."""

    INFRA = "INFRA"  # transport / availability / rate limit / auth
    MODEL = "MODEL"  # reached the model, request rejected or response unusable
    TIMEOUT = "TIMEOUT"


def outcome_for_provider(kind: ErrorKind, *, judge: bool = False) -> Outcome:
    """Map a provider error kind to an outcome. Judge calls always become JUDGE_ERROR."""
    if judge:
        return Outcome.JUDGE_ERROR
    if kind is ErrorKind.INFRA:
        return Outcome.INFRA_ERROR
    if kind is ErrorKind.MODEL:
        return Outcome.MODEL_ERROR
    return Outcome.TIMEOUT
