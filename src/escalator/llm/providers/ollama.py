"""Ollama native API (/api/chat): the only route exposing prompt_eval_count, num_ctx and think."""

from __future__ import annotations

import os
import time

import httpx

from escalator.llm.errors import ModelResolutionError, ProviderError, TransientProviderError, UnsupportedParameter
from escalator.llm.providers.base import RawResponse, load_body, raise_for_status, tool_call
from escalator.llm.types import JsonDict, Message, Normalized, Request, StopReason, ToolCall, Usage

PREFIX = "ollama/"
BASE_URL_ENV = "OLLAMA_BASE_URL"  # no default: the server may run on another machine
TIMEOUT_S = 600.0


def model_id(tag: str, digest: str) -> str:
    return f"{PREFIX}{tag}@{digest}"


def split_model_id(model: str) -> tuple[str, str]:
    """`ollama/<tag>@<digest>` -> (tag, digest)."""
    if not model.startswith(PREFIX) or "@" not in model:
        raise ModelResolutionError(f"not an Ollama model id: {model!r}")
    tag, digest = model[len(PREFIX):].rsplit("@", 1)
    return tag, digest


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("-")[0].split(".") if p.isdigit())


def _wire_messages(messages: tuple[Message, ...]) -> list[JsonDict]:
    names: dict[str, str] = {}
    out: list[JsonDict] = []
    for m in messages:
        msg: JsonDict = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            msg["tool_calls"] = [{"function": {"name": c.name, "arguments": c.args if c.args is not None
                                                else c.args_raw}} for c in m.tool_calls]
            names.update({c.id: c.name for c in m.tool_calls})
        if m.role == "tool" and m.tool_call_id is not None and m.tool_call_id in names:
            msg["tool_name"] = names[m.tool_call_id]
        out.append(msg)
    return out


def normalize(body: bytes, req: Request) -> Normalized:
    """Parse a stored or fresh /api/chat body. Needs no server: cached runs parse without a provider."""
    obj = load_body(body)
    msg = obj.get("message")
    msg = msg if isinstance(msg, dict) else {}
    content = msg.get("content")
    calls: list[ToolCall] = []
    raw_calls = msg.get("tool_calls")
    for i, c in enumerate(raw_calls if isinstance(raw_calls, list) else []):
        fn = c.get("function") if isinstance(c, dict) else None
        fn = fn if isinstance(fn, dict) else {}
        name = fn.get("name")
        # Deterministic id: a uuid would enter the next prompt and miss the cache downstream.
        calls.append(tool_call(f"call_{i}", name if isinstance(name, str) else "", fn.get("arguments")))
    p, e = obj.get("prompt_eval_count"), obj.get("eval_count")
    # Context overflow (Ollama truncates silently past num_ctx) is detected by the agent loop from
    # these counts and recorded as an outcome; parsing never raises on model output.
    usage = Usage(uncached_in=p if isinstance(p, int) else None, out=e if isinstance(e, int) else None)
    done = obj.get("done_reason")
    stop: StopReason
    if done == "length":
        stop = "max_tokens"
    elif done == "stop":
        stop = "tool_use" if calls else "end_turn"
    else:
        stop = "other"
    return Normalized(text=content if isinstance(content, str) else "", tool_calls=tuple(calls),
                      provider_blocks=(), stop_reason=stop, usage=usage)


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None) -> None:
        base_url = base_url or os.environ.get(BASE_URL_ENV)
        if not base_url:
            raise ProviderError(f"{BASE_URL_ENV} is not set (see .env.example)")
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=TIMEOUT_S)

    # --- server metadata ----------------------------------------------------

    def version(self) -> str:
        r = self._client.get("/api/version")
        raise_for_status(r.status_code, r.text)
        v = r.json().get("version")
        if not isinstance(v, str):
            raise ModelResolutionError("/api/version returned no version")
        return v

    def tags(self) -> dict[str, str]:
        """Local tag -> full digest, from /api/tags."""
        r = self._client.get("/api/tags")
        raise_for_status(r.status_code, r.text)
        out: dict[str, str] = {}
        for m in r.json().get("models", []):
            if isinstance(m, dict) and isinstance(m.get("name"), str) and isinstance(m.get("digest"), str):
                out[m["name"]] = m["digest"]
        return out

    # --- calls ---------------------------------------------------------------

    def body(self, req: Request) -> JsonDict:
        for field in ("thinking", "effort", "parallel_tool_calls"):
            if getattr(req, field) is not None:
                raise UnsupportedParameter(f"Ollama does not support {field}")
        if req.tool_choice not in (None, "auto"):
            raise UnsupportedParameter(f"Ollama does not support tool_choice={req.tool_choice!r}")
        tag, _ = split_model_id(req.model)
        options: JsonDict = {"num_predict": req.max_tokens}
        for wire, value in (("num_ctx", req.num_ctx), ("temperature", req.temperature), ("top_p", req.top_p),
                            ("seed", req.seed), ("stop", list(req.stop) if req.stop is not None else None)):
            if value is not None:
                options[wire] = value
        body: JsonDict = {"model": tag, "messages": _wire_messages(req.messages), "stream": False,
                          "options": options}
        if req.think is not None:
            body["think"] = req.think
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in req.tools]
        return body

    def call(self, req: Request) -> RawResponse:
        body = self.body(req)
        start = time.monotonic()
        try:
            r = self._client.post("/api/chat", json=body)
        except httpx.TimeoutException as exc:
            raise TransientProviderError(f"timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"transport: {exc}") from exc
        latency_ms = round((time.monotonic() - start) * 1000)
        raise_for_status(r.status_code, r.text)
        return RawResponse(r.content, latency_ms)

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return normalize(body, req)

    def close(self) -> None:
        self._client.close()
