"""Provider-neutral request, response and usage records.

Every parameter sent to a provider is a field of `Request`, so the cache key (a hash of the whole
request) covers everything that changes the output by construction.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

# JSON values carried opaquely (tool schemas, tool arguments, provider blocks). Typed as `object`:
# the adapter never interprets them, and canon.dumps rejects anything non-JSON.
JsonDict = dict[str, object]

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]
Role = Literal["system", "user", "assistant", "tool"]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Usage(Record):
    """Four disjoint token buckets. `None` means the provider did not report the count."""

    uncached_in: int | None
    cache_read: int = 0
    cache_write: int = 0
    out: int | None


class ToolCall(Record):
    id: str
    name: str
    args: JsonDict | None  # None when the arguments are not a JSON object
    args_raw: str


class ToolSpec(Record):
    name: str
    description: str
    input_schema: JsonDict


class Message(Record):
    """Canonical message. Assistant turns carry `provider_blocks` verbatim (Anthropic thinking)."""

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on role="tool"
    provider_blocks: tuple[JsonDict, ...] = ()


class Request(Record):
    """Everything sent to a provider. `None` means not sent."""

    model: str  # exact version, or "ollama/<tag>@<digest>"
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: Literal["auto", "none", "any", "tool"] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int
    stop: tuple[str, ...] | None = None
    seed: int | None = None
    sample_idx: int = 0
    # Ollama
    num_ctx: int | None = None
    think: bool | None = None
    # Anthropic
    thinking: JsonDict | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    parallel_tool_calls: bool | None = None

    @field_validator("tools")
    @classmethod
    def _sort_tools(cls, tools: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
        """Sorted by name before both sending and hashing."""
        return tuple(sorted(tools, key=lambda t: t.name))


class Normalized(Record):
    """What a provider's parser extracts from a raw response body."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    provider_blocks: tuple[JsonDict, ...]
    stop_reason: StopReason
    usage: Usage


class Response(Record):
    text: str
    tool_calls: tuple[ToolCall, ...]
    provider_blocks: tuple[JsonDict, ...]
    stop_reason: StopReason
    usage: Usage
    usd: Decimal  # nominal
    latency_ms: int  # nominal: the stored original on a hit
    cache_hit: bool
    request_key: str

    def as_message(self) -> Message:
        """The assistant turn to append to the conversation, blocks unchanged."""
        return Message(
            role="assistant", content=self.text, tool_calls=self.tool_calls, provider_blocks=self.provider_blocks
        )
