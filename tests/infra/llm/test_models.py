"""configs/models.yaml loading: the optional per-model max_tokens."""

from __future__ import annotations

from pathlib import Path

import pytest

from escalator.llm.errors import ModelResolutionError
from escalator.llm.models import load_models

BASE = """ollama_min_version: "0.20.2"
models:
  api:
    tier: S
    provider: anthropic
    model: claude-haiku-5-5
    settings: {effort: medium}
"""


def _load(tmp_path: Path, extra: str = ""):
    path = tmp_path / "models.yaml"
    path.write_text(BASE + extra, encoding="utf-8")
    return load_models(path)


def test_max_tokens_defaults_to_none(tmp_path: Path) -> None:
    assert _load(tmp_path).models["api"].max_tokens is None


def test_max_tokens_is_parsed(tmp_path: Path) -> None:
    assert _load(tmp_path, "    max_tokens: 16000\n").models["api"].max_tokens == 16000


@pytest.mark.parametrize("value", ["0", "-1", '"16000"', "16000.0", "true"])
def test_bad_max_tokens_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(ModelResolutionError, match="max_tokens"):
        _load(tmp_path, f"    max_tokens: {value}\n")


def test_only_keeps_the_models_a_run_calls() -> None:
    from escalator.llm.models import load_models

    cfg = load_models()
    sub = cfg.only(["gemma4-31b", "sonnet-5-5"])
    assert sorted(sub.models) == ["gemma4-31b", "sonnet-5-5"] and sub.ollama_min_version == cfg.ollama_min_version
    assert cfg.only([]).models == {}

