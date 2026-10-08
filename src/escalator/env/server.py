"""MCP server over stdio: the four agent tools as a thin adapter over the sandbox and schema card.

Stateless and deterministic. No scoring, no gold, no manifest. Argument shape is validated by the
client (`agent/tools.py`) against the `tools/list` manifest; the server only runs semantic checks
(unknown db, unknown table). Nothing but protocol frames reaches stdout: logging goes to stderr.

    uv run --locked python -m escalator.env.server --db-root data/raw/bird
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from escalator.datasets.config import REPO_ROOT
from escalator.datasets.lock import load_env_lock
from escalator.env import sandbox as sandbox_mod
from escalator.env.sandbox import Result, Sandbox, SqlError, SqlValue
from escalator.env.schema import UnknownTable, render_card

# Bump whenever tool definitions, output format or limits change: the client hashes it into its
# LLM cache key.
SERVER_VERSION = "1"

PREVIEW_ROWS = 50
CELL_CHARS = 200
MAX_ROWS = sandbox_mod.ROW_CAP
SAMPLE_ROWS = 5

ENV_LOCK = REPO_ROOT / "data" / "env.lock"

_KIND = {
    "syntax": "sql_error",
    "runtime": "sql_error",
    "multi_statement": "sql_error",
    "denied": "forbidden",
    "timeout": "timeout",
    "unknown_db": "unknown_db",
}

log = logging.getLogger("escalator.env.server")

# JSON values: tool results and arguments are arbitrary JSON, typed at the client.
JsonObj = dict[str, Any]


def dumps(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def render_cell(value: SqlValue) -> SqlValue:
    if isinstance(value, str) and len(value) > CELL_CHARS:
        return f"{value[:CELL_CHARS]}…[+{len(value) - CELL_CHARS} chars]"
    if isinstance(value, bytes):
        return f"<blob:{len(value)} bytes>"
    return value


def result_object(result: Result) -> JsonObj:
    return {
        "columns": result.columns,
        "n_rows": len(result.rows),
        "n_rows_is_lower_bound": result.truncated,
        "rows": [[render_cell(v) for v in row] for row in result.rows[:PREVIEW_ROWS]],
    }


def error_text(kind: str, message: str) -> str:
    return dumps({"error": kind, "message": message})


def _db_id_schema(db_ids: list[str]) -> JsonObj:
    return {"type": "string", "enum": db_ids}


def tool_defs(db_ids: list[str]) -> list[types.Tool]:
    """Static definitions; only the sorted `db_id` enum depends on --db-root."""
    sql_props = {"db_id": _db_id_schema(db_ids), "sql": {"type": "string"}}
    return [
        types.Tool(
            name="get_schema",
            description="Schema card of a database: tables, columns, types, keys and descriptions. "
            "Optionally restricted to the listed tables.",
            input_schema={
                "type": "object",
                "properties": {
                    "db_id": _db_id_schema(db_ids),
                    "tables": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["db_id"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="sample_rows",
            description=f"The first {SAMPLE_ROWS} rows of a table, in storage order.",
            input_schema={
                "type": "object",
                "properties": {"db_id": _db_id_schema(db_ids), "table": {"type": "string"}},
                "required": ["db_id", "table"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="execute_sql",
            description=f"Run one read-only SQLite SELECT. Returns at most {PREVIEW_ROWS} rows and counts "
            f"up to {MAX_ROWS}.",
            input_schema={
                "type": "object",
                "properties": sql_props,
                "required": ["db_id", "sql"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="submit_answer",
            description="Submit the final SQL query. Runs it like execute_sql.",
            input_schema={
                "type": "object",
                "properties": sql_props,
                "required": ["db_id", "sql"],
                "additionalProperties": False,
            },
        ),
    ]


def _text(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)], is_error=is_error)


def _error(kind: str, message: str) -> types.CallToolResult:
    return _text(error_text(kind, message), is_error=True)


def _sql_result(out: Result | SqlError) -> types.CallToolResult:
    if isinstance(out, SqlError):
        return _error(_KIND[out.kind], out.message)
    return _text(dumps(result_object(out)))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _sample_rows(sandbox: Sandbox, db_id: str, table: str) -> types.CallToolResult:
    found = sandbox.introspect(db_id, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
    if not found:
        return _error("unknown_table", f"unknown table: {table}")
    without_rowid = sandbox.introspect(
        db_id, "SELECT wr FROM pragma_table_list WHERE schema = 'main' AND name = ?", (table,)
    )
    if without_rowid and without_rowid[0][0]:
        pk = sandbox.introspect(db_id, "SELECT name FROM pragma_table_info(?) WHERE pk > 0 ORDER BY pk", (table,))
        order = ", ".join(_quote(str(r[0])) for r in pk)
    else:
        order = "rowid"
    return _sql_result(
        sandbox.execute(db_id, f"SELECT * FROM {_quote(table)} ORDER BY {order} LIMIT {SAMPLE_ROWS}", row_cap=MAX_ROWS)
    )


def call_tool(sandbox: Sandbox, name: str, args: JsonObj) -> types.CallToolResult:
    db_id = args["db_id"]
    if db_id not in sandbox.db_ids:
        return _error("unknown_db", f"unknown database: {db_id}")
    if name == "get_schema":
        try:
            return _text(render_card(sandbox, db_id, args.get("tables")))
        except UnknownTable as exc:
            return _error("unknown_table", str(exc))
    if name == "sample_rows":
        return _sample_rows(sandbox, db_id, args["table"])
    if name in ("execute_sql", "submit_answer"):
        return _sql_result(sandbox.execute(db_id, args["sql"], row_cap=MAX_ROWS))
    raise MCPError(code=types.INVALID_PARAMS, message=f"unknown tool: {name}")


def build_server(sandbox: Sandbox) -> Server[Any]:
    tools = tool_defs(sorted(sandbox.db_ids))

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # Sequential by design: each runner worker spawns its own server process.
        return call_tool(sandbox, params.name, params.arguments or {})

    return Server("escalator", version=SERVER_VERSION, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def open_sandbox(db_root: Path) -> Sandbox:
    """Databases are the `.sqlite` stems under db_root; the Sandbox lays them out as <root>/bird/<id>/."""
    db_ids = sorted({p.stem for p in db_root.rglob("*.sqlite")})
    return Sandbox(db_root.parent, db_ids, expected_sqlite=load_env_lock(ENV_LOCK).sqlite_version)


async def _serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m escalator.env.server")
    parser.add_argument("--db-root", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    sandbox = open_sandbox(args.db_root)
    log.info("serving %d databases from %s", len(sandbox.db_ids), args.db_root)
    anyio.run(_serve, build_server(sandbox))


if __name__ == "__main__":
    main()
