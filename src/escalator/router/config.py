"""configs/router.yaml: self-consistency and verifier collection settings, the run inventory the
simulation reads, and the bootstrap settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from escalator.datasets.config import REPO_ROOT

DEFAULT_ROUTER_CONFIG = REPO_ROOT / "configs" / "router.yaml"


class RouterConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SelfConsistencyConfig:
    k: int
    temperature: float
    seeds: tuple[int, ...]  # seeds[j] for sample j: explicit, pairwise distinct
    models: tuple[str, ...]


@dataclass(frozen=True)
class VerifierConfig:
    prompt_version: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class SmallRuns:
    baseline: str
    self_consistency: str | None
    verifier: str | None


@dataclass(frozen=True)
class FrontierRuns:
    model: str
    baseline: str
    repeat: str | None


@dataclass(frozen=True)
class BootstrapConfig:
    B: int
    seed: int


@dataclass(frozen=True)
class RouterConfig:
    self_consistency: SelfConsistencyConfig
    verifier: VerifierConfig
    small: dict[str, SmallRuns]  # small model name -> its runs, in config order
    frontier: FrontierRuns
    bootstrap: BootstrapConfig
    headline_small_tier: str | None  # no default: set by hand
    # Task set of the study; None = the pinned 200-task sample and its files.
    manifest: Path | None = None  # data/manifest.json
    scoring: Path | None = None  # configs/scoring.yaml
    baselines: Path | None = None  # results/baselines.json (integrity check)


def _map(raw: object, where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RouterConfigError(f"{where} must be a mapping")
    return {str(k): v for k, v in raw.items()}


def _int(raw: dict[str, object], key: str, where: str, *, minimum: int = 0) -> int:
    v = raw.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise RouterConfigError(f"{where}.{key} must be an integer >= {minimum}")
    return v


def _str(raw: dict[str, object], key: str, where: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v:
        raise RouterConfigError(f"{where}.{key} must be a non-empty string")
    return v


def _opt_str(raw: dict[str, object], key: str, where: str) -> str | None:
    if key not in raw:
        raise RouterConfigError(f"{where}.{key} must be given (null when not collected)")
    v = raw[key]
    if v is None:
        return None
    if not isinstance(v, str) or not v:
        raise RouterConfigError(f"{where}.{key} must be a run id or null")
    return v


def _names(raw: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    v = raw.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(set(v)) != len(v):
        raise RouterConfigError(f"{where}.{key} must be a list of distinct model names")
    return tuple(v)


def parse_router_config(raw: object, where: str = "router.yaml") -> RouterConfig:
    top = _map(raw, where)
    sc_raw = _map(top.get("self_consistency"), f"{where}: self_consistency")
    k = _int(sc_raw, "k", "self_consistency", minimum=2)
    t = sc_raw.get("temperature")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t < 0:
        raise RouterConfigError("self_consistency.temperature must be a number >= 0")
    seeds = sc_raw.get("seeds")
    if (not isinstance(seeds, list) or len(seeds) != k
            or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds) or len(set(seeds)) != k):
        raise RouterConfigError(f"self_consistency.seeds must be {k} pairwise distinct integers")
    sc = SelfConsistencyConfig(k, float(t), tuple(seeds), _names(sc_raw, "models", "self_consistency"))

    v_raw = _map(top.get("verifier"), f"{where}: verifier")
    verifier = VerifierConfig(_str(v_raw, "prompt_version", "verifier"), _names(v_raw, "models", "verifier"))

    inv = _map(top.get("inventory"), f"{where}: inventory")
    small: dict[str, SmallRuns] = {}
    for name, entry in _map(inv.get("small"), "inventory.small").items():
        e = _map(entry, f"inventory.small.{name}")
        w = f"inventory.small.{name}"
        small[name] = SmallRuns(_str(e, "baseline", w), _opt_str(e, "self_consistency", w), _opt_str(e, "verifier", w))
    if not small:
        raise RouterConfigError("inventory.small lists no small model")
    f = _map(inv.get("frontier"), "inventory.frontier")
    frontier = FrontierRuns(_str(f, "model", "inventory.frontier"), _str(f, "baseline", "inventory.frontier"),
                            _opt_str(f, "repeat", "inventory.frontier"))

    b = _map(top.get("bootstrap"), f"{where}: bootstrap")
    bootstrap = BootstrapConfig(_int(b, "B", "bootstrap", minimum=1), _int(b, "seed", "bootstrap"))

    headline = top.get("headline_small_tier")
    if headline is not None and (not isinstance(headline, str) or headline not in small):
        raise RouterConfigError(f"headline_small_tier must name a model of inventory.small, got {headline!r}")
    paths: dict[str, Path | None] = {}
    for key in ("manifest", "scoring", "baselines"):
        value = top.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise RouterConfigError(f"{key} must be a path relative to the repo root")
        paths[key] = None if value is None else REPO_ROOT / value
    return RouterConfig(sc, verifier, small, frontier, bootstrap, headline, paths["manifest"], paths["scoring"],
                        paths["baselines"])


def load_router_config(path: Path = DEFAULT_ROUTER_CONFIG) -> RouterConfig:
    return parse_router_config(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
