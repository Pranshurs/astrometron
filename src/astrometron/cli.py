"""``astrometron`` command-line interface.

Exit codes: 0 ok; 1 evidence problem (invalid/stale receipt, divergence, no eligible
model); 2 usage/config error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import digest_bytes
from .config import ConfigError, load_config
from .dataset import DatasetError
from .receipt import STATUS_COMPLETE
from .runner import IntegrityError, ProvenanceMismatch, run_eval
from .store import DEFAULT_ROOT, Store, StoreError
from .taxonomy import ORDER


def _store(args: argparse.Namespace) -> Store:
    return Store(args.store or os.environ.get("ASTROMETRON_HOME") or DEFAULT_ROOT)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f} ms"


def _usd(v: float | None) -> str:
    return "UNKNOWN" if v is None else f"${v:.8f}"


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def render_summary(r: dict[str, Any], store: Store | None = None) -> str:
    agg = r["aggregate"]
    ident = r["provider"]["identity"]
    lines = [
        "ASTROMETRON RUN" + ("" if r["status"] == STATUS_COMPLETE else f"  [{r['status']}]"),
        "",
        f"Run:              {r['run_id']}",
        f"Dataset:          {r['dataset']['id']}  ({r['dataset']['revision'][:19]}…)",
        f"Cases:            {agg['requested']}",
        f"Provider:         {ident['provider']}/{ident['model']}"
        + ("" if ident.get("pinned") else "  (revision not pinned)"),
        f"Scorer:           {r['scorer']['name']}-v{r['scorer']['version']}",
    ]
    if r.get("judge"):
        lines.append(f"Judge:            {r['judge']['provider']}/{r['judge']['model']}")
    lines.append("")
    for o in ORDER:
        n = agg["counts"][o.value]
        if n or o.value in ("PASS", "TASK_FAILURE"):
            lines.append(f"{o.value:<16}{n:>6}")
    lines += ["", f"{'TOTAL':<16}{agg['accounted']:>6}", ""]
    if agg["accounted"] == agg["requested"]:
        lines.append("No rows omitted.")
    else:
        lines.append(
            f"ACCOUNTING FAILURE: requested {agg['requested']}, accounted {agg['accounted']}"
        )
    pr = agg["pass_rate"]
    ci = pr.get("ci95_wilson")
    lines += [
        "",
        f"Pass rate:        {_pct(pr['value'])} of {pr['denominator']} ({pr['denominator_basis']})"
        + (f"  95% CI [{_pct(ci[0])}, {_pct(ci[1])}]" if ci else ""),
    ]
    ps = agg["pass_rate_of_scored"]
    if ps["denominator"] != pr["denominator"]:
        lines.append(
            f"  conditional:    {_pct(ps['value'])} of {ps['denominator']} scored "
            "(PASS / (PASS+TASK_FAILURE)) - not the headline number"
        )
    if agg.get("scored_after_tool_failure"):
        lines.append(
            f"  note:           {agg['scored_after_tool_failure']} scored case(s) answered after "
            "a tool failure (marked 'degraded')"
        )
    lat = agg["latency_ms"]
    lines.append(
        f"Model latency:    p50 {_ms(lat['model']['p50'])}  p95 {_ms(lat['model']['p95'])}  "
        f"p99 {_ms(lat['model']['p99'])}"
    )
    lines.append(
        f"Overhead (ours):  p50 {_ms(lat['ledger_overhead']['p50'])}  "
        f"p95 {_ms(lat['ledger_overhead']['p95'])}"
    )
    cost = agg["cost"]
    if cost["complete"] and cost["per_case_usd"] is not None:
        basis = "price table" if r.get("pricing") else "provider-reported"
        lines.append(
            f"Cost:             ${cost['total_usd_known']:.6f} total, "
            f"${cost['per_case_usd']:.8f}/case ({basis})"
        )
    else:
        lines.append(f"Cost:             UNKNOWN for {cost['unknown_cases']} case(s)")
    for w in r.get("warnings", []):
        lines.append(f"WARNING: {w}")
    if store is not None:
        lines += ["", "Receipt:", f"{store.root / 'runs' / r['run_id'] / 'receipt.json'}"]
    lines.append(f"receipt_digest {r['receipt_digest']}")
    return "\n".join(lines)


# -- commands -------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    store = _store(args)
    cfg = load_config(args.config)

    def progress(done: int, total: int) -> None:
        if not args.quiet and sys.stderr.isatty():
            print(f"\r{done}/{total}", end="", file=sys.stderr, flush=True)

    receipt = run_eval(cfg, store, progress=progress)
    if not args.quiet and sys.stderr.isatty():
        print("\r", end="", file=sys.stderr)
    if args.json:
        _dump(receipt)
    else:
        print(render_summary(receipt, store))
    return 0 if receipt["status"] == STATUS_COMPLETE else 130


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    run_id = store.resolve(args.run)
    row = store.run_row(run_id)
    if row["status"] == "RUNNING":
        print(f"run {run_id} is INCOMPLETE (never finished); it is not evidence")
        return 1
    receipt = store.load_receipt(run_id)
    if args.json:
        _dump(receipt)
        return 0
    print(render_summary(receipt, store))
    if args.cases:
        print()
        print(f"{'case_id':<24}{'outcome':<15}detail")
        for c in receipt["cases"]:
            if args.cases == "failed" and c["outcome"] == "PASS":
                continue
            detail = (
                (c.get("error") or {}).get("message")
                or (c.get("score") or {}).get("detail")
                or c.get("exclusion_reason")
                or ""
            )
            print(f"{c['case_id']:<24}{c['outcome']:<15}{str(detail)[:80]}")
    v = store.verify_run(run_id)
    print("\nIntegrity: " + ("VALID" if v.ok else "INVALID - " + "; ".join(v.problems[:3])))
    return 0 if v.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from .reproduce import staleness

    store = _store(args)
    run_id = store.resolve(args.run)
    v = store.verify_run(run_id)
    stale: list[str] = []
    if v.ok:
        stale = staleness(store.load_receipt(run_id))
    verdict = "INVALID" if not v.ok else ("STALE" if stale else "VALID")
    if args.json:
        _dump({"run_id": run_id, "verdict": verdict, "problems": v.problems, "stale": stale})
    else:
        print(f"{run_id}: {verdict}")
        for p in v.problems:
            print(f"  - {p}")
        for s in stale:
            print(f"  - stale: {s}")
    return 0 if verdict == "VALID" else 1


def cmd_compare(args: argparse.Namespace) -> int:
    from .compare import compare_runs

    store = _store(args)
    res = compare_runs(store, store.resolve(args.run_a), store.resolve(args.run_b))
    if args.json:
        _dump(res)
        return 0
    print(f"COMPARE {res['run_a']} -> {res['run_b']}")
    print(f"\nComparability: {res['comparability']}")
    for r in res["reasons"]:
        print(f"  - {r}")
    if "pass_rate" in res:
        pr = res["pass_rate"]
        print(
            f"\nPASS RATE (each on its full denominator {pr['denominators'][0]} / "
            f"{pr['denominators'][1]})"
        )
        print(f"{_pct(pr['a'])} -> {_pct(pr['b'])}   {pr['delta_pp']:+.1f} percentage points")
    if "paired" in res:
        p = res["paired"]
        ci = p["delta_ci95_pp"]
        print(
            f"\nPaired on {p['cases']} cases scored in both runs"
            + (f": {p['delta_pp']:+.1f} pp" if p["delta_pp"] is not None else "")
            + (f"  95% CI [{ci[0]:+.1f}, {ci[1]:+.1f}]" if ci else "")
            + (f"  McNemar p={p['mcnemar_p']}" if p["mcnemar_p"] is not None else "")
        )
        print(f"\nImproved cases:     {p['improved']:>5}")
        print(f"Regressed cases:    {p['regressed']:>5}")
        print(f"Unchanged PASS:     {p['unchanged_pass']:>5}")
        print(f"Unchanged FAIL:     {p['unchanged_fail']:>5}")
        print(
            f"Not scored in both: {p['not_scored_in_both']:>5}  (errors/cancellations: "
            "not model verdicts, excluded from the paired test)"
        )
        print(f"New infra failures: {p['new_infra_failures']:>5}")
        if p["row_changed"] or p["only_in_a"] or p["only_in_b"]:
            print(
                f"Not paired: {p['row_changed']} changed rows, {p['only_in_a']} only in A, "
                f"{p['only_in_b']} only in B"
            )
        if p["transitions"]:
            print("\nOutcome transitions:")
            for k, n in p["transitions"].items():
                print(f"  {k:<36}{n:>5}")
        lat = res["latency_ms_model"]
        print(f"\nModel latency p95: {_ms(lat['p95']['a'])} -> {_ms(lat['p95']['b'])}")
        c = res["cost_per_case_usd"]
        print(f"Cost per case:     {_usd(c['a'])} -> {_usd(c['b'])}")
    if res.get("config_changes"):
        print("\nConfig changes:")
        for k, (va, vb) in list(res["config_changes"].items())[:20]:
            print(f"  {k}: {va!r} -> {vb!r}")
    for w in res["warnings"]:
        print(f"\nWARNING:\n{w}")
    return 0


def cmd_reproduce(args: argparse.Namespace) -> int:
    from .reproduce import NOT_EXACT, REPRODUCED, VERIFIED_ONLY, reproduce

    store = _store(args)
    rep = reproduce(
        store, store.resolve(args.run), execute=not args.no_execute, repeats=args.repeats
    )
    if args.json:
        _dump(rep.to_dict())
    else:
        print(f"REPRODUCE {rep.original}\n\nVerdict: {rep.verdict}")
        if rep.new_run:
            print(f"New run: {rep.new_run}")
        for m in rep.mismatches:
            print(f"  mismatch: {m}")
        for n in rep.notes:
            print(f"  note: {n}")
        if rep.agreement:
            a = rep.agreement
            print(
                f"\nCases identical: {a['identical']}/{a['cases']}"
                + (" (all differences involve infra errors)" if a["infra_only"] else "")
            )
    return 0 if rep.verdict in (REPRODUCED, VERIFIED_ONLY, NOT_EXACT) else 1


def cmd_runs(args: argparse.Namespace) -> int:
    store = _store(args)
    rows = store.list_runs(task=args.task)
    if args.json:
        _dump(rows)
        return 0
    print(f"{'run_id':<28}{'status':<12}{'task':<16}{'model':<28}{'pass':>8}  dataset")
    for r in rows:
        print(
            f"{r['run_id']:<28}{r['status']:<12}{(r['task'] or '-'):<16}"
            f"{(r['provider'] + '/' + r['model'])[:27]:<28}{_pct(r['pass_rate']):>8}  "
            f"{r['dataset_id']}"
        )
    return 0


def cmd_datasets(args: argparse.Namespace) -> int:
    from .dataset import load_file

    store = _store(args)
    if args.ds_cmd == "list":
        rows = store.list_datasets()
        if args.json:
            _dump(rows)
            return 0
        print(f"{'dataset_id':<28}{'cases':>7}  revision")
        for r in rows:
            print(f"{r['dataset_id']:<28}{r['case_count']:>7}  {r['revision']}")
        return 0
    if args.ds_cmd == "register":
        ds = load_file(args.path, args.id)
        store.register_dataset(ds)
        print(f"{ds.dataset_id} {ds.revision} ({len(ds)} cases)")
        return 0
    # verify
    rows = [r for r in store.list_datasets() if r["dataset_id"] == args.dataset_id]
    if not rows:
        print(f"no dataset {args.dataset_id!r} in store")
        return 1
    ok = True
    for r in rows:
        try:
            store.get_dataset(r["revision"])
            status = "snapshot OK"
        except StoreError as exc:
            ok, status = False, f"SNAPSHOT INVALID: {exc}"
        print(f"{r['revision']}  {status}")
    latest = rows[-1]
    src = args.path or latest["source"]
    if src and Path(src).exists():
        current = digest_bytes(Path(src).read_bytes())
        if current == latest["source_digest"]:
            print(f"source {src}: unchanged since latest revision")
        else:
            cur_ds = load_file(src, args.dataset_id)
            known = {r["revision"] for r in rows}
            if cur_ds.revision in known:
                print(
                    f"source {src}: bytes changed, content revision {cur_ds.revision} "
                    "already registered"
                )
            else:
                ok = False
                print(
                    f"source {src}: CHANGED - content is now {cur_ds.revision}, which is "
                    "not a registered revision"
                )
    return 0 if ok else 1


def cmd_route(args: argparse.Namespace) -> int:
    from .router import load_policy, render_decision, route

    store = _store(args)
    policy = load_policy(args.policy)
    decision = route(store, policy, task=args.task)
    if args.json:
        _dump(decision)
    else:
        print(render_decision(decision))
    return 0 if decision["status"] == "SELECTED" else 1


def cmd_selfcheck(args: argparse.Namespace) -> int:
    from .integrity import render, run_suite

    results = run_suite()
    if args.json:
        _dump(results)
    else:
        print(render(results))
    return 0 if all(r["passed"] for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="astrometron", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"astrometron {__version__}")
    p.add_argument("--store", help="store directory (default $ASTROMETRON_HOME or ./.astrometron)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    s = add("run", "run an evaluation and write a receipt")
    s.add_argument("config")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(fn=cmd_run)
    s = add("show", "show a run summary")
    s.add_argument("run", help="run id, unique prefix, or 'latest'")
    s.add_argument("--cases", nargs="?", const="all", choices=["all", "failed"])
    s.set_defaults(fn=cmd_show)
    s = add("verify", "verify a receipt's integrity and freshness")
    s.add_argument("run")
    s.set_defaults(fn=cmd_verify)
    s = add("compare", "compare two runs")
    s.add_argument("run_a")
    s.add_argument("run_b")
    s.set_defaults(fn=cmd_compare)
    s = add("reproduce", "re-execute a run from its receipt and check it")
    s.add_argument("run")
    s.add_argument("--no-execute", action="store_true", help="check inputs only")
    s.add_argument("--repeats", type=int, default=1, help="re-run N times; all must agree")
    s.set_defaults(fn=cmd_reproduce)
    s = add("runs", "list runs")
    s.add_argument("--task")
    s.set_defaults(fn=cmd_runs)
    s = add("datasets", "list, register or verify datasets")
    ds = s.add_subparsers(dest="ds_cmd", required=True)
    d = ds.add_parser("list")
    d.add_argument("--json", action="store_true")
    d = ds.add_parser("register")
    d.add_argument("path")
    d.add_argument("--id")
    d.add_argument("--json", action="store_true")
    d = ds.add_parser("verify")
    d.add_argument("dataset_id")
    d.add_argument("--path", help="current source file to check (default: recorded source)")
    d.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_datasets)
    s = add("route", "choose a model from verified evaluation evidence")
    s.add_argument("policy")
    s.add_argument("--task", help="task class (default: policy's task)")
    s.set_defaults(fn=cmd_route)
    s = add("selfcheck", "run the built-in eval-integrity adversarial suite")
    s.set_defaults(fn=cmd_selfcheck)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (ConfigError, DatasetError, StoreError, ProvenanceMismatch, ValueError) as exc:
        print(f"astrometron: error: {exc}", file=sys.stderr)
        return 2
    except IntegrityError as exc:
        print(f"astrometron: INTEGRITY ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
