"""configs/models.yaml: pinned tiers, their exact model ids and request settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from escalator.datasets.config import REPO_ROOT
from escalator.llm.errors import ModelResolutionError
from escalator.llm.providers import ollama
from escalator.llm.providers.ollama import OllamaProvider
from escalator.llm.types import JsonDict

DEFAULT_MODELS = REPO_ROOT / "configs" / "models.yaml"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    tier: Literal["S", "L"]
    provider: Literal["ollama", "anthropic"]
    tag: str | None  # Ollama
    digest: str | None  # Ollama, full
    model: str | None  # Anthropic exact version
    settings: JsonDict

    @property
    def model_id(self) -> str:
        if self.provider == "anthropic":
            assert self.model is not None
            return self.model
        if self.tag is None or not self.digest:
            raise ModelResolutionError(f"{self.name}: digest not pinned; run `python -m escalator.llm pin`")
        return ollama.model_id(self.tag, self.digest)


@dataclass(frozen=True)
class ModelsConfig:
    ollama_min_version: str
    models: dict[str, ModelSpec]


def load_models(path: Path = DEFAULT_MODELS) -> ModelsConfig:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ModelResolutionError(f"{path}: expected a mapping with `models`")
    min_v = raw.get("ollama_min_version")
    if not isinstance(min_v, str):
        raise ModelResolutionError(f"{path}: ollama_min_version must be a string")
    specs: dict[str, ModelSpec] = {}
    for name, e in raw["models"].items():
        if not isinstance(e, dict) or e.get("tier") not in ("S", "L") or e.get("provider") not in ("ollama", "anthropic"):
            raise ModelResolutionError(f"{path}: models.{name} needs tier S|L and provider ollama|anthropic")
        settings = e.get("settings") or {}
        if not isinstance(settings, dict):
            raise ModelResolutionError(f"{path}: models.{name}.settings must be a mapping")
        tag, digest, model = e.get("tag"), e.get("digest"), e.get("model")
        if e["provider"] == "ollama" and not isinstance(tag, str):
            raise ModelResolutionError(f"{path}: models.{name} needs a tag")
        if e["provider"] == "anthropic" and not isinstance(model, str):
            raise ModelResolutionError(f"{path}: models.{name} needs an exact model version")
        specs[str(name)] = ModelSpec(str(name), e["tier"], e["provider"], tag if isinstance(tag, str) else None,
                                     digest if isinstance(digest, str) else None,
                                     model if isinstance(model, str) else None,
                                     {str(k): v for k, v in settings.items()})
    return ModelsConfig(min_v, specs)


def check_ollama(cfg: ModelsConfig, provider: OllamaProvider) -> str:
    """Before the first provider call: server version >= minimum and every pinned tag still points at
    its digest. Cache hits skip it (the runner calls it at the first miss): their key holds the digest.

    Returns the server version, recorded in RunMeta.
    """
    version = provider.version()
    if ollama.parse_version(version) < ollama.parse_version(cfg.ollama_min_version):
        raise ModelResolutionError(f"Ollama {version} < {cfg.ollama_min_version}")
    live = provider.tags()
    for spec in cfg.models.values():
        if spec.provider != "ollama":
            continue
        model_id = spec.model_id  # raises if unpinned
        assert spec.tag is not None
        if live.get(spec.tag) != spec.digest:
            raise ModelResolutionError(f"{spec.tag}: live digest {live.get(spec.tag)} != pinned {spec.digest}"
                                       f" ({model_id})")
    return version
