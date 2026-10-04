"""Deterministic scorers.

A scorer turns ``(model output, case)`` into a :class:`Score`. Two failure channels
are kept apart on purpose:

* the *model's* answer is malformed (not a number, not JSON, not a known label)
  -> a failing :class:`Score` -> ``TASK_FAILURE`` (the model produced it);
* the *scorer* cannot operate (no expected value, bad regex, a bug)
  -> :class:`ScorerError` or any exception -> ``HARNESS_ERROR``.

Each scorer's identity includes a digest of its implementation source, so changing the
code without bumping ``version`` is still visible in the receipt.
"""

from __future__ import annotations

import inspect
import json
import math
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .canonical import canonical_bytes, digest_text
from .dataset import Case


class ScorerError(Exception):
    """The scorer itself could not produce a verdict (harness-side problem)."""


@dataclass(frozen=True)
class Score:
    passed: bool
    value: float | None = None
    detail: str | None = None
    #: Predicted label for classification scorers.
    label: str | None = None
    #: Extra evidence (judge provenance etc.).
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"passed": self.passed, "value": self.value}
        if self.detail is not None:
            out["detail"] = self.detail
        if self.label is not None:
            out["label"] = self.label
        if self.evidence:
            out["evidence"] = self.evidence
        return out


def _code_digest(obj: Any) -> str | None:
    try:
        return digest_text(inspect.getsource(obj))
    except (OSError, TypeError):
        return None


def _expected_text(case: Case) -> str:
    if case.expected is None:
        raise ScorerError(f"case {case.case_id} has no expected value")
    if isinstance(case.expected, str):
        return case.expected
    return canonical_bytes(case.expected).decode()


class Scorer:
    name: ClassVar[str] = "base"
    version: ClassVar[str] = "0"

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params = dict(params or {})

    def identity(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "params": self.params,
            "code_digest": _code_digest(type(self)),
        }

    def score(self, output: str, case: Case) -> Score:  # pragma: no cover - abstract
        raise NotImplementedError


class ExactMatch(Scorer):
    name = "exact"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        ok = output == _expected_text(case)
        return Score(ok, 1.0 if ok else 0.0)


_PUNCT = str.maketrans("", "", string.punctuation)


def normalize(text: str) -> str:
    return " ".join(text.casefold().translate(_PUNCT).split())


class NormalizedMatch(Scorer):
    """Case-folded, punctuation-stripped, whitespace-collapsed equality."""

    name = "normalized"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        ok = normalize(output) == normalize(_expected_text(case))
        return Score(ok, 1.0 if ok else 0.0)


class RegexMatch(Scorer):
    """``pattern`` param, or the case's expected value used as a pattern."""

    name = "regex"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        pattern = self.params.get("pattern") or _expected_text(case)
        try:
            rx = re.compile(pattern, re.IGNORECASE if self.params.get("ignore_case") else 0)
        except re.error as exc:
            raise ScorerError(f"invalid regex {pattern!r}: {exc}") from exc
        hit = rx.fullmatch(output.strip()) if self.params.get("full", True) else rx.search(output)
        return Score(hit is not None, 1.0 if hit else 0.0)


_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class NumericTolerance(Scorer):
    """Numeric equality within ``abs_tol`` / ``rel_tol``. ``extract: first`` takes the
    first number in the answer; default requires the whole answer to be a number."""

    name = "numeric"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        try:
            expected = float(_expected_text(case))
        except ValueError as exc:
            raise ScorerError(f"expected value is not numeric: {exc}") from exc
        text = output.strip().replace(",", "")
        if self.params.get("extract") == "first":
            m = _NUM.search(text)
            text = m.group(0) if m else ""
        try:
            got = float(text)
        except ValueError:
            return Score(False, 0.0, detail="answer is not a number")
        if not math.isfinite(got):
            return Score(False, 0.0, detail="answer is not finite")
        ok = math.isclose(
            got,
            expected,
            rel_tol=float(self.params.get("rel_tol", 0.0)),
            abs_tol=float(self.params.get("abs_tol", 1e-9)),
        )
        return Score(ok, 1.0 if ok else 0.0)


class JSONFields(Scorer):
    """Answer must be a JSON object; ``fields`` (default: all expected keys) must equal."""

    name = "json_fields"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        if not isinstance(case.expected, dict):
            raise ScorerError(f"case {case.case_id}: expected must be an object")
        try:
            got = json.loads(output)
        except ValueError:
            return Score(False, 0.0, detail="answer is not valid JSON")
        if not isinstance(got, dict):
            return Score(False, 0.0, detail="answer is not a JSON object")
        fields = self.params.get("fields") or sorted(case.expected)
        wrong = [f for f in fields if got.get(f) != case.expected.get(f)]
        value = 1.0 - len(wrong) / len(fields) if fields else 1.0
        return Score(not wrong, value, detail=f"mismatched: {wrong}" if wrong else None)


class Classification(Scorer):
    """Normalized label match. Answers outside ``labels`` fail with the label recorded
    as ``<invalid>`` so per-label metrics stay honest."""

    name = "classification"
    version = "1"

    def score(self, output: str, case: Case) -> Score:
        labels = [normalize(str(x)) for x in self.params.get("labels", [])]
        got = normalize(output)
        want = normalize(_expected_text(case))
        if labels and got not in labels:
            return Score(False, 0.0, detail="label not in label set", label="<invalid>")
        ok = got == want
        return Score(ok, 1.0 if ok else 0.0, label=got)


class PythonScorer(Scorer):
    """``function: module:fn`` called as ``fn(output, case)``; returns bool, float in
    [0,1] (passes at ``threshold``, default 1.0) or a :class:`Score`."""

    name = "python"
    version = "1"

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        super().__init__(params)
        from .providers.python_callable import resolve_callable

        self.fn: Callable[..., Any] = resolve_callable(str(self.params["function"]))

    def identity(self) -> dict[str, Any]:
        ident = super().identity()
        ident["function_digest"] = _code_digest(self.fn)
        return ident

    def score(self, output: str, case: Case) -> Score:
        out = self.fn(output, case)
        if isinstance(out, Score):
            return out
        if isinstance(out, bool):
            return Score(out, 1.0 if out else 0.0)
        if isinstance(out, (int, float)):
            thr = float(self.params.get("threshold", 1.0))
            return Score(float(out) >= thr, float(out))
        raise ScorerError(f"scorer returned {type(out).__name__}")


BUILTIN: dict[str, type[Scorer]] = {
    cls.name: cls
    for cls in (
        ExactMatch,
        NormalizedMatch,
        RegexMatch,
        NumericTolerance,
        JSONFields,
        Classification,
        PythonScorer,
    )
}


def build_scorer(options: Mapping[str, Any]) -> Scorer:
    kind = options.get("kind")
    params = {k: v for k, v in options.items() if k != "kind"}
    if kind == "judge":
        from .judge import JudgeScorer

        return JudgeScorer(params)
    cls = BUILTIN.get(str(kind))
    if cls is None:
        raise ValueError(f"unknown scorer {kind!r}; known: {sorted([*BUILTIN, 'judge'])}")
    return cls(params)
