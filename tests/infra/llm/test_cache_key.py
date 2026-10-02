"""U2: every Request field and context entry is in the cache key; ordering noise is not."""

from __future__ import annotations

import pytest

from escalator.llm.cache import key
from escalator.llm.types import Message, Request, ToolCall, ToolSpec

TOOL_A = ToolSpec(name="a_tool", description="A", input_schema={"type": "object", "properties": {"x": {}, "y": {}}})
TOOL_B = ToolSpec(name="b_tool", description="B", input_schema={"type": "object"})
CONTEXT = {"tool_manifest_sha": "f" * 64, "mcp_server_version": "1.0.0"}

BASE = Request(
    model="ollama/gemma4:e4b-it-qat@" + "a" * 64,
    messages=(Message(role="system", content="s"), Message(role="user", content="q")),
    tools=(TOOL_A, TOOL_B),
    max_tokens=1024,
)

# One alternative value per Request field. A new field without an entry here fails the test.
ALTERNATIVES: dict[str, object] = {
    "model": "ollama/gemma4:e4b-it-qat@" + "b" * 64,
    "messages": (Message(role="system", content="s"), Message(role="user", content="q2")),
    "tools": (TOOL_A,),
    "tool_choice": "auto",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 2048,
    "stop": ("\n\n",),
    "seed": 7,
    "sample_idx": 1,
    "num_ctx": 16384,
    "think": False,
    "thinking": {"type": "between_tools"},
    "effort": "medium",
    "parallel_tool_calls": False,
}

MESSAGE_VARIANTS: list[tuple[Message, ...]] = [
    (Message(role="assistant", content="", provider_blocks=({"type": "thinking", "thinking": "t"},)),),
    (Message(role="assistant", content="", tool_calls=(ToolCall(id="call_0", name="x", args={}, args_raw="{}"),)),),
    (Message(role="tool", content="r", tool_call_id="call_0"),),
]


def test_alternatives_cover_every_field() -> None:
    assert set(ALTERNATIVES) == set(Request.model_fields)


@pytest.mark.parametrize("field", sorted(ALTERNATIVES))
def test_each_request_field_changes_key(field: str) -> None:
    changed = BASE.model_copy(update={field: ALTERNATIVES[field]})
    assert key(changed, CONTEXT) != key(BASE, CONTEXT)


@pytest.mark.parametrize("extra", MESSAGE_VARIANTS)
def test_message_content_changes_key(extra: tuple[Message, ...]) -> None:
    changed = BASE.model_copy(update={"messages": BASE.messages + extra})
    assert key(changed, CONTEXT) != key(BASE, CONTEXT)


@pytest.mark.parametrize("entry", sorted(CONTEXT))
def test_each_context_entry_changes_key(entry: str) -> None:
    assert key(BASE, {**CONTEXT, entry: "changed"}) != key(BASE, CONTEXT)


def test_added_context_entry_changes_key() -> None:
    assert key(BASE, {**CONTEXT, "other": "x"}) != key(BASE, CONTEXT)


def test_tool_order_does_not_change_key() -> None:
    permuted = Request(model=BASE.model, messages=BASE.messages, tools=(TOOL_B, TOOL_A), max_tokens=1024)
    assert key(permuted, CONTEXT) == key(BASE, CONTEXT)


def test_dict_key_order_does_not_change_key() -> None:
    schema = {"properties": {"y": {}, "x": {}}, "type": "object"}
    reordered_tool = ToolSpec(name="a_tool", description="A", input_schema=schema)
    permuted = Request(model=BASE.model, messages=BASE.messages, tools=(reordered_tool, TOOL_B), max_tokens=1024)
    reordered_ctx = dict(reversed(list(CONTEXT.items())))
    assert key(permuted, reordered_ctx) == key(BASE, CONTEXT)
