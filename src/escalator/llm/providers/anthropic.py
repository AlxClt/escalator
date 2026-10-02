"""Anthropic Messages API through the official SDK, raw body kept for the cache.

The request body is built here (not via typed SDK params) so that exactly the `Request` fields are
sent; the SDK supplies auth, versioning and typed errors. SDK retries are off: the adapter retries.
"""

from __future__ import annotations

import os
import time

import anthropic

from escalator.llm.errors import ProviderError, TransientProviderError, UnsupportedParameter
from escalator.llm.providers.base import RawResponse, load_body, raise_for_status, tool_call
from escalator.llm.types import JsonDict, Message, Normalized, Request, StopReason, ToolCall, Usage

API_KEY_ENV = "ESCALATOR_ANTHROPIC_KEY"
TIMEOUT_S = 600.0

_STOP: dict[str, StopReason] = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
                                "refusal": "refusal"}


def validate(req: Request) -> None:
    """Sonnet 5.5 returns 400 for these; fail before sending. `seed` stays in the key, never sent."""
    for field in ("temperature", "top_p", "num_ctx", "think"):
        if getattr(req, field) is not None:
            raise UnsupportedParameter(f"{req.model} does not accept {field}")
    if req.tool_choice in ("any", "tool"):
        raise UnsupportedParameter(f"{req.model} does not accept tool_choice={req.tool_choice!r}")
    if req.thinking is not None and req.thinking.get("type") == "disabled":
        raise UnsupportedParameter(f"{req.model} does not accept thinking disabled")
    if req.parallel_tool_calls:
        raise UnsupportedParameter("parallel tool calls are disabled (one tool call per turn)")


def _content(m: Message) -> list[JsonDict]:
    if m.role == "tool":
        return [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]
    if m.role == "assistant":
        if m.provider_blocks:  # verbatim round-trip: thinking blocks must not be edited
            return list(m.provider_blocks)
        blocks: list[JsonDict] = [{"type": "text", "text": m.content}] if m.content else []
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args or {}} for c in m.tool_calls]
        return blocks
    return [{"type": "text", "text": m.content}]


def wire_messages(messages: tuple[Message, ...]) -> tuple[str | None, list[JsonDict]]:
    """System text, and messages with tool results as user turns and adjacent same-role turns merged."""
    system = [m.content for m in messages if m.role == "system"]
    out: list[JsonDict] = []
    for m in messages:
        if m.role == "system":
            continue
        role = "assistant" if m.role == "assistant" else "user"
        blocks = _content(m)
        if out and out[-1]["role"] == role:
            prev = out[-1]["content"]
            assert isinstance(prev, list)
            prev.extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return ("\n\n".join(system) if system else None), out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        if client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise ProviderError(f"{API_KEY_ENV} is not set")
            client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=TIMEOUT_S)
        self._client = client

    def body(self, req: Request) -> JsonDict:
        validate(req)
        system, messages = wire_messages(req.messages)
        body: JsonDict = {"model": req.model, "max_tokens": req.max_tokens, "messages": messages}
        if system is not None:
            body["system"] = system
        if req.stop is not None:
            body["stop_sequences"] = list(req.stop)
        if req.thinking is not None:
            body["thinking"] = req.thinking
        if req.effort is not None:
            body["output_config"] = {"effort": req.effort}
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                             for t in req.tools]
            body["tool_choice"] = ({"type": "none"} if req.tool_choice == "none"
                                   else {"type": "auto", "disable_parallel_tool_use": True})
        # No cache_control anywhere (D3).
        return body

    def call(self, req: Request) -> RawResponse:
        body = self.body(req)
        start = time.monotonic()
        try:
            # Raw response: the body is never parsed by the SDK, so the bytes are what the API sent.
            # `body` is a plain dict built and validated by self.body(); the SDK's TypedDict params
            # cannot express it statically.
            r = self._client.messages.with_raw_response.create(**body)  # pyright: ignore[reportArgumentType, reportCallIssue]
        except anthropic.APIStatusError as exc:
            raise_for_status(exc.status_code, str(exc))
            raise ProviderError(str(exc)) from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise TransientProviderError(str(exc)) from exc
        latency_ms = round((time.monotonic() - start) * 1000)
        raise_for_status(r.status_code, r.http_response.text)
        return RawResponse(r.http_response.content, latency_ms)

    def normalize(self, body: bytes, req: Request) -> Normalized:
        obj = load_body(body)
        content = obj.get("content")
        blocks: list[JsonDict] = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for b in blocks:
            if b.get("type") == "text" and isinstance(b.get("text"), str):
                texts.append(str(b["text"]))
            elif b.get("type") == "tool_use":
                name = b.get("name")
                calls.append(tool_call(str(b.get("id", "")), name if isinstance(name, str) else "", b.get("input")))
        u = obj.get("usage")
        u = u if isinstance(u, dict) else {}

        def count(k: str) -> int | None:
            v = u.get(k)
            return v if isinstance(v, int) else None

        usage = Usage(uncached_in=count("input_tokens"), cache_read=count("cache_read_input_tokens") or 0,
                      cache_write=count("cache_creation_input_tokens") or 0, out=count("output_tokens"))
        stop = obj.get("stop_reason")
        return Normalized(text="".join(texts), tool_calls=tuple(calls), provider_blocks=tuple(blocks),
                          stop_reason=_STOP.get(stop, "other") if isinstance(stop, str) else "other", usage=usage)
