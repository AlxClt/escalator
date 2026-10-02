"""`complete(req, context=...)`: the only path to a model. Cache lookup, provider on miss, normalize, price."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping

from escalator.llm import cache as cache_mod
from escalator.llm.cache import Cache
from escalator.llm.cost import PriceTable, usd
from escalator.llm.errors import LLMError, ProviderError, TransientProviderError
from escalator.llm.providers.base import Provider, RawResponse
from escalator.llm.types import Request, Response

MAX_ATTEMPTS = 5
BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0


def provider_name(model: str) -> str:
    if model.startswith("ollama/"):
        return "ollama"
    if model.startswith("claude-"):
        return "anthropic"
    raise LLMError(f"no provider for model {model!r}")


class Adapter:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        cache: Cache,
        prices: PriceTable,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.providers = providers
        self.cache = cache
        self.prices = prices
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.provider_calls = 0  # attempts that reached a provider, retries included

    def complete(self, req: Request, *, context: dict[str, str]) -> Response:
        name = provider_name(req.model)
        provider = self.providers.get(name)
        if provider is None:
            raise LLMError(f"provider {name!r} is not configured")
        k = cache_mod.key(req, context)
        entry = self.cache.get(k)
        if entry is not None:
            if entry.provider != name:
                raise LLMError(f"cache entry {k} was written by {entry.provider}, expected {name}")
            raw, hit = RawResponse(entry.raw, entry.latency_ms), True
        else:
            raw, hit = self._call_with_retry(provider, req), False
            self.cache.put(k, name, raw.body, raw.latency_ms)
        n = provider.normalize(raw.body, req)
        return Response(text=n.text, tool_calls=n.tool_calls, provider_blocks=n.provider_blocks,
                        stop_reason=n.stop_reason, usage=n.usage, usd=usd(n.usage, req.model, self.prices),
                        latency_ms=raw.latency_ms, cache_hit=hit, request_key=k)

    def _call_with_retry(self, provider: Provider, req: Request) -> RawResponse:
        """Exponential backoff with full jitter on 429, 5xx and timeouts. Invisible to the cache."""
        last: TransientProviderError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(self._rng.uniform(0, min(MAX_DELAY_S, BASE_DELAY_S * 2 ** attempt)))
            self.provider_calls += 1
            try:
                return provider.call(req)
            except TransientProviderError as exc:
                last = exc
        raise ProviderError(f"{MAX_ATTEMPTS} attempts failed: {last}") from last
