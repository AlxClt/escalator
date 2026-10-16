"""Sampling and max_tokens per model, and the real configs' Anthropic models build a valid request."""

from __future__ import annotations

import anthropic
import pytest
from agent_helpers import LARGE, SMALL

from escalator.agent.loop import LoopConfig, max_tokens_for
from escalator.eval.runner import load_agent_config, sampling
from escalator.llm.cost import PriceTable
from escalator.llm.models import ModelSpec, load_models
from escalator.llm.providers.anthropic import AnthropicProvider
from escalator.llm.types import Message, Request

API_SMALL = ModelSpec("api-small", "S", "anthropic", None, None, "claude-haiku-5-5",
                      {"thinking": {"type": "adaptive"}, "effort": "medium"}, max_tokens=16000)


def test_sampling_follows_the_provider_not_the_tier() -> None:
    assert sampling(SMALL, 7) == (0.0, 7)
    assert sampling(LARGE, 7) == (None, None)
    assert sampling(API_SMALL, 7) == (None, None)  # tier S, but the API rejects T = 0


def test_model_max_tokens_overrides_the_tier() -> None:
    cfg = LoopConfig(max_tokens={"S": 2048, "L": 16000})
    assert max_tokens_for(SMALL, cfg) == 2048
    assert max_tokens_for(LARGE, cfg) == 16000
    assert max_tokens_for(API_SMALL, cfg) == 16000


ANTHROPIC_SPECS = [s for s in load_models().models.values() if s.provider == "anthropic"]


@pytest.mark.parametrize("spec", ANTHROPIC_SPECS, ids=lambda s: s.name)
def test_configured_anthropic_model_is_priced_and_builds_a_valid_body(spec: ModelSpec) -> None:
    """What the runner would send, built offline: a 400-prone field fails here, not on a paid run."""
    PriceTable.load().price(spec.model_id)
    cfg = load_agent_config()
    temperature, seed = sampling(spec, cfg.seed)
    req = Request.model_validate({
        **spec.settings, "model": spec.model_id, "messages": (Message(role="user", content="q"),),
        "temperature": temperature, "seed": seed, "max_tokens": max_tokens_for(spec, cfg.loop),
    })
    body = AnthropicProvider(client=anthropic.Anthropic(api_key="offline")).body(req)
    assert "temperature" not in body and "top_p" not in body
    assert body["max_tokens"] == max_tokens_for(spec, cfg.loop) >= 16000
    assert body["output_config"] == {"effort": "medium"}


def test_sonnet_and_haiku_are_configured() -> None:
    names = {s.name for s in ANTHROPIC_SPECS}
    assert {"sonnet-5-5", "haiku-5-5"} <= names
    presets = load_agent_config().presets
    assert set().union(*(p.models for p in presets.values())) >= names  # every API model is run by some preset
    assert "opus-5-5" in presets["challenging"].models and "opus-5-5" not in presets["baselines"].models
