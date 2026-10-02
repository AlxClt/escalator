"""Provider protocol: wire translation in, raw body out; parsing is a separate pure step."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from escalator.llm.errors import ProviderError, TransientProviderError
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
