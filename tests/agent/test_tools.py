"""agent/tools.py: canonical manifest and startup check (10), server-side rejection raises (11),
typed validation errors (2), classification and rendering."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import mcp_types as types
import pytest
from agent_helpers import (
    DB_ID,
    DB_IDS,
    ScriptedProvider,
    Server,
    by_step,
    make_adapter,
    ollama_body,
    run_one,
    sql_args,
)
from mcp.shared.exceptions import MCPError

from escalator.agent.prompts import load_prompt
from escalator.agent.tools import (
    HarnessError,
    ManifestMismatch,
    RenderCaps,
    build_manifest,
    check_server_tools,
    classify,
    input_schemas,
    manifest_sha256,
    render_result,
    validate,
)
from escalator.llm.types import ToolCall

SCHEMAS = input_schemas(DB_IDS)


def call(name: str, args: object) -> ToolCall:
    raw = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(id="c", name=name, args=args if isinstance(args, dict) else None, args_raw=raw)


def text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)


# --- 10. canonical manifest and startup check ----------------------------------------


def listed_tools(server: Server) -> list[types.Tool]:
    async def go() -> list[types.Tool]:
        return list((await server.session.list_tools()).tools)

    return server.run(go)


def test_server_tools_match_canonical(server: Server) -> None:
    tools = listed_tools(server)
    check_server_tools(tools, DB_IDS)
    check_server_tools(list(reversed(tools)), DB_IDS)  # the server's order never matters


def test_manifest_hash_invariant_to_order() -> None:
    manifest = build_manifest(DB_IDS, load_prompt("v1"))
    assert [t.name for t in manifest] == sorted(t.name for t in manifest)
    assert manifest_sha256(manifest) == manifest_sha256(reversed(manifest))
    # Descriptions are part of what the model sees, so part of the hash.
    assert manifest_sha256(manifest) != manifest_sha256(build_manifest(["other"], load_prompt("v1")))


@pytest.mark.parametrize("mutate", [
    lambda tools: tools[1:],
    lambda tools: [*tools, tools[0].model_copy(update={"name": "drop_table"})],
    lambda tools: [tools[0].model_copy(update={"input_schema": {**tools[0].input_schema, "required": []}}), *tools[1:]],
], ids=["missing_tool", "extra_tool", "schema_changed"])
def test_startup_mismatch_fails(server: Server, mutate: Callable[[list[types.Tool]], list[types.Tool]]) -> None:
    with pytest.raises(ManifestMismatch):
        check_server_tools(mutate(listed_tools(server)), DB_IDS)


def test_startup_mismatch_on_db_set(server: Server) -> None:
    with pytest.raises(ManifestMismatch):
        check_server_tools(listed_tools(server), [DB_ID, "another_db"])


def test_descriptions_ignored_by_startup_check(server: Server) -> None:
    tools = listed_tools(server)
    described = [t.model_copy(update={"description": "different"}) for t in tools]
    check_server_tools(described, DB_IDS)


# --- 2. typed validation errors (pure) -------------------------------------------------


def test_validation_errors_are_typed_and_deterministic() -> None:
    assert validate(call("execute_sql", sql_args("SELECT 1")), SCHEMAS) is None
    assert validate(call("execute_sql", '{"db_id": '), SCHEMAS) == {
        "error_type": "validation_error", "tool": "execute_sql", "detail": {"reason": "unparsable_arguments"}}
    assert validate(call("nope", {}), SCHEMAS) == {
        "error_type": "validation_error", "tool": "nope", "detail": {"reason": "unknown_tool"}}
    bad = call("get_schema", {"db_id": "x", "tables": "team", "z": 1})
    first, second = validate(bad, SCHEMAS), validate(bad, SCHEMAS)
    assert first == second and first is not None
    detail = first["detail"]
    assert isinstance(detail, dict) and detail["reason"] == "schema"
    errors = detail["errors"]
    assert isinstance(errors, list)
    assert [e["path"] for e in errors] == sorted(e["path"] for e in errors)
    assert {e["path"] for e in errors} == {"/", "/db_id", "/tables"}


# --- 11. server-side rejection of a validated call is a harness bug ---------------------


class StubSession:
    def __init__(self, on_call: Callable[[str], types.CallToolResult]) -> None:
        self.on_call = on_call

    async def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> types.CallToolResult:
        if name == "get_schema":
            return text_result("## player\nid INTEGER PK")
        return self.on_call(name)


def raise_mcp(_name: str) -> types.CallToolResult:
    raise MCPError(code=types.INVALID_PARAMS, message="Invalid params")


@pytest.mark.parametrize("on_call", [
    raise_mcp,
    lambda _n: text_result('{"error":"unknown_db","message":"unknown database: fixture"}', is_error=True),
    lambda _n: text_result("Input validation error: 'sql' is a required property", is_error=True),
], ids=["jsonrpc_error", "unknown_db", "non_json_error"])
def test_server_side_validation_error_raises(server: Server, tmp_path: Path,
                                             on_call: Callable[[str], types.CallToolResult]) -> None:
    provider = ScriptedProvider("ollama", by_step(ollama_body([("execute_sql", sql_args("SELECT 1"))])))
    with pytest.raises(HarnessError):
        run_one(server, make_adapter(provider, server.cache(tmp_path / "llm.sqlite")), session=StubSession(on_call))
    assert provider.calls == 1  # no retry


# --- classification and rendering ---------------------------------------------------------


@pytest.mark.parametrize(("kind", "status"), [
    ("sql_error", "execution_error"), ("unknown_table", "execution_error"), ("timeout", "timeout"),
    ("forbidden", "denied"),
])
def test_classify_server_errors(kind: str, status: str) -> None:
    text = json.dumps({"error": kind, "message": "m"})
    assert classify("execute_sql", text_result(text, is_error=True)) == (status, text)


def result_text(n_rows: int, *, cell: str = "x", lower_bound: bool = False) -> str:
    rows = [[i, cell] for i in range(min(n_rows, 50))]
    return json.dumps({"columns": ["i", "s"], "n_rows": n_rows, "n_rows_is_lower_bound": lower_bound, "rows": rows})


def test_render_caps_rows_and_states_counts() -> None:
    out = json.loads(render_result("execute_sql", "ok", result_text(120), RenderCaps(max_rows=10, max_chars=4000)))
    assert out["rows_shown"] == 10 and len(out["rows"]) == 10
    assert out["n_rows"] == 120 and out["truncated"] is True


def test_render_caps_chars() -> None:
    text = render_result("execute_sql", "ok", result_text(50, cell="y" * 100), RenderCaps(max_rows=50, max_chars=1000))
    out = json.loads(text)
    assert len(text) <= 1000 and 0 < out["rows_shown"] < 50 and out["truncated"] is True


def test_render_small_result_not_truncated() -> None:
    out = json.loads(render_result("sample_rows", "ok", result_text(3), RenderCaps()))
    assert out["rows_shown"] == 3 and out["truncated"] is False


def test_render_is_pure_and_passes_through_errors_and_cards() -> None:
    text = result_text(80)
    assert render_result("execute_sql", "ok", text, RenderCaps()) == render_result("execute_sql", "ok", text, RenderCaps())
    err = '{"error":"sql_error","message":"no such column: nope"}'
    assert render_result("execute_sql", "execution_error", err, RenderCaps()) == err
    card = "## t\n" + "c TEXT {braces}\n" * 1000
    assert render_result("get_schema", "ok", card, RenderCaps(max_chars=10)) == card


def test_render_through_server(server: Server, tmp_path: Path) -> None:
    sql = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 120) SELECT x FROM c"
    provider = ScriptedProvider("ollama", by_step(ollama_body([("execute_sql", sql_args(sql))]),
                                                  ollama_body([("submit_answer", sql_args("SELECT 1"))])))
    run_one(server, make_adapter(provider, server.cache(tmp_path / "llm.sqlite")))
    shown = json.loads(provider.requests[1].messages[-1].content)
    assert shown["n_rows"] == 120 and shown["rows_shown"] == 50 and shown["truncated"] is True
