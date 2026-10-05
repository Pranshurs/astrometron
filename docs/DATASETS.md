# Datasets

An evaluation dataset is a versioned input, not a mutable list of examples.

## Revision

```
revision = sha256( canonical_json({
    "schema":     "astrometron.dataset/v1",
    "dataset_id": <id>,
    "rows":       [[case_id, row_digest], ...]  # sorted by case_id
}))
row_digest = sha256( canonical_json({case_id, input, expected, metadata, tags, split, invalid_reason}) )
```

| Change | Effect |
|---|---|
| Reorder rows | Same revision, same case ids, same row digests. |
| Change one byte of any input, expected value, metadata, tag or split | That row's digest changes, so the revision changes. |
| Add or remove a row | Revision changes. |
| Change description, source path or creation time | No effect. These are *provenance*, recorded beside the revision. |
| Same file, different `dataset_id` | Different revision. |

Datasets with zero cases or duplicate case ids are refused.

## Case identity

- An explicit `id` / `case_id` column (or `fields.id`) is used verbatim.
- Without one, the id is `auto-` plus 16 hex characters of the input's digest. It is
  stable under reordering and changes when the input changes. Two rows with the same
  input and no ids collide and are refused as duplicates.
- Each case record in a receipt carries `case_id`, `row_digest`, `input_digest` and
  `expected_digest`. Comparison pairs cases by `case_id` and only when `row_digest` is
  unchanged.

## Formats

- **JSONL**: one JSON object per line. Reserved keys are `id`/`case_id`, `input`,
  `expected`, `metadata`, `tags`, `split`. Every key not consumed as a field is folded
  into `metadata`, so every byte of a row counts toward its digest. That includes
  reserved names left unused by a `fields` remapping, such as
  `dataset.fields: {id: ticket_id, input: text, expected: label}`.
- **CSV**: same keys as columns. Values are strings. `tags` is `;`-separated.
- **Python**: `astrometron.dataset.load_rows("my-ds", rows)`.

A row without an input is kept as a case with `invalid_reason`. It resolves to
`INVALID_CASE` and still counts in the denominator. The reason never includes a file
name or line number, so the digest stays independent of row order and file path.

## Snapshots and the parent chain

Every run registers a snapshot of the exact dataset it used in the store
(`objects/sha256/…`). Reproduction and verification use the snapshot, not the current
file. If a dataset id is loaded with content matching no registered revision, the new
revision records the previous one as `parent_revision`.

## Pinning

```yaml
dataset:
  path: tickets.jsonl
  id: support-routing-v3
  revision: sha256:bf50b4dd…   # run refuses if the file no longer has this content
```

## Selection

`dataset.select` takes `split`, `tags` (all must match), `case_ids`, `sample`
(seeded, hash-ordered) and `limit` (first n by case id). Selection is deterministic.
The selected ids and their digest are recorded, and verification re-derives them from
the snapshot. Listing an unknown case id is an error, never a silent drop.

## Exclusions

```yaml
dataset:
  exclude:
    - {case_id: tkt-0042, reason: "label disputed, see issue 17"}
```

Excluded cases stay in the receipt as `EXCLUDED` with their reason. The headline
denominator becomes `requested minus N explicitly excluded`, and says so.

## CLI

```
astrometron datasets list
astrometron datasets register data.jsonl --id my-ds
astrometron datasets verify my-ds          # snapshot integrity + has the source file changed?
```
