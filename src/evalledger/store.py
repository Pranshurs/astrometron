"""Local evidence store.

Layout (default root ``./.evalledger``)::

    objects/sha256/<2 hex>/<62 hex>.json   immutable content-addressed artifacts
                                           (dataset snapshots, routing decisions)
    runs/<run_id>/receipt.json             the sealed EvaluationReceipt
    index.sqlite                           queryable metadata; schema-versioned

Durability semantics (see ARCHITECTURE.md):

* every file is written to a temp file in the same directory, fsynced, then atomically
  renamed into place - readers never see a half-written file;
* a run is inserted as ``RUNNING`` before execution and flipped to its final status only
  after its receipt file is durable. A crash leaves ``RUNNING``, which every reader
  reports as incomplete - a partial run is never reported as complete;
* the index stores each receipt's digest; a receipt file whose digest no longer matches
  its index row is reported as tampered.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes, digest, digest_bytes, is_digest
from .dataset import Dataset, DatasetError
from .receipt import STATUS_COMPLETE, Verification, verify_receipt

SCHEMA_VERSION = 2
DEFAULT_ROOT = ".evalledger"


class StoreError(RuntimeError):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _migrate_0_to_1(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE datasets (
            dataset_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            source TEXT,
            source_digest TEXT,
            case_count INTEGER NOT NULL,
            registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            PRIMARY KEY (dataset_id, revision)
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            name TEXT,
            task TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            dataset_id TEXT NOT NULL,
            dataset_revision TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            model_revision TEXT,
            pinned INTEGER NOT NULL,
            scorer_digest TEXT,
            receipt_digest TEXT,
            inputs_digest TEXT,
            pass_rate REAL,
            requested INTEGER,
            reproduces TEXT
        );
        CREATE INDEX runs_by_task ON runs (task, status);
        CREATE INDEX runs_by_dataset ON runs (dataset_id, dataset_revision);
        """
    )


def _migrate_1_to_2(con: sqlite3.Connection) -> None:
    """v2: routing decisions are recorded, and runs are indexed by candidate."""
    con.executescript(
        """
        CREATE TABLE routing_decisions (
            decision_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            task TEXT,
            status TEXT NOT NULL,
            selected TEXT,
            decision_digest TEXT NOT NULL
        );
        CREATE INDEX runs_by_candidate ON runs (task, provider, model, status);
        """
    )


#: version N -> function migrating N to N+1
MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2}


def schema_version(con: sqlite3.Connection) -> int:
    has_meta = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if not has_meta:
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if tables:
            raise StoreError("index.sqlite has tables but no schema metadata; refusing to touch it")
        return 0
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise StoreError("index.sqlite meta table has no schema_version")
    return int(row[0])


def migrate(con: sqlite3.Connection, target: int = SCHEMA_VERSION) -> int:
    current = schema_version(con)
    if current > target:
        raise StoreError(
            f"store schema v{current} is newer than this EvalLedger (v{target}); upgrade it"
        )
    while current < target:
        with con:
            MIGRATIONS[current](con)
            current += 1
            con.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )
    return current


class Store:
    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite"
        con = self._connect()
        try:
            migrate(con)
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            with con:
                yield con
        finally:
            con.close()

    # -- objects --------------------------------------------------------------
    def _object_path(self, d: str) -> Path:
        if not is_digest(d):
            raise StoreError(f"not a digest: {d!r}")
        h = d.split(":", 1)[1]
        return self.root / "objects" / "sha256" / h[:2] / f"{h[2:]}.json"

    def put_object(self, obj: Any) -> str:
        data = canonical_bytes(obj)
        d = digest_bytes(data)
        path = self._object_path(d)
        if not path.exists():
            atomic_write(path, data)
        return d

    def get_object(self, d: str) -> Any:
        path = self._object_path(d)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise StoreError(f"object {d} not in store") from None
        if digest_bytes(data) != d:
            raise StoreError(f"object {d} is corrupted (content digest mismatch)")
        return json.loads(data)

    # -- datasets -------------------------------------------------------------
    def register_dataset(self, ds: Dataset) -> str:
        snap = ds.to_snapshot()
        # Content-address the snapshot by its revision-bearing content, provenance included.
        obj = self.put_object(snap)
        with self._db() as con:
            con.execute(
                "INSERT OR IGNORE INTO datasets(dataset_id, revision, source, source_digest, "
                "case_count) VALUES(?,?,?,?,?)",
                (
                    ds.dataset_id,
                    ds.revision,
                    ds.provenance.source,
                    ds.provenance.source_digest,
                    len(ds),
                ),
            )
        self._snapshot_pointer(ds.revision).parent.mkdir(parents=True, exist_ok=True)
        if not self._snapshot_pointer(ds.revision).exists():
            atomic_write(self._snapshot_pointer(ds.revision), obj.encode())
        return obj

    def _snapshot_pointer(self, revision: str) -> Path:
        return self.root / "datasets" / revision.replace(":", "-")

    def get_dataset(self, revision: str) -> Dataset:
        ptr = self._snapshot_pointer(revision)
        try:
            obj = ptr.read_text().strip()
        except FileNotFoundError:
            raise StoreError(f"no snapshot for dataset revision {revision}") from None
        try:
            ds = Dataset.from_snapshot(self.get_object(obj))
        except DatasetError as exc:
            raise StoreError(f"dataset snapshot {revision} is invalid: {exc}") from exc
        if ds.revision != revision:
            raise StoreError(f"snapshot pointer for {revision} resolves to {ds.revision}")
        return ds

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._db() as con:
            rows = con.execute(
                "SELECT * FROM datasets ORDER BY dataset_id, registered_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def dataset_revisions(self, dataset_id: str) -> list[str]:
        with self._db() as con:
            rows = con.execute(
                "SELECT revision FROM datasets WHERE dataset_id=? ORDER BY registered_at",
                (dataset_id,),
            ).fetchall()
        return [r[0] for r in rows]

    # -- runs -----------------------------------------------------------------
    def _receipt_path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or ".." in run_id:
            raise StoreError(f"bad run id {run_id!r}")
        return self.root / "runs" / run_id / "receipt.json"

    def begin_run(self, run_id: str, meta: Mapping[str, Any]) -> None:
        with self._db() as con:
            con.execute(
                "INSERT INTO runs(run_id, status, name, task, started_at, dataset_id, "
                "dataset_revision, provider, model, model_revision, pinned, reproduces) "
                "VALUES(?, 'RUNNING', ?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    meta.get("name"),
                    meta.get("task"),
                    meta["started_at"],
                    meta["dataset_id"],
                    meta["dataset_revision"],
                    meta["provider"],
                    meta["model"],
                    meta.get("model_revision"),
                    int(bool(meta.get("pinned"))),
                    meta.get("reproduces"),
                ),
            )

    def finish_run(self, receipt: Mapping[str, Any]) -> Path:
        run_id = receipt["run_id"]
        path = self._receipt_path(run_id)
        atomic_write(
            path, json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
        )
        agg = receipt["aggregate"]
        with self._db() as con:
            cur = con.execute(
                "UPDATE runs SET status=?, finished_at=?, receipt_digest=?, inputs_digest=?, "
                "scorer_digest=?, pass_rate=?, requested=? WHERE run_id=? AND status='RUNNING'",
                (
                    receipt["status"],
                    receipt["timing"]["finished_at"],
                    receipt["receipt_digest"],
                    receipt["inputs_digest"],
                    digest(receipt["scorer"]),
                    agg["pass_rate"]["value"],
                    agg["requested"],
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(f"run {run_id} was not RUNNING; refusing to overwrite")
        return path

    def run_row(self, run_id: str) -> dict[str, Any]:
        with self._db() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"no run {run_id!r}")
        return dict(row)

    def resolve(self, ref: str) -> str:
        """Resolve a run id, unique prefix, or ``latest``."""
        with self._db() as con:
            if ref == "latest":
                row = con.execute(
                    "SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    raise StoreError("no runs in store")
                return str(row[0])
            rows = con.execute(
                "SELECT run_id FROM runs WHERE substr(run_id, 1, ?) = ?", (len(ref), ref)
            ).fetchall()
        if not rows:
            raise StoreError(f"no run matching {ref!r}")
        if len(rows) > 1:
            raise StoreError(f"ambiguous run prefix {ref!r} ({len(rows)} matches)")
        return str(rows[0][0])

    def load_receipt(self, run_id: str) -> dict[str, Any]:
        path = self._receipt_path(run_id)
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            row = self.run_row(run_id)
            raise StoreError(
                f"run {run_id} has no receipt (status {row['status']}: the run did not finish)"
            ) from None
        except ValueError as exc:
            raise StoreError(f"receipt for {run_id} is not valid JSON: {exc}") from exc
        return data

    def list_runs(self, task: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if task:
            q += " AND task=?"
            args.append(task)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY started_at, run_id"
        with self._db() as con:
            return [dict(r) for r in con.execute(q, args).fetchall()]

    def verify_run(self, run_id: str) -> Verification:
        """Full integrity check: receipt self-consistency, index binding, dataset binding."""
        v = Verification(ok=True)
        row = self.run_row(run_id)
        if row["status"] == "RUNNING":
            v.add("run never finished (status RUNNING): incomplete, not evidence")
            return v
        try:
            receipt = self.load_receipt(run_id)
        except StoreError as exc:
            v.add(str(exc))
            return v
        inner = verify_receipt(receipt)
        for p in inner.problems:
            v.add(p)
        if receipt.get("run_id") != run_id:
            v.add(f"receipt run_id {receipt.get('run_id')} != {run_id}")
        if row["receipt_digest"] != receipt.get("receipt_digest"):
            v.add("receipt digest differs from the digest indexed when the run finished")
        if row["status"] != receipt.get("status"):
            v.add(f"index status {row['status']} != receipt status {receipt.get('status')}")
        for column, value in self._index_view(receipt).items():
            if row.get(column) != value:
                v.add(
                    f"index binding: column {column!r} is {row.get(column)!r} in the index "
                    f"but {value!r} in the receipt"
                )
        rev = receipt.get("dataset", {}).get("revision")
        try:
            ds = self.get_dataset(rev)
        except StoreError as exc:
            v.add(f"dataset binding: {exc}")
        else:
            for c in receipt.get("cases", []):
                cid = c.get("case_id")
                if cid not in ds:
                    v.add(f"dataset binding: case {cid} not in snapshot {rev}")
                elif ds.row_digest(cid) != c.get("row_digest"):
                    v.add(f"dataset binding: case {cid} row digest differs from snapshot")
            v_sel = self._rederive_selection(receipt, ds)
            if v_sel:
                v.add(v_sel)
        return v

    @staticmethod
    def _index_view(receipt: Mapping[str, Any]) -> dict[str, Any]:
        """What every queryable index column must say, derived from the receipt."""
        try:
            ident = receipt["provider"]["identity"]
            return {
                "name": receipt["eval"]["name"],
                "task": receipt["eval"]["task"],
                "started_at": receipt["timing"]["started_at"],
                "finished_at": receipt["timing"]["finished_at"],
                "dataset_id": receipt["dataset"]["id"],
                "dataset_revision": receipt["dataset"]["revision"],
                "provider": ident["provider"],
                "model": ident["model"],
                "model_revision": ident.get("revision"),
                "pinned": int(bool(ident.get("pinned"))),
                "scorer_digest": digest(receipt["scorer"]),
                "inputs_digest": receipt["inputs_digest"],
                "pass_rate": receipt["aggregate"]["pass_rate"]["value"],
                "requested": receipt["aggregate"]["requested"],
                "reproduces": receipt.get("reproduces"),
            }
        except (KeyError, TypeError) as exc:
            return {"<receipt>": f"missing field {exc}"}

    @staticmethod
    def _rederive_selection(receipt: Mapping[str, Any], ds: Dataset) -> str | None:
        """Re-apply the recorded selection spec to the snapshot; it must give the same ids."""
        sel = dict(receipt.get("dataset", {}).get("selection") or {})
        try:
            ids = ds.select(
                split=sel.get("split"),
                tags=sel.get("tags"),
                case_ids=sel.get("case_ids"),
                limit=sel.get("limit"),
                sample=sel.get("sample"),
                seed=int(sel.get("seed") or 0),
            )
        except DatasetError as exc:
            return f"selection binding: recorded selection cannot be re-derived: {exc}"
        if ids != receipt["dataset"].get("selected_case_ids"):
            return (
                "selection binding: selected_case_ids differ from the selection re-derived "
                f"from the dataset snapshot ({len(ids)} expected, "
                f"{len(receipt['dataset'].get('selected_case_ids') or [])} recorded)"
            )
        if sel != {
            **(receipt.get("config", {}).get("dataset", {}).get("select") or {}),
            "seed": sel.get("seed"),
        }:
            return "selection binding: recorded selection differs from the recorded config"
        return None

    def record_decision(self, decision: Mapping[str, Any]) -> str:
        obj = self.put_object(decision)
        with self._db() as con:
            con.execute(
                "INSERT INTO routing_decisions(decision_id, created_at, task, status, selected, "
                "decision_digest) VALUES(?,?,?,?,?,?)",
                (
                    decision["decision_id"],
                    decision["created_at"],
                    decision.get("task"),
                    decision["status"],
                    decision.get("selected"),
                    obj,
                ),
            )
        return obj

    def is_trustworthy(self, run_id: str) -> bool:
        row = self.run_row(run_id)
        return row["status"] == STATUS_COMPLETE and self.verify_run(run_id).ok
