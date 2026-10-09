"""Agent loop against a real MCP server subprocess and a scripted provider (spec tests 1-9, 14)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from agent_helpers import (
    DB_ID,
    LARGE,
    SMALL,
    Call,
    ScriptedProvider,
    Server,
    anthropic_body,
    anthropic_content,
    by_step,
    make_adapter,
    ollama_body,
    run_one,
    sql_args,
    step_of,
)

from escalator.agent.loop import LoopConfig, TaskOutcome
from escalator.agent.tools import IGNORED_AFTER_SUBMIT
from escalator.llm.models import ModelSpec
from escalator.llm.providers.anthropic import AnthropicProvider
from escalator.trace.schema import StepRecord

HEX64 = re.compile(r"[0-9a-f]{64}")
GOOD: Call = ("execute_sql", sql_args("SELECT 1"))
BAD: Call = ("execute_sql", {"db_id": DB_ID})  # missing sql
SUBMIT: Call = ("submit_answer", sql_args("SELECT COUNT(*) FROM player"))


def steps(*turns: list[Call]) -> ScriptedProvider:
    return ScriptedProvider("ollama", by_step(*(ollama_body(t) for t in turns)))


def run(server: Server, tmp_path: Path, provider: ScriptedProvider, *, tier: ModelSpec = SMALL,
        config: LoopConfig | None = None) -> TaskOutcome:
    return run_one(server, make_adapter(provider, server.cache(tmp_path / "llm.sqlite")), tier=tier, config=config)


def tool_message(provider: ScriptedProvider, step: int, back: int = 1) -> str:
    """Content of a tool result the model saw in the request of `step`."""
    msg = provider.requests[step].messages[-back]
    assert msg.role == "tool"
    return msg.content


# --- 1. happy path -------------------------------------------------------------


def test_happy_path(server: Server, tmp_path: Path) -> None:
    provider = steps([("get_schema", {"db_id": DB_ID})], [("execute_sql", sql_args("SELECT COUNT(*) FROM player"))],
                     [SUBMIT])
    o = run(server, tmp_path, provider)

    assert o.end == "submitted"
    assert o.submitted_sql == "SELECT COUNT(*) FROM player" and o.submit_status == "ok"
    assert [r.tool for r in o.records] == [["get_schema"], ["execute_sql"], ["submit_answer"]]
    assert [r.end for r in o.records] == [None, None, "submitted"]
    assert [r.step for r in o.records] == [0, 1, 2]
    for r in o.records:
        assert r.schema_version == 2 and r.run_id == "test" and r.task_id == "1" and r.tier == "S"
        assert HEX64.fullmatch(r.request_key) and r.cache_hit is False and r.stop_reason == "tool_use"
        assert r.n_tool_calls == 1 and r.tool_status == ["ok"] and r.args_raw == [None]
        assert r.result_hash[0] is not None and HEX64.fullmatch(r.result_hash[0])
        assert r.result_preview[0] is not None
        assert r.tokens_in == 100 and r.tokens_out == 20 and r.tokens_cache_read == 0 and r.tokens_cache_write == 0
        assert r.latency_ms is not None and r.usd is not None
    # The schema card in the prompt is the server's get_schema output, byte for byte.
    system = provider.requests[0].messages[0].content
    assert tool_message(provider, 1) in system and "## player" in system
    assert json.loads(tool_message(provider, 2))["rows"] == [[5]]
    # The submit result is recorded for the trace but never shown to the model.
    assert provider.calls == 3


# --- 2. typed validation errors ------------------------------------------------------


@pytest.mark.parametrize(("call", "reason"), [
    (("execute_sql", '{"db_id": "fixture", "sql": '), "unparsable_arguments"),
    (("drop_table", {"db_id": DB_ID}), "unknown_tool"),
    (("execute_sql", {"db_id": DB_ID}), "schema"),
    (("execute_sql", {"db_id": "nope", "sql": "SELECT 1"}), "schema"),
    (("sample_rows", {"db_id": DB_ID, "table": "team", "extra": 1}), "schema"),
], ids=["unparsable", "unknown_tool", "missing_arg", "bad_enum", "extra_arg"])
def test_invalid_call_is_typed_validation_error(server: Server, tmp_path: Path, call: Call, reason: str) -> None:
    provider = steps([call], [SUBMIT])
    o = run(server, tmp_path, provider)

    first = o.records[0]
    assert first.tool_status == ["validation_error"] and first.tool == [call[0]]
    assert o.n_validation_failures == 1 and o.end == "submitted"
    error = json.loads(tool_message(provider, 1))
    assert error["error_type"] == "validation_error" and error["tool"] == call[0]
    assert error["detail"]["reason"] == reason
    if reason == "unparsable_arguments":
        assert first.args == [None] and first.args_raw == [call[1]]
    if reason == "schema":
        assert error["detail"]["errors"] and all({"path", "message"} == set(e) for e in error["detail"]["errors"])


# --- 3. retry cap --------------------------------------------------------------------


@pytest.mark.parametrize(("turns", "end", "n_invalid"), [
    ([BAD, BAD, GOOD, SUBMIT], "submitted", 2),
    ([BAD, BAD, BAD], "validation_exhausted", 3),
    ([BAD, BAD, GOOD, BAD, BAD, SUBMIT], "submitted", 4),
    ([BAD, BAD, GOOD, BAD, BAD, BAD], "validation_exhausted", 5),
], ids=["two_then_valid", "three_in_a_row", "reset_then_submit", "reset_then_three"])
def test_retry_cap(server: Server, tmp_path: Path, turns: list[Call], end: str, n_invalid: int) -> None:
    o = run(server, tmp_path, steps(*([t] for t in turns)))
    assert len(o.records) == len(turns)
    assert o.end == end and o.records[-1].end == end
    assert all(r.end is None for r in o.records[:-1])
    assert o.n_validation_failures == n_invalid


# --- 4. SQL errors are execution errors ---------------------------------------------------


@pytest.mark.parametrize(("call", "status", "kind"), [
    (("execute_sql", sql_args("SELECT nope FROM player")), "execution_error", "sql_error"),
    (("sample_rows", {"db_id": DB_ID, "table": "nope"}), "execution_error", "unknown_table"),
    (("execute_sql", sql_args("SELECT random()")), "denied", "forbidden"),
])
def test_sql_error_is_execution_error(server: Server, tmp_path: Path, call: Call, status: str, kind: str) -> None:
    provider = steps([call], [SUBMIT])
    o = run(server, tmp_path, provider)
    assert o.records[0].tool_status == [status]
    assert o.n_validation_failures == 0 and len(o.records) == 2 and o.end == "submitted"
    assert json.loads(tool_message(provider, 1))["error"] == kind


def test_submitted_query_that_errors_is_visible_in_trace(server: Server, tmp_path: Path) -> None:
    o = run(server, tmp_path, steps([("submit_answer", sql_args("SELECT nope"))]))
    assert o.end == "submitted" and o.submit_status == "execution_error"
    assert o.records[-1].tool_status == ["execution_error"]


# --- 5-6. max_steps and no_tool_call --------------------------------------------------------


def test_max_steps_at_exactly_eight_calls(server: Server, tmp_path: Path) -> None:
    provider = steps([GOOD])
    o = run(server, tmp_path, provider)
    assert provider.calls == 8 and len(o.records) == 8
    assert o.end == "max_steps" and [r.end for r in o.records] == [None] * 7 + ["max_steps"]


def test_text_only_response_is_no_tool_call(server: Server, tmp_path: Path) -> None:
    provider = ScriptedProvider("ollama", by_step(ollama_body(text="There are 5 players.")))
    o = run(server, tmp_path, provider)
    assert o.end == "no_tool_call" and len(o.records) == 1 and provider.calls == 1
    r = o.records[0]
    assert r.n_tool_calls == 0 and r.tool == [] and r.text == "There are 5 players." and r.stop_reason == "end_turn"


# --- 7. several calls in one response -----------------------------------------------------


def test_calls_after_submit_are_ignored(server: Server, tmp_path: Path) -> None:
    o = run(server, tmp_path, steps([GOOD, SUBMIT, ("execute_sql", sql_args("SELECT 2"))]))
    assert o.end == "submitted" and len(o.records) == 1
    r = o.records[0]
    assert r.n_tool_calls == 3 and o.n_tool_calls == 3
    assert r.tool == ["execute_sql", "submit_answer", "execute_sql"]
    assert r.tool_status == ["ok", "ok", "ignored"]
    assert r.result_preview[2] == IGNORED_AFTER_SUBMIT


def test_several_calls_execute_in_order(server: Server, tmp_path: Path) -> None:
    provider = steps([GOOD, ("sample_rows", {"db_id": DB_ID, "table": "team"}), BAD], [SUBMIT])
    o = run(server, tmp_path, provider)
    first = o.records[0]
    assert first.tool_status == ["ok", "ok", "validation_error"] and o.n_validation_failures == 1
    tool_msgs = [m for m in provider.requests[1].messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["call_0", "call_1", "call_2"]
    assert json.loads(tool_msgs[1].content)["columns"] == ["id", "name", "city"]


def test_invalid_call_then_submit_in_one_response(server: Server, tmp_path: Path) -> None:
    o = run(server, tmp_path, steps([BAD, SUBMIT]))
    assert o.end == "submitted" and o.records[0].tool_status == ["validation_error", "ok"]


# --- 8. context overflow ------------------------------------------------------------------


@pytest.mark.parametrize(("prompt_tokens", "out_tokens", "margin", "overflow"), [
    (16384 - 1024, 20, 1024, True),
    (16384 - 1025, 20, 1024, False),
    (16000, 384, 0, True),  # prompt + output fill num_ctx
    (16000, 383, 0, False),
])
def test_context_overflow(server: Server, tmp_path: Path, prompt_tokens: int, out_tokens: int, margin: int,
                          overflow: bool) -> None:
    provider = ScriptedProvider("ollama", by_step(
        ollama_body([GOOD], prompt_tokens=prompt_tokens, out_tokens=out_tokens), ollama_body([SUBMIT])))
    o = run(server, tmp_path, provider, config=LoopConfig(overflow_margin=margin))
    if overflow:
        assert o.end == "context_overflow" and len(o.records) == 1
        assert o.records[0].tool_status == ["ignored"] and o.records[0].result_hash == [None]
    else:
        assert o.end == "submitted" and len(o.records) == 2


# --- 9. provider error ---------------------------------------------------------------------


@pytest.mark.parametrize("fail_at", [0, 1])
def test_provider_error(server: Server, tmp_path: Path, fail_at: int) -> None:
    provider = ScriptedProvider("ollama", by_step(ollama_body([GOOD]), ollama_body([SUBMIT])),
                                fail=lambda req: step_of(req) >= fail_at)
    o = run(server, tmp_path, provider)
    assert o.end == "provider_error" and len(o.records) == fail_at + 1
    last: StepRecord = o.records[-1]
    assert last.end == "provider_error" and HEX64.fullmatch(last.request_key)
    assert last.stop_reason is None and last.usd is None and last.latency_ms is None and last.n_tool_calls == 0
    assert provider.calls == fail_at + 5  # the adapter's retries were exhausted


# --- 14. opaque assistant content round-trips ------------------------------------------------


def test_opaque_content_round_trips(server: Server, tmp_path: Path) -> None:
    call: Call = ("execute_sql", sql_args("SELECT name FROM team"))
    provider = ScriptedProvider("anthropic", by_step(
        anthropic_body([call], step=0, thinking="First, the teams.", signature="sig-0"),
        anthropic_body([SUBMIT], step=1)))
    o = run(server, tmp_path, provider, tier=LARGE)
    assert o.end == "submitted"

    first, second = provider.requests
    assert first.temperature is None and first.seed is None and first.thinking == {"type": "between_tools"}
    blocks = anthropic_content([call], step=0, thinking="First, the teams.", signature="sig-0")
    assistant = second.messages[2]
    assert assistant.role == "assistant" and list(assistant.provider_blocks) == blocks
    wire = AnthropicProvider(client=object()).body(second)  # type: ignore[arg-type]
    assert wire["messages"][1] == {"role": "assistant", "content": blocks}  # type: ignore[index]
    assert "temperature" not in wire
