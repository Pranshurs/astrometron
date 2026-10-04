"""Measure EvalLedger's own overhead, separately from provider latency.

Writes eval/results/bench.json. Numbers are whatever this machine measures; nothing is
tuned or filtered. Usage: python bench/bench_overhead.py [--cases 1000] [--reps 30]
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evalledger import __version__
from evalledger.compare import compare
from evalledger.config import from_dict
from evalledger.dataset import Dataset, load_file
from evalledger.receipt import aggregate, percentile, seal, verify_receipt
from evalledger.runner import run_eval
from evalledger.store import Store

ROOT = Path(__file__).resolve().parent.parent


def stats(samples_ms: list[float]) -> dict[str, Any]:
    return {
        "n": len(samples_ms),
        "p50_ms": round(percentile(samples_ms, 50) or 0, 4),
        "p95_ms": round(percentile(samples_ms, 95) or 0, 4),
        "p99_ms": round(percentile(samples_ms, 99) or 0, 4),
        "max_ms": round(max(samples_ms), 4),
    }


def timeit(fn: Callable[[], Any], reps: int) -> list[float]:
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def hardware() -> dict[str, Any]:
    def sysctl(key: str) -> str | None:
        try:
            return (
                subprocess.run(
                    ["sysctl", "-n", key], capture_output=True, text=True, check=False
                ).stdout.strip()
                or None
            )
        except OSError:
            return None

    mem = sysctl("hw.memsize")
    return {
        "cpu": sysctl("machdep.cpu.brand_string") or platform.processor(),
        "cores": sysctl("hw.ncpu"),
        "memory_gib": round(int(mem) / 2**30, 1) if mem else None,
        "os": platform.platform(),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "evalledger": __version__,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=1000)
    ap.add_argument("--reps", type=int, default=30)
    ap.add_argument("--out", default=str(ROOT / "eval" / "results" / "bench.json"))
    args = ap.parse_args()
    n, reps = args.cases, args.reps
    tmp = Path(tempfile.mkdtemp(prefix="evalledger-bench-"))
    try:
        data = tmp / "bench.jsonl"
        data.write_text(
            "".join(
                json.dumps(
                    {
                        "id": f"b{i:06d}",
                        "input": f"question number {i} " * 4,
                        "expected": f"answer {i}",
                        "tags": ["bench"],
                    }
                )
                + "\n"
                for i in range(n)
            )
        )
        store = Store(tmp / ".evalledger")

        def cfg(**prov: Any) -> Any:
            return from_dict(
                {
                    "name": "bench",
                    "task": "bench",
                    "dataset": {"path": str(data), "id": "bench"},
                    "provider": {
                        "kind": "mock",
                        "model": "bench",
                        "accuracy": 0.9,
                        "infra_error_rate": 0.02,
                        **prov,
                    },
                    "scorer": {"kind": "exact"},
                    "run": {"seed": 0, "concurrency": prov.pop("concurrency", 1)},
                },
                tmp,
            )

        results: dict[str, Any] = {"hardware": hardware(), "cases": n, "reps": reps}

        # 1) end-to-end throughput, zero-latency provider (pure ledger + harness cost)
        thr = {}
        for conc in (1, 4):
            walls = []
            for _ in range(5):
                t0 = time.perf_counter()
                r = run_eval(cfg(concurrency=conc), store)
                walls.append(time.perf_counter() - t0)
            best = sorted(walls)[len(walls) // 2]
            thr[f"concurrency_{conc}"] = {
                "runs": len(walls),
                "median_wall_s": round(best, 4),
                "cases_per_s": round(n / best, 1),
            }
        results["throughput_zero_latency_provider"] = thr

        # 2) per-case recording overhead (from receipts: total - model - tool - scoring)
        overhead = [c["latency_ms"]["overhead"] for c in r["cases"] if c["latency_ms"]]
        model = [c["latency_ms"]["model"] for c in r["cases"] if c["latency_ms"]]
        results["per_case_ledger_overhead"] = stats(overhead)
        results["per_case_model_latency_zero_latency_mock"] = stats(model)

        # 3) same with a 5 ms provider: overhead must stay separate from model latency
        r5 = run_eval(cfg(latency_ms=5, concurrency=8), store)
        results["with_5ms_provider"] = {
            "model_latency": stats(
                [c["latency_ms"]["model"] for c in r5["cases"] if c["latency_ms"]]
            ),
            "ledger_overhead": stats(
                [c["latency_ms"]["overhead"] for c in r5["cases"] if c["latency_ms"]]
            ),
        }

        # 4) component timings on an n-case receipt
        body = {k: v for k, v in r.items() if k not in ("receipt_digest",)}
        cases, sel = r["cases"], r["dataset"]["selected_case_ids"]
        results["aggregate_computation"] = stats(timeit(lambda: aggregate(cases, sel, []), reps))
        results["receipt_generation_seal"] = stats(timeit(lambda: seal(body), reps))
        results["receipt_verification"] = stats(timeit(lambda: verify_receipt(r), reps))
        results["store_full_verify_run"] = stats(
            timeit(lambda: store.verify_run(r["run_id"]), max(5, reps // 3))
        )
        r2 = run_eval(cfg(accuracy=0.8), store)
        results["comparison"] = stats(timeit(lambda: compare(r, r2), reps))
        results["dataset_load_and_digest"] = stats(timeit(lambda: load_file(data, "bench"), reps))
        snap = load_file(data, "bench").to_snapshot()
        results["dataset_snapshot_verify"] = stats(
            timeit(lambda: Dataset.from_snapshot(snap), reps)
        )

        # 5) persistence: atomic receipt write + fsync + index update
        def persist() -> None:
            rid = "01BENCH" + str(time.perf_counter_ns())[-19:]
            store.begin_run(
                rid,
                {
                    "started_at": "2026-01-01T00:00:00Z",
                    "dataset_id": "bench",
                    "dataset_revision": r["dataset"]["revision"],
                    "provider": "mock",
                    "model": "bench",
                },
            )
            store.finish_run({**r, "run_id": rid})

        results["persistence_finish_run"] = stats(timeit(persist, reps))
        receipt_bytes = (store.root / "runs" / r["run_id"] / "receipt.json").stat().st_size
        results["receipt_size_bytes"] = receipt_bytes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
