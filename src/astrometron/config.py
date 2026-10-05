"""``eval.yaml`` parsing and validation.

The config is validated strictly: unknown keys are errors, and literal secrets are
refused (use ``api_key_env: NAME`` instead) so they can never reach a receipt.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .canonical import digest

TOP_KEYS = {
    "name",
    "task",
    "description",
    "dataset",
    "provider",
    "prompt",
    "params",
    "solver",
    "tools",
    "scorer",
    "run",
    "pricing",
    "record_outputs",
    "python_path",
}
DATASET_KEYS = {"path", "id", "revision", "fields", "select", "exclude", "description"}
SELECT_KEYS = {"split", "tags", "case_ids", "limit", "sample"}
RUN_KEYS = {"seed", "concurrency", "timeout_s", "retries", "retry_backoff_s"}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "access_token",
    "bearer",
}


class ConfigError(ValueError):
    pass


def find_secrets(obj: Any, path: str = "") -> list[str]:
    """Paths of keys that look like literal credentials."""
    hits: list[str] = []
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            key = str(k).lower()
            p = f"{path}.{k}" if path else str(k)
            if key in _SECRET_KEYS and isinstance(v, str) and v:
                hits.append(p)
            if key == "headers" and isinstance(v, Mapping):
                hits.extend(f"{p}.{h}" for h in v if str(h).lower() in _SECRET_KEYS)
            hits.extend(find_secrets(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_secrets(v, f"{path}[{i}]"))
    return hits


def _check_keys(d: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {unknown}")


@dataclass(frozen=True)
class EvalConfig:
    raw: dict[str, Any]
    base_dir: Path
    source_path: Path | None = None

    @property
    def digest(self) -> str:
        return digest(self.raw)

    # convenience accessors ----------------------------------------------------
    @property
    def name(self) -> str:
        return str(self.raw.get("name") or "eval")

    @property
    def task(self) -> str | None:
        t = self.raw.get("task")
        return str(t) if t else None

    @property
    def dataset(self) -> dict[str, Any]:
        return dict(self.raw["dataset"])

    @property
    def run(self) -> dict[str, Any]:
        return dict(self.raw.get("run") or {})

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)


def validate(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError("eval config must be a mapping")
    data = copy.deepcopy(dict(raw))
    _check_keys(data, TOP_KEYS, "eval config")
    secrets = find_secrets(data)
    if secrets:
        raise ConfigError(
            f"literal credentials are not allowed in eval config ({secrets}); "
            "use api_key_env: ENV_VAR_NAME"
        )
    for req in ("dataset", "provider", "scorer"):
        if req not in data:
            raise ConfigError(f"eval config is missing required key {req!r}")
    ds = data["dataset"]
    if not isinstance(ds, Mapping) or "path" not in ds:
        raise ConfigError("dataset.path is required")
    _check_keys(ds, DATASET_KEYS, "dataset")
    _check_keys(ds.get("select") or {}, SELECT_KEYS, "dataset.select")
    for ex in ds.get("exclude") or []:
        if not isinstance(ex, Mapping) or not ex.get("case_id") or not ex.get("reason"):
            raise ConfigError("every dataset.exclude entry needs case_id and a non-empty reason")
    _check_keys(data.get("run") or {}, RUN_KEYS, "run")
    run = data.get("run") or {}
    if int(run.get("concurrency", 1)) < 1:
        raise ConfigError("run.concurrency must be >= 1")
    for key in ("provider", "scorer"):
        if not isinstance(data[key], Mapping) or "kind" not in data[key]:
            raise ConfigError(f"{key}.kind is required")
    return data


def load_config(path: str | Path) -> EvalConfig:
    p = Path(path).resolve()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    return EvalConfig(validate(raw), p.parent, p)


def from_dict(raw: Mapping[str, Any], base_dir: str | Path = ".") -> EvalConfig:
    return EvalConfig(validate(raw), Path(base_dir).resolve(), None)
