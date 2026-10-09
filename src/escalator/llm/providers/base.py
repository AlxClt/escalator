"""Provider protocol: wire translation in, raw body out; parsing is a separate pure step."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from escalator.llm.errors import ProviderError, ProviderSetupError, TransientProviderError
from escalator.llm.types import JsonDict, Normalized, Request, ToolCall
from escalator.util import canon


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    latency_ms: int


class Provider(Protocol):
    name: str

    def call(self, req: Request) -> RawResponse:
        """One attempt. Raises TransientProviderError (retryable) or ProviderError."""
        ...

    def normalize(self, body: bytes, req: Request) -> Normalized:
        """Parse a stored or fresh raw body. Never raises on malformed model output."""
        ...


class LazyProvider[P: Provider]:
    """A provider built, and checked, only before its first call: at the first cache miss.

    Cached bodies are parsed with `parse`, which needs no server, key or network, so a fully cached
    run never builds the provider. Hits are safe without the check: the cache key holds the exact
    model id (Ollama digest included), and every entry was written after a check in its process.
    """

    def __init__(self, name: str, factory: Callable[[], P], parse: Callable[[bytes, Request], Normalized],
                 check: Callable[[P], str | None] = lambda _p: None) -> None:
        self.name = name
        self._factory = factory
        self._parse = parse
        self._check = check
        self._inner: P | None = None
        self.server_version: str | None = None  # from check(), once the provider was built

    @property
    def started(self) -> bool:
        return self._inner is not None

    def call(self, req: Request) -> RawResponse:
        if self._inner is None:
            try:
                inner = self._factory()
                self.server_version = self._check(inner)
            except Exception as exc:
                raise ProviderSetupError(f"{self.name}: provider setup failed: {exc}") from exc
            self._inner = inner
        return self._inner.call(req)

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return self._parse(body, req)


def raise_for_status(status: int, text: str) -> None:
    if status == 429 or status >= 500:
        raise TransientProviderError(f"HTTP {status}: {text[:500]}")
    if status >= 400:
        raise ProviderError(f"HTTP {status}: {text[:500]}")


def tool_call(call_id: str, name: str, arguments: object) -> ToolCall:
    """Arguments as given: a JSON object is kept; a string is parsed; anything else yields args=None."""
    if isinstance(arguments, str):
        raw = arguments
        try:
            parsed: object = json.loads(arguments)
        except ValueError:
            parsed = None
    else:
        parsed = arguments
        try:
            raw = canon.dumps(arguments).decode("utf-8")
        except (TypeError, ValueError):
            raw = repr(arguments)
    args: JsonDict | None = None
    if isinstance(parsed, dict):
        args = {str(k): v for k, v in parsed.items()}
    return ToolCall(id=call_id, name=name, args=args, args_raw=raw)


def load_body(body: bytes) -> JsonDict:
    """A stored body is a JSON object by construction (only 2xx bodies are cached)."""
    obj: object = json.loads(body)
    if not isinstance(obj, dict):
        raise ProviderError("provider body is not a JSON object")
    return {str(k): v for k, v in obj.items()}
