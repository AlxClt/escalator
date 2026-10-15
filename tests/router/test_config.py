"""configs/router.yaml validation."""

from __future__ import annotations

import copy

import pytest

from escalator.router.config import RouterConfigError, load_router_config, parse_router_config

BASE: dict[str, object] = {
    "self_consistency": {"k": 3, "temperature": 0.7, "seeds": [1, 2, 3], "models": ["a"]},
    "verifier": {"prompt_version": "verifier_v1", "models": ["a"]},
    "inventory": {"small": {"a": {"baseline": "ba", "self_consistency": None, "verifier": None}},
                  "frontier": {"model": "f", "baseline": "bf", "repeat": None}},
    "bootstrap": {"B": 2000, "seed": 7},
}


def _with(path: list[str], value: object) -> dict[str, object]:
    raw = copy.deepcopy(BASE)
    node: dict[str, object] = raw
    for key in path[:-1]:
        nxt = node[key]
        assert isinstance(nxt, dict)
        node = nxt
    node[path[-1]] = value
    return raw


def test_repo_config_loads_without_headline() -> None:
    cfg = load_router_config()
    assert cfg.headline_small_tier is None
    assert cfg.self_consistency.k == len(cfg.self_consistency.seeds) == len(set(cfg.self_consistency.seeds))
    assert list(cfg.small) == ["gemma4-e4b", "gemma4-12b", "gemma4-31b", "haiku-5-5"]


@pytest.mark.parametrize(("path", "value"), [
    (["self_consistency", "seeds"], [1, 1, 2]),
    (["self_consistency", "seeds"], [1, 2]),
    (["self_consistency", "k"], 1),
    (["self_consistency", "temperature"], "hot"),
    (["inventory", "small", "a", "baseline"], None),
    (["bootstrap", "B"], 0),
    (["headline_small_tier"], "not-a-small-model"),
])
def test_invalid_values_are_rejected(path: list[str], value: object) -> None:
    with pytest.raises(RouterConfigError):
        parse_router_config(_with(path, value))


def test_inventory_roles_must_be_explicit() -> None:
    raw = copy.deepcopy(BASE)
    inv = raw["inventory"]
    assert isinstance(inv, dict)
    del inv["small"]["a"]["verifier"]
    with pytest.raises(RouterConfigError, match="must be given"):
        parse_router_config(raw)
