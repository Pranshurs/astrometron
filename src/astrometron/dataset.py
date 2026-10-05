"""Immutable, content-addressed evaluation datasets.

A :class:`Dataset` is a frozen set of :class:`Case` rows. Its *revision* is a digest of
its content only (dataset id, schema version, and the sorted ``(case_id, row_digest)``
pairs), so:

* re-loading the same file gives the same revision;
* reordering rows does **not** change the revision or any case identity;
* changing any byte of any row's input, expected output, metadata, tags or split
  **does** change that row's digest and therefore the revision.

Provenance (source path, raw-file digest, creation time, parent revision) is recorded
alongside but is deliberately excluded from the revision.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import CanonicalizationError, digest, digest_bytes

DATASET_SCHEMA = "astrometron.dataset/v1"
_ID_KEYS = ("case_id", "id")
_RESERVED = {"case_id", "id", "input", "expected", "metadata", "tags", "split"}


class DatasetError(ValueError):
    """The dataset cannot be loaded or does not match its claimed identity."""


@dataclass(frozen=True)
class Case:
    """One evaluation case. ``invalid_reason`` is set for rows that are structurally
    unusable; such cases are kept (they still count) and resolve to ``INVALID_CASE``."""

    case_id: str
    input: Any
    expected: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    split: str | None = None
    invalid_reason: str | None = None

    def content(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "input": self.input,
            "expected": self.expected,
            "metadata": dict(self.metadata),
            "tags": list(self.tags),
            "split": self.split,
            "invalid_reason": self.invalid_reason,
        }

    @property
    def row_digest(self) -> str:
        return digest(self.content())

    @property
    def input_digest(self) -> str:
        return digest(self.input)

    @property
    def expected_digest(self) -> str:
        return digest(self.expected)


@dataclass(frozen=True)
class Provenance:
    source: str | None = None
    source_digest: str | None = None
    format: str | None = None
    created_at: str | None = None
    parent_revision: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_digest": self.source_digest,
            "format": self.format,
            "created_at": self.created_at,
            "parent_revision": self.parent_revision,
            "description": self.description,
        }


class Dataset:
    """A frozen collection of cases with a content revision."""

    def __init__(
        self,
        dataset_id: str,
        cases: Iterable[Case],
        provenance: Provenance | None = None,
    ) -> None:
        if not dataset_id or not isinstance(dataset_id, str):
            raise DatasetError("dataset_id must be a non-empty string")
        by_id: dict[str, Case] = {}
        for case in cases:
            if case.case_id in by_id:
                raise DatasetError(f"duplicate case_id {case.case_id!r} in {dataset_id!r}")
            try:
                case.row_digest  # noqa: B018 - validates canonicality eagerly
            except CanonicalizationError as exc:
                raise DatasetError(f"case {case.case_id!r}: {exc}") from exc
            by_id[case.case_id] = case
        if not by_id:
            raise DatasetError(f"dataset {dataset_id!r} has no cases")
        self.dataset_id = dataset_id
        self.schema_version = DATASET_SCHEMA
        self._cases: dict[str, Case] = dict(sorted(by_id.items()))
        self.provenance = provenance or Provenance()
        self._row_digests = {cid: c.row_digest for cid, c in self._cases.items()}
        self.revision = digest(
            {
                "schema": self.schema_version,
                "dataset_id": self.dataset_id,
                "rows": [[cid, d] for cid, d in self._row_digests.items()],
            }
        )

    # -- access ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._cases)

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._cases

    @property
    def case_ids(self) -> list[str]:
        return list(self._cases)

    def case(self, case_id: str) -> Case:
        try:
            return self._cases[case_id]
        except KeyError:
            raise DatasetError(f"case {case_id!r} not in {self.dataset_id}") from None

    def cases(self) -> list[Case]:
        return list(self._cases.values())

    def row_digest(self, case_id: str) -> str:
        return self._row_digests[case_id]

    # -- selection ------------------------------------------------------------
    def select(
        self,
        *,
        split: str | None = None,
        tags: Sequence[str] | None = None,
        case_ids: Sequence[str] | None = None,
        limit: int | None = None,
        sample: int | None = None,
        seed: int = 0,
    ) -> list[str]:
        """Deterministically select case ids. Result is sorted by case_id.

        ``case_ids`` that are not in the dataset raise (never silently dropped).
        ``sample`` picks ``n`` cases by a seeded hash order; ``limit`` takes the first
        ``n`` in case_id order. Both are recorded in the receipt's selection block.
        """
        if case_ids is not None:
            missing = [c for c in case_ids if c not in self._cases]
            if missing:
                raise DatasetError(f"selected case ids not in dataset: {missing[:5]}")
            if len(set(case_ids)) != len(case_ids):
                raise DatasetError("selected case ids contain duplicates")
            pool = sorted(case_ids)
        else:
            pool = list(self._cases)
        if split is not None:
            pool = [c for c in pool if self._cases[c].split == split]
        if tags:
            want = set(tags)
            pool = [c for c in pool if want.issubset(self._cases[c].tags)]
        if sample is not None:
            if sample < 0:
                raise DatasetError("sample must be >= 0")
            pool = sorted(pool, key=lambda c: digest([seed, c]))[:sample]
            pool.sort()
        if limit is not None:
            if limit < 0:
                raise DatasetError("limit must be >= 0")
            pool = pool[:limit]
        if not pool:
            raise DatasetError("selection is empty")
        return pool

    # -- snapshots ------------------------------------------------------------
    def to_snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.schema_version,
            "dataset_id": self.dataset_id,
            "revision": self.revision,
            "provenance": self.provenance.to_dict(),
            "rows": [c.content() for c in self._cases.values()],
        }

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, Any]) -> Dataset:
        if snap.get("schema") != DATASET_SCHEMA:
            raise DatasetError(f"unsupported dataset schema {snap.get('schema')!r}")
        cases = [
            Case(
                case_id=r["case_id"],
                input=r["input"],
                expected=r.get("expected"),
                metadata=r.get("metadata") or {},
                tags=tuple(r.get("tags") or ()),
                split=r.get("split"),
                invalid_reason=r.get("invalid_reason"),
            )
            for r in snap["rows"]
        ]
        prov = Provenance(**(snap.get("provenance") or {}))
        ds = cls(snap["dataset_id"], cases, prov)
        if ds.revision != snap.get("revision"):
            raise DatasetError(
                f"snapshot revision mismatch: claimed {snap.get('revision')}, "
                f"content is {ds.revision}"
            )
        return ds


# -- loaders ------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_case(row: Mapping[str, Any], fields: Mapping[str, str], where: str) -> Case:
    id_key = fields.get("id")
    in_key = fields.get("input", "input")
    exp_key = fields.get("expected", "expected")
    raw_id: Any = None
    id_source: str | None = None
    if id_key:
        raw_id, id_source = row.get(id_key), id_key
    else:
        for k in _ID_KEYS:
            if k in row:
                raw_id, id_source = row[k], k
                break
    # Every column that is not consumed as a field is kept in metadata, so every byte of
    # the row contributes to its digest (including unmapped reserved names).
    consumed = {in_key, exp_key, "metadata", "tags", "split", id_source}
    meta_raw = row.get("metadata") or {}
    if not isinstance(meta_raw, Mapping):
        meta_raw = {"metadata": meta_raw}
    metadata: dict[str, Any] = dict(meta_raw)
    for k, v in row.items():
        if k in consumed:
            continue
        if k in metadata:
            raise DatasetError(f"{where}: column {k!r} collides with a metadata key")
        metadata[k] = v
    tags_raw = row.get("tags") or ()
    if isinstance(tags_raw, str):
        tags_raw = [t for t in tags_raw.split(";") if t]
    tags = tuple(sorted(str(t) for t in tags_raw))
    split = row.get("split") or None
    invalid: str | None = None
    if in_key not in row or row.get(in_key) in (None, ""):
        # No location in the reason: it is part of the row digest, which must not depend
        # on file name or row order.
        invalid = f"missing input field {in_key!r}"
    if raw_id is None or raw_id == "":
        # Identity derived from content: stable under reordering, changes with content.
        basis = row.get(in_key) if invalid is None else dict(row)
        case_id = "auto-" + digest(basis)[7:23]
    else:
        case_id = str(raw_id)
    return Case(
        case_id=case_id,
        input=row.get(in_key),
        expected=row.get(exp_key),
        metadata=metadata,
        tags=tags,
        split=str(split) if split is not None else None,
        invalid_reason=invalid,
    )


def load_rows(
    dataset_id: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: Mapping[str, str] | None = None,
    provenance: Provenance | None = None,
) -> Dataset:
    """Build a dataset from in-memory rows (Python-defined fixtures)."""
    cases = [_row_to_case(r, fields or {}, f"row {i}") for i, r in enumerate(rows)]
    return Dataset(dataset_id, cases, provenance or Provenance(format="python", created_at=_now()))


def load_file(
    path: str | Path,
    dataset_id: str | None = None,
    *,
    fields: Mapping[str, str] | None = None,
    description: str | None = None,
) -> Dataset:
    """Load a JSONL or CSV file into a dataset."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise DatasetError(f"cannot read dataset file {p}: {exc}") from exc
    text = raw.decode("utf-8")
    suffix = p.suffix.lower()
    rows: list[Mapping[str, Any]] = []
    if suffix in (".jsonl", ".ndjson"):
        fmt = "jsonl"
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{p}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"{p}:{lineno}: each line must be a JSON object")
            rows.append(obj)
    elif suffix == ".csv":
        fmt = "csv"
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise DatasetError(f"unsupported dataset format {suffix!r} (use .jsonl or .csv)")
    prov = Provenance(
        source=str(p),
        source_digest=digest_bytes(raw),
        format=fmt,
        created_at=_now(),
        description=description,
    )
    cases = [_row_to_case(r, fields or {}, f"{p.name}:{i + 1}") for i, r in enumerate(rows)]
    return Dataset(dataset_id or p.stem, cases, prov)
