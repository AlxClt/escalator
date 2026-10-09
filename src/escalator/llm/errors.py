"""Typed failures of the LLM layer. Nothing here falls back to a default silently."""

from __future__ import annotations


class LLMError(RuntimeError):
    pass


class PriceTableError(LLMError):
    """configs/prices.yaml is malformed (e.g. a price written as a YAML float)."""


class PriceMissing(LLMError):
    """No price for this exact model id; a missing entry is never 0."""


class UnexpectedCacheUsage(LLMError):
    """The provider reported prompt-cache tokens although none were requested (D3)."""


class CacheMiss(LLMError):
    """A readonly cache has no entry for the request key."""


class ProviderError(LLMError):
    """The provider failed for good: a non-retryable 4xx, or retries exhausted."""


class TransientProviderError(ProviderError):
    """429, 5xx or a timeout: retried by the adapter, never cached."""


class UnsupportedParameter(LLMError):
    """The request sets a parameter the target model rejects; raised before sending."""


class ModelResolutionError(LLMError):
    """An Ollama tag could not be resolved to the pinned digest."""
