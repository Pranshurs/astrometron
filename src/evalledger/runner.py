"""Evaluation execution.

``run_eval`` guarantees that every selected case ends with exactly one typed record,
whatever happens: provider outages, tool crashes, scorer bugs, judge failures,
timeouts, Ctrl-C. Before a receipt is written it is re-verified by
:func:`evalledger.receipt.verify_receipt`; a receipt that fails its own accounting is
never stored as evidence.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import digest, digest_bytes, digest_text
from .config import EvalConfig
from .cost import PriceTable, case_cost, load_price_table
from .dataset import Case, Dataset, Provenance, load_file
from .judge import JudgeError, JudgeScorer
from .providers import MockProvider, ModelProvider, ProviderError, build_provider
from .providers.python_callable import resolve_callable
from .receipt import (
    RECEIPT_SCHEMA,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    aggregate,
    seal,
    verify_receipt,
)
from .scorers import Scorer, build_scorer
from .solver import (
    BUILTIN_SOLVERS,
    PromptTemplate,
    SolverContext,
    SolverFn,
    TemplateError,
    Tool,
    ToolFailure,
    UnknownTool,
    solver_identity,
)
from .store import Store
from .taxonomy import Outcome, outcome_for_provider


class ProvenanceMismatch(RuntimeError):
    """The data on disk is not the data the config claims to evaluate."""


class IntegrityError(RuntimeError):
    """The harness produced a receipt that fails its own accounting. Not stored."""


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_run_id() -> str:
    """ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def git_info(path: Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if commit is None:
        return {"commit": None, "dirty": None}
    status = git("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status)}


def environment() -> dict[str, Any]:
    def ver(dist: str) -> str | None:
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return None

    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": {"evalledger": __version__, "PyYAML": ver("PyYAML")},
    }


# -- assembly -----------------------------------------------------------------


def load_dataset_for(config: EvalConfig, store: Store) -> Dataset:
    spec = config.dataset
    ds = load_file(
        config.resolve(spec["path"]),
        spec.get("id"),
        fields=spec.get("fields"),
        description=spec.get("description"),
    )
    pinned = spec.get("revision")
    if pinned and pinned != ds.revision:
        raise ProvenanceMismatch(
            f"dataset provenance mismatch: config pins {ds.dataset_id} at {pinned}, "
            f"but {spec['path']} now has content revision {ds.revision}. "
            "The dataset changed; update the pin deliberately or restore the data."
        )
    previous = store.dataset_revisions(ds.dataset_id)
    if previous and ds.revision not in previous:
        prov = ds.provenance
        ds = Dataset(
            ds.dataset_id,
            ds.cases(),
            Provenance(
                source=prov.source,
                source_digest=prov.source_digest,
                format=prov.format,
                created_at=prov.created_at,
                parent_revision=previous[-1],
                description=prov.description,
            ),
        )
    return ds


def _tools(config: EvalConfig) -> dict[str, Tool]:
    out: dict[str, Tool] = {}
    for t in config.raw.get("tools") or []:
        name = str(t["name"])
        if name in out:
            raise ValueError(f"duplicate tool {name!r}")
        out[name] = Tool(
            name,
            str(t.get("version", "0")),
            resolve_callable(str(t["function"])),
            str(t.get("description", "")),
        )
    return out


def _solver(config: EvalConfig) -> tuple[SolverFn, dict[str, Any]]:
    spec = config.raw.get("solver") or "generate"
    if isinstance(spec, str):
        if spec not in BUILTIN_SOLVERS:
            raise ValueError(f"unknown solver {spec!r}")
        fn = BUILTIN_SOLVERS[spec]
        return fn, solver_identity(spec, fn)
    fn = resolve_callable(str(spec["function"]))
    return fn, solver_identity(str(spec["function"]), fn)


def _add_python_path(config: EvalConfig) -> None:
    for p in config.raw.get("python_path") or []:
        full = str(config.resolve(str(p)))
        if full not in sys.path:
            sys.path.insert(0, full)


# -- per-case execution --------------------------------------------------------


class _CaseRunner:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        scorer: Scorer,
        solver: SolverFn,
        template: PromptTemplate,
        params: dict[str, Any],
        seed: int | None,
        tools: dict[str, Tool],
        retries: int,
        backoff: float,
        timeout_s: float | None,
        excluded: Mapping[str, str],
        price_table: PriceTable | None,
        record_outputs: bool,
    ) -> None:
        self.provider = provider
        self.scorer = scorer
        self.solver = solver
        self.template = template
        self.params = params
        self.seed = seed
        self.tools = tools
        self.retries = retries
        self.backoff = backoff
        self.timeout_s = timeout_s
        self.excluded = excluded
        self.price_table = price_table
        self.record_outputs = record_outputs
        self.model_key = provider.identity().key

    def base(self, case: Case) -> dict[str, Any]:
        return {
            "case_id": case.case_id,
            "row_digest": case.row_digest,
            "input_digest": case.input_digest,
            "expected_digest": case.expected_digest,
        }

    def not_attempted(
        self, case: Case, outcome: Outcome, error: dict[str, Any] | None, reason: str | None = None
    ) -> dict[str, Any]:
        rec = self.base(case)
        rec.update(
            outcome=outcome.value,
            output_digest=None,
            score=None,
            error=error,
            latency_ms={},
            usage=[],
            cost_usd=None,
            cost_basis="none",
            trajectory=[],
        )
        if reason is not None:
            rec["exclusion_reason"] = reason
        return rec

    def run(self, case: Case) -> dict[str, Any]:
        if case.case_id in self.excluded:
            return self.not_attempted(case, Outcome.EXCLUDED, None, self.excluded[case.case_id])
        if case.invalid_reason:
            return self.not_attempted(
                case, Outcome.INVALID_CASE, {"stage": "dataset", "message": case.invalid_reason}
            )
        ctx = SolverContext(
            case=case,
            provider=self.provider,
            template=self.template,
            params=self.params,
            seed=self.seed,
            tools=self.tools,
            max_retries=self.retries,
            retry_backoff_s=self.backoff,
        )
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["record"] = self._execute(case, ctx)
            except BaseException as exc:  # pragma: no cover - _execute catches all
                box["crash"] = exc

        t0 = time.perf_counter()
        if self.timeout_s:
            th = threading.Thread(target=work, daemon=True, name=f"case-{case.case_id}")
            th.start()
            th.join(self.timeout_s)
            if th.is_alive():
                rec = self.base(case)
                rec.update(
                    outcome=Outcome.TIMEOUT.value,
                    output_digest=None,
                    score=None,
                    error={"stage": "case", "message": f"exceeded timeout {self.timeout_s}s"},
                    latency_ms={
                        "total": round((time.perf_counter() - t0) * 1000, 3),
                        "model": round(ctx.model_latency_ms, 3),
                    },
                    usage=list(ctx.usages),
                    cost_usd=None,
                    cost_basis="UNKNOWN",
                    trajectory=list(ctx.steps),
                )
                return rec
        else:
            work()
        if "crash" in box:
            rec = self.base(case)
            rec.update(
                outcome=Outcome.HARNESS_ERROR.value,
                output_digest=None,
                score=None,
                error={"stage": "harness", "message": repr(box["crash"])[:500]},
                latency_ms={},
                usage=[],
                cost_usd=None,
                cost_basis="UNKNOWN",
                trajectory=list(ctx.steps),
            )
            return rec
        record: dict[str, Any] = box["record"]
        return record

    def _execute(self, case: Case, ctx: SolverContext) -> dict[str, Any]:
        t0 = time.perf_counter()
        rec = self.base(case)
        output: Any = None
        error: dict[str, Any] | None = None
        outcome: Outcome
        score: dict[str, Any] | None = None
        scoring_ms = 0.0
        try:
            output = self.solver(ctx)
        except UnknownTool as exc:
            outcome, error = Outcome.HARNESS_ERROR, {"stage": "solver", "message": str(exc)}
        except ToolFailure as exc:
            outcome, error = (
                Outcome.TOOL_ERROR,
                {"stage": "tool", "tool": exc.tool, "message": str(exc)[:500]},
            )
        except ProviderError as exc:
            outcome = outcome_for_provider(exc.kind)
            error = {"stage": "model", "kind": exc.kind.value, "message": str(exc)[:500]}
        except TemplateError as exc:
            outcome, error = Outcome.HARNESS_ERROR, {"stage": "prompt", "message": str(exc)}
        except Exception as exc:
            outcome, error = (
                Outcome.HARNESS_ERROR,
                {"stage": "solver", "message": f"{type(exc).__name__}: {exc}"[:500]},
            )
        else:
            unrecovered = ctx.unrecovered_model_error()
            if unrecovered is not None:
                outcome = outcome_for_provider(unrecovered)
                error = {
                    "stage": "model",
                    "kind": unrecovered.value,
                    "message": "final model call failed; solver output not attributable",
                }
            elif not isinstance(output, str):
                outcome, error = (
                    Outcome.HARNESS_ERROR,
                    {"stage": "solver", "message": f"solver returned {type(output).__name__}"},
                )
            else:
                s0 = time.perf_counter()
                try:
                    sc = self.scorer.score(output, case)
                except JudgeError as exc:
                    outcome, error = (
                        Outcome.JUDGE_ERROR,
                        {"stage": "judge", "message": str(exc)[:500], "evidence": exc.evidence},
                    )
                except Exception as exc:
                    outcome, error = (
                        Outcome.HARNESS_ERROR,
                        {"stage": "scorer", "message": f"{type(exc).__name__}: {exc}"[:500]},
                    )
                else:
                    score = sc.to_dict()
                    outcome = Outcome.PASS if sc.passed else Outcome.TASK_FAILURE
                    if sc.label is not None:
                        rec["expected_label"] = (
                            str(case.expected).casefold() if case.expected is not None else None
                        )
                scoring_ms = (time.perf_counter() - s0) * 1000.0
        total = (time.perf_counter() - t0) * 1000.0
        cost, basis = case_cost(self.model_key, ctx.usages, self.price_table)
        text = output if isinstance(output, str) else None
        rec.update(
            outcome=outcome.value,
            output_digest=digest_text(text) if text is not None else None,
            score=score,
            error=error,
            latency_ms={
                "total": round(total, 3),
                "model": round(ctx.model_latency_ms, 3),
                "tool": round(ctx.tool_latency_ms, 3),
                "scoring": round(scoring_ms, 3),
                "overhead": round(
                    max(0.0, total - ctx.model_latency_ms - ctx.tool_latency_ms - scoring_ms), 3
                ),
            },
            usage=ctx.usages,
            served_models=sorted(ctx.served_models),
            cost_usd=cost,
            cost_basis=basis,
            trajectory=ctx.steps,
        )
        if self.record_outputs and text is not None:
            rec["output"] = text
        return rec


# -- run ----------------------------------------------------------------------


def run_eval(
    config: EvalConfig,
    store: Store,
    *,
    dataset: Dataset | None = None,
    reproduces: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    _add_python_path(config)
    ds = dataset if dataset is not None else load_dataset_for(config, store)
    store.register_dataset(ds)
    sel = dict(config.dataset.get("select") or {})
    run_cfg = config.run
    seed = run_cfg.get("seed", 0)
    selected = ds.select(
        split=sel.get("split"),
        tags=sel.get("tags"),
        case_ids=sel.get("case_ids"),
        limit=sel.get("limit"),
        sample=sel.get("sample"),
        seed=int(seed or 0),
    )
    excluded = {str(e["case_id"]): str(e["reason"]) for e in config.dataset.get("exclude") or []}
    stray = sorted(set(excluded) - set(selected))
    if stray:
        raise ValueError(f"excluded case ids are not in the selection: {stray[:5]}")

    provider = build_provider(config.raw["provider"])
    if isinstance(provider, MockProvider):
        provider.bind_answer_key({c.case_id: c.expected for c in ds.cases()})
    scorer = build_scorer(config.raw["scorer"])
    prompt_cfg = config.raw.get("prompt") or {}
    if isinstance(prompt_cfg, str):
        prompt_cfg = {"template": prompt_cfg}
    template = PromptTemplate(str(prompt_cfg.get("template", "{input}")), prompt_cfg.get("system"))
    solver_fn, solver_ident = _solver(config)
    tools = _tools(config)
    price_table = load_price_table(config.raw.get("pricing"), config.base_dir)
    params = dict(config.raw.get("params") or {})
    timeout_s = run_cfg.get("timeout_s")
    retries = int(run_cfg.get("retries", 0))
    concurrency = int(run_cfg.get("concurrency", 1))
    identity = provider.identity()

    run_id = new_run_id()
    started = utcnow()
    store.begin_run(
        run_id,
        {
            "name": config.name,
            "task": config.task,
            "started_at": started,
            "dataset_id": ds.dataset_id,
            "dataset_revision": ds.revision,
            "provider": identity.provider,
            "model": identity.model,
            "model_revision": identity.revision,
            "pinned": identity.pinned,
            "reproduces": reproduces,
        },
    )
    runner = _CaseRunner(
        provider=provider,
        scorer=scorer,
        solver=solver_fn,
        template=template,
        params=params,
        seed=seed,
        tools=tools,
        retries=retries,
        backoff=float(run_cfg.get("retry_backoff_s", 0.0)),
        timeout_s=float(timeout_s) if timeout_s else None,
        excluded=excluded,
        price_table=price_table,
        record_outputs=bool(config.raw.get("record_outputs", True)),
    )
    records: dict[str, dict[str, Any]] = {}
    status = STATUS_COMPLETE
    t_wall = time.perf_counter()
    cases = [ds.case(cid) for cid in selected]

    def safe(case: Case) -> dict[str, Any]:
        try:
            return runner.run(case)
        except BaseException as exc:  # defence in depth: never lose a case
            rec = runner.base(case)
            rec.update(
                outcome=Outcome.HARNESS_ERROR.value,
                output_digest=None,
                score=None,
                error={"stage": "harness", "message": repr(exc)[:500]},
                latency_ms={},
                usage=[],
                cost_usd=None,
                cost_basis="UNKNOWN",
                trajectory=[],
            )
            return rec

    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        pending: dict[Future[dict[str, Any]], str] = {}
        it = iter(cases)
        for case in it:
            pending[pool.submit(safe, case)] = case.case_id
            if len(pending) >= concurrency * 2:
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                cid = pending.pop(fut)
                records[cid] = fut.result()
                if progress:
                    progress(len(records), len(cases))
                nxt = next(it, None)
                if nxt is not None:
                    pending[pool.submit(safe, nxt)] = nxt.case_id
    except KeyboardInterrupt:
        status = STATUS_INCOMPLETE
        for fut in list(pending):
            fut.cancel()
    finally:
        pool.shutdown(wait=status == STATUS_COMPLETE, cancel_futures=True)

    for case in cases:
        if case.case_id not in records:
            outcome = Outcome.CANCELLED if status == STATUS_INCOMPLETE else Outcome.HARNESS_ERROR
            err = (
                {"stage": "run", "message": "run interrupted before this case finished"}
                if outcome is Outcome.CANCELLED
                else {"stage": "harness", "message": "no result recorded for case"}
            )
            records[case.case_id] = runner.not_attempted(case, outcome, err)
    case_records = [records[cid] for cid in selected]
    finished = utcnow()

    served = sorted({m for r in case_records for m in r.get("served_models", [])})
    warnings: list[str] = []
    substituted = [m for m in served if m != identity.model]
    if substituted:
        warnings.append(
            f"MODEL SUBSTITUTION: configured model {identity.model!r} but the provider "
            f"reported serving {substituted}"
        )
    if not identity.pinned:
        warnings.append("provider model revision is not pinned: exact reproduction is not possible")
    git = git_info(config.base_dir)
    if git.get("dirty"):
        warnings.append("code revision has uncommitted changes")
    if price_table is None and any(r.get("cost_basis") == "UNKNOWN" for r in case_records):
        warnings.append("cost UNKNOWN for some cases: no provider-reported cost and no price table")

    source_path = config.source_path
    body: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "run_id": run_id,
        "status": status,
        "reproduces": reproduces,
        "eval": {
            "name": config.name,
            "task": config.task,
            "description": config.raw.get("description"),
        },
        "config": config.raw,
        "config_digest": config.digest,
        "config_source": {
            "path": str(source_path) if source_path else None,
            "file_digest": digest_bytes(source_path.read_bytes())
            if source_path and source_path.exists()
            else None,
            "base_dir": str(config.base_dir),
        },
        "dataset": {
            "id": ds.dataset_id,
            "revision": ds.revision,
            "schema": ds.schema_version,
            "case_count": len(ds),
            "provenance": ds.provenance.to_dict(),
            "selection": {**sel, "seed": seed},
            "selected_case_ids": selected,
            "selected_digest": digest(selected),
            "excluded": [{"case_id": k, "reason": v} for k, v in sorted(excluded.items())],
        },
        "code": {
            "evalledger_version": __version__,
            "git_commit": git["commit"],
            "git_dirty": git["dirty"],
            "cwd": os.getcwd(),
        },
        "provider": {"identity": identity.to_dict(), "params": params, "served_models": served},
        "prompt": {
            "digest": template.digest,
            "template": template.template,
            "system": template.system,
        },
        "solver": solver_ident,
        "tools": [t.identity() for t in sorted(tools.values(), key=lambda t: t.name)],
        "scorer": scorer.identity(),
        "judge": scorer.identity().get("judge") if isinstance(scorer, JudgeScorer) else None,
        "environment": environment(),
        "run": {
            "seed": seed,
            "concurrency": concurrency,
            "timeout_s": timeout_s,
            "retries": retries,
        },
        "pricing": price_table.to_dict() if price_table else None,
        "timing": {
            "started_at": started,
            "finished_at": finished,
            "wall_ms": round((time.perf_counter() - t_wall) * 1000, 3),
        },
        "cases": case_records,
        "aggregate": aggregate(case_records, selected, excluded),
        "warnings": warnings,
    }
    receipt = seal(body)
    check = verify_receipt(receipt)
    if not check.ok:
        raise IntegrityError(
            "refusing to store a receipt that fails verification: " + "; ".join(check.problems[:5])
        )
    store.finish_run(receipt)
    return receipt
