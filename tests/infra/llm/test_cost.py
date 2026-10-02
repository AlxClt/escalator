"""U1: raw provider usage -> normalized buckets -> exact Decimal; the price table refuses to guess."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from escalator.llm.cost import DEFAULT_PRICES, PriceTable, usd
from escalator.llm.errors import PriceMissing, PriceTableError, UnexpectedCacheUsage
from escalator.llm.providers.anthropic import AnthropicProvider
from escalator.llm.providers.ollama import OllamaProvider
from escalator.llm.types import Message, Request, Usage

FIXTURES = Path(__file__).parent / "fixtures"
DIGEST = "a" * 64
GEMMA = f"ollama/gemma4:e4b-it-qat@{DIGEST}"
SONNET = "claude-sonnet-5-5"

PRICES = f"""
retrieved_on: 2026-10-02
source: test
unit: usd_per_million_tokens
models:
  {SONNET}: {{input: "2.00", output: "10.00"}}
  {GEMMA}: {{input: "0", output: "0"}}
""".encode()


def _req(model: str, **kw: object) -> Request:
    return Request(model=model, messages=(Message(role="user", content="q"),), max_tokens=1024, **kw)  # type: ignore[arg-type]


def _anthropic() -> AnthropicProvider:
    return AnthropicProvider(client=object())  # type: ignore[arg-type]  # normalize never touches the client


def _ollama() -> OllamaProvider:
    return OllamaProvider(base_url="http://unused", client=object())  # type: ignore[arg-type]


@pytest.fixture
def table() -> PriceTable:
    return PriceTable.parse(PRICES)


def test_sonnet_payload_exact_usd(table: PriceTable) -> None:
    n = _anthropic().normalize((FIXTURES / "anthropic_sonnet_5_5.json").read_bytes(), _req(SONNET))
    # output_tokens includes thinking tokens: no separate thinking bucket exists.
    assert n.usage == Usage(uncached_in=1234, cache_read=0, cache_write=0, out=567)
    assert usd(n.usage, SONNET, table) == Decimal("0.008138")  # 1234*2/1e6 + 567*10/1e6
    assert n.stop_reason == "tool_use"
    assert [b["type"] for b in n.provider_blocks] == ["thinking", "text", "tool_use"]
    assert n.tool_calls[0].args == {"db_id": "superhero"}


def test_gemma_payload_zero_usd_keeps_tokens(table: PriceTable) -> None:
    n = _ollama().normalize((FIXTURES / "ollama_gemma4.json").read_bytes(), _req(GEMMA, num_ctx=16384, think=False))
    assert n.usage == Usage(uncached_in=812, out=45)  # token counts feed the budget model even at $0
    assert usd(n.usage, GEMMA, table) == Decimal("0")
    assert n.stop_reason == "tool_use"
    assert n.tool_calls[0].id == "call_0"
    assert n.tool_calls[0].args == {"sql": "SELECT COUNT(*) FROM superhero"}


def test_ollama_kv_reuse_records_none_not_zero() -> None:
    body = json.loads((FIXTURES / "ollama_gemma4.json").read_bytes())
    del body["prompt_eval_count"]
    n = _ollama().normalize(json.dumps(body).encode(), _req(GEMMA))
    assert n.usage.uncached_in is None


@pytest.mark.parametrize("arguments", ['{"sql": "SELECT 1"', "[1, 2]", "not json"])
def test_malformed_tool_args_kept_raw(arguments: str) -> None:
    body = json.loads((FIXTURES / "ollama_gemma4.json").read_bytes())
    body["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    call = _ollama().normalize(json.dumps(body).encode(), _req(GEMMA)).tool_calls[0]
    assert call.args is None and call.args_raw == arguments


def test_unknown_model_raises(table: PriceTable) -> None:
    with pytest.raises(PriceMissing):
        usd(Usage(uncached_in=1, out=1), "ollama/gemma4:e4b-it-qat@" + "b" * 64, table)


@pytest.mark.parametrize("field", ["cache_read_input_tokens", "cache_creation_input_tokens"])
def test_nonzero_cache_bucket_raises(table: PriceTable, field: str) -> None:
    body = json.loads((FIXTURES / "anthropic_sonnet_5_5.json").read_bytes())
    body["usage"][field] = 100
    n = _anthropic().normalize(json.dumps(body).encode(), _req(SONNET))
    with pytest.raises(UnexpectedCacheUsage):
        usd(n.usage, SONNET, table)


@pytest.mark.parametrize("price", ["2.00", "0.3", "2"])
def test_yaml_float_is_load_error(price: str) -> None:
    with pytest.raises(PriceTableError):
        PriceTable.parse(PRICES.replace(b'input: "2.00"', f"input: {price}".encode()))


def test_repo_price_table_loads() -> None:
    t = PriceTable.load(DEFAULT_PRICES)
    assert t.price(SONNET).input == Decimal("2.00") and t.price(SONNET).output == Decimal("10.00")
    assert len(t.sha256) == 64
