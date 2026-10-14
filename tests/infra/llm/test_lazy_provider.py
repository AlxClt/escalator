"""LazyProvider: a provider built and checked only at the first cache miss, so a fully cached run
needs no live provider (no server, no env var, no key)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from escalator.llm.adapter import Adapter
from escalator.llm.cache import Cache
from escalator.llm.cost import PriceTable
from escalator.llm.errors import ModelResolutionError, ProviderError, ProviderSetupError
from escalator.llm.providers import ollama
from escalator.llm.providers.base import LazyProvider, RawResponse
from escalator.llm.types import Message, Normalized, Request

MODEL = "ollama/fake:tag@" + "d" * 64
PRICES = PriceTable.parse(f"""
retrieved_on: 2026-10-02
source: test
unit: usd_per_million_tokens
models:
  {MODEL}: {{input: "1.00", output: "4.00"}}
""".encode())
BODY = json.dumps({"done": True, "done_reason": "stop", "message": {"role": "assistant", "content": "pong"},
                   "prompt_eval_count": 10, "eval_count": 2}).encode()


def request(text: str = "ping") -> Request:
    return Request(model=MODEL, messages=(Message(role="user", content=text),), max_tokens=16)


class FakeOllama:
    name = "ollama"

    def __init__(self) -> None:
        self.calls = 0

    def call(self, req: Request) -> RawResponse:
        self.calls += 1
        return RawResponse(BODY, 42)

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return ollama.normalize(body, req)


class Counter:
    def __init__(self) -> None:
        self.factory = 0
        self.check = 0
        self.inner = FakeOllama()

    def build(self) -> FakeOllama:
        self.factory += 1
        return self.inner

    def verify(self, _p: FakeOllama) -> str:
        self.check += 1
        return "0.20.9"


def unreachable() -> FakeOllama:
    raise AssertionError("a cached run must not build the provider")


def test_normalize_never_builds_the_provider() -> None:
    lazy = LazyProvider("ollama", unreachable, ollama.normalize)
    assert lazy.normalize(BODY, request()).text == "pong"
    assert not lazy.started and lazy.server_version is None


def test_first_call_builds_and_checks_once() -> None:
    c = Counter()
    lazy = LazyProvider("ollama", c.build, ollama.normalize, check=c.verify)
    lazy.call(request())
    lazy.call(request("again"))
    assert (c.factory, c.check, c.inner.calls) == (1, 1, 2)
    assert lazy.started and lazy.server_version == "0.20.9"


def fail_build() -> FakeOllama:
    raise ProviderError("OLLAMA_BASE_URL is not set")


def fail_check(_p: FakeOllama) -> str:
    raise ModelResolutionError("fake:tag: live digest e != pinned d")


@pytest.mark.parametrize(("build", "check"), [(fail_build, None), (FakeOllama, fail_check)],
                         ids=["factory", "check"])
def test_setup_failure_is_not_a_provider_error(build: object, check: object) -> None:
    lazy = (LazyProvider("ollama", fail_build, ollama.normalize) if check is None
            else LazyProvider("ollama", FakeOllama, ollama.normalize, check=fail_check))
    with pytest.raises(ProviderSetupError) as info:
        lazy.call(request())
    # Not a ProviderError: the loop must not record a configuration failure as an outage.
    assert not isinstance(info.value, ProviderError)
    assert not lazy.started


def test_adapter_all_hits_never_builds_the_provider(tmp_path: Path) -> None:
    path = tmp_path / "llm.sqlite"
    c = Counter()
    first = Adapter({"ollama": LazyProvider("ollama", c.build, ollama.normalize, check=c.verify)}, Cache(path),
                    PRICES)
    first.complete(request(), context={})
    assert c.factory == 1

    lazy = LazyProvider("ollama", unreachable, ollama.normalize, check=fail_check)
    second = Adapter({"ollama": lazy}, Cache(path), PRICES)
    r = second.complete(request(), context={})
    assert r.cache_hit and r.text == "pong" and second.provider_calls == 0
    assert not lazy.started
