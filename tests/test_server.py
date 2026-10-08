"""MCP server, driven as a real subprocess through the SDK's stdio client.

Needs the BIRD databases under data/raw/bird (`make data`); skipped where they are absent (CI).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import CallToolResult, TextContent
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from escalator.datasets.config import PLAT_FULL_JSON, REPO_ROOT
from escalator.env.sandbox import Result
from escalator.env.server import MAX_ROWS, PREVIEW_ROWS, dumps, open_sandbox, result_object

DB_ROOT = REPO_ROOT / "data" / "raw" / "bird"
SERVER_CMD = [sys.executable, "-m", "escalator.env.server", "--db-root", str(DB_ROOT)]

pytestmark = pytest.mark.skipif(
    not any(DB_ROOT.glob("*/*.sqlite")), reason="BIRD databases not installed (run make data)"
)

T = TypeVar("T")


def with_session(fn: Callable[[ClientSession], Awaitable[T]]) -> T:
    """Start a fresh server process, initialize a session, run `fn`, shut down."""
    params = StdioServerParameters(command=SERVER_CMD[0], args=SERVER_CMD[1:], cwd=REPO_ROOT)

    async def run() -> T:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)

    return anyio.run(run)


def text(res: CallToolResult) -> str:
    assert len(res.content) == 1 and isinstance(res.content[0], TextContent)
    assert res.structured_content is None
    return res.content[0].text


def error_kind(res: CallToolResult) -> str:
    assert res.is_error, text(res)
    obj = json.loads(text(res))
    assert set(obj) == {"error", "message"}
    return str(obj["error"])


async def call(session: ClientSession, name: str, **args: Any) -> CallToolResult:
    return await session.call_tool(name, args)


# --- 1. tools/list is stable across restarts -----------------------------------


def test_tools_list_identical_across_restarts() -> None:
    async def canonical(session: ClientSession) -> str:
        tools = (await session.list_tools()).tools
        dumped = sorted((t.model_dump(mode="json", by_alias=True) for t in tools), key=lambda t: t["name"])
        return json.dumps(dumped, sort_keys=True)

    first, second = with_session(canonical), with_session(canonical)
    assert first == second
    tools = json.loads(first)
    assert [t["name"] for t in tools] == ["execute_sql", "get_schema", "sample_rows", "submit_answer"]
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["db_id"]["enum"] == sorted(p.stem for p in DB_ROOT.glob("*/*.sqlite"))


# --- 2. every tool is deterministic ---------------------------------------------


CALLS: list[tuple[str, dict[str, Any]]] = [
    ("get_schema", {"db_id": "superhero"}),
    ("get_schema", {"db_id": "superhero", "tables": ["hero_power", "colour"]}),
    ("sample_rows", {"db_id": "formula_1", "table": "races"}),
    ("execute_sql", {"db_id": "financial", "sql": "SELECT district_id, A2 FROM district ORDER BY A2 LIMIT 70"}),
    ("submit_answer", {"db_id": "superhero", "sql": "SELECT COUNT(*) FROM superhero"}),
]


@pytest.mark.parametrize(("name", "args"), CALLS, ids=[c[0] for c in CALLS])
def test_tool_output_byte_identical(name: str, args: dict[str, Any]) -> None:
    async def twice(session: ClientSession) -> tuple[CallToolResult, CallToolResult]:
        return await session.call_tool(name, args), await session.call_tool(name, args)

    a, b = with_session(twice)
    assert not a.is_error, text(a)
    assert text(a) == text(b)


def test_get_schema_matches_schema_card() -> None:
    from escalator.env.schema import render_card

    sandbox = open_sandbox(DB_ROOT)
    out = with_session(lambda s: call(s, "get_schema", db_id="superhero", tables=["colour"]))
    assert text(out) == render_card(sandbox, "superhero", ["colour"])


# --- 3. forbidden and malformed SQL never succeeds ------------------------------


DENIED = [
    ("INSERT INTO colour VALUES (999, 'x')", "forbidden"),
    ("CREATE TEMP TABLE z (a)", "forbidden"),
    ("ATTACH DATABASE 'other.sqlite' AS o", "forbidden"),
    ("PRAGMA table_info(colour)", "forbidden"),
    ("SELECT 1; SELECT 2", "sql_error"),
    ("SELECT random()", "forbidden"),
    ("SELECT date('now')", "forbidden"),
]


def test_denied_inputs() -> None:
    async def run_all(session: ClientSession) -> list[str]:
        return [error_kind(await call(session, "execute_sql", db_id="superhero", sql=sql)) for sql, _ in DENIED]

    assert with_session(run_all) == [kind for _, kind in DENIED]


# --- 4. timeout, and the process survives it ------------------------------------


@pytest.mark.slow
def test_timeout_then_recovers() -> None:
    async def run(session: ClientSession) -> tuple[float, CallToolResult, CallToolResult]:
        start = time.monotonic()
        slow = await call(session, "execute_sql", db_id="financial", sql="SELECT COUNT(*) FROM trans a, trans b")
        elapsed = time.monotonic() - start
        return elapsed, slow, await call(session, "execute_sql", db_id="financial", sql="SELECT 1")

    elapsed, slow, after = with_session(run)
    assert error_kind(slow) == "timeout"
    assert elapsed < 31
    assert not after.is_error and json.loads(text(after))["rows"] == [[1]]


# --- 5. row cap, preview and cell clipping --------------------------------------


def test_row_cap_preview_and_clipping() -> None:
    sql = "SELECT Id, Body FROM posts WHERE length(Body) > 200 ORDER BY Id"
    out = json.loads(text(with_session(lambda s: call(s, "execute_sql", db_id="codebase_community", sql=sql))))
    assert out["n_rows"] == MAX_ROWS == 1000
    assert out["n_rows_is_lower_bound"] is True
    assert len(out["rows"]) == PREVIEW_ROWS == 50

    direct = open_sandbox(DB_ROOT).execute("codebase_community", sql, row_cap=MAX_ROWS)
    assert isinstance(direct, Result)
    body = direct.rows[0][1]
    assert isinstance(body, str) and len(body) > 200
    assert out["rows"][0][1] == f"{body[:200]}…[+{len(body) - 200} chars]"


# --- 6. server output equals the direct sandbox result --------------------------


def _gold_queries() -> list[tuple[str, str]]:
    """The first Arcwise gold query of each of the first 10 databases (sorted), in file order."""
    tasks = json.loads((REPO_ROOT / "data" / "raw" / PLAT_FULL_JSON).read_text(encoding="utf-8"))
    first: dict[str, str] = {}
    for task in tasks:
        first.setdefault(task["db_id"], task["SQL"])
    return [(db, first[db]) for db in sorted(first)[:10]]


def test_execute_sql_matches_direct_sandbox() -> None:
    queries = _gold_queries()
    assert len(queries) == 10

    async def run_all(session: ClientSession) -> list[CallToolResult]:
        return [await call(session, "execute_sql", db_id=db, sql=sql) for db, sql in queries]

    results = with_session(run_all)
    sandbox = open_sandbox(DB_ROOT)
    for (db, sql), res in zip(queries, results, strict=True):
        direct = sandbox.execute(db, sql, row_cap=MAX_ROWS)
        assert isinstance(direct, Result), direct
        assert not res.is_error
        assert text(res) == dumps(result_object(direct))


# --- 7. semantic errors ---------------------------------------------------------


def test_unknown_table_and_db() -> None:
    async def run(session: ClientSession) -> list[str]:
        return [
            error_kind(await call(session, "get_schema", db_id="superhero", tables=["colour", "nope"])),
            error_kind(await call(session, "sample_rows", db_id="superhero", table="nope")),
            error_kind(await call(session, "execute_sql", db_id="nope", sql="SELECT 1")),
        ]

    assert with_session(run) == ["unknown_table", "unknown_table", "unknown_db"]


# --- 8. stdout carries protocol frames only -------------------------------------


def test_stdout_is_only_jsonrpc_frames() -> None:
    proc = subprocess.Popen(
        SERVER_CMD,
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdin is not None and proc.stdout is not None
    lines: list[str] = []

    def send(frame: dict[str, Any], *, reply: bool) -> None:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(frame) + "\n")
        proc.stdin.flush()
        if reply:
            lines.append(proc.stdout.readline())

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            reply=True,
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"}, reply=False)
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, reply=True)
        for i, (name, args) in enumerate(CALLS, start=3):
            send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}},
                 reply=True)
        send({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
              "params": {"name": "execute_sql", "arguments": {"db_id": "superhero", "sql": "SELECT random()"}}},
             reply=True)
        proc.stdin.close()
        lines.extend(proc.stdout.readlines())
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()

    frames = [json.loads(line) for line in lines if line.strip()]
    assert all(line.strip() for line in lines)
    assert all(f.get("jsonrpc") == "2.0" for f in frames)
    assert [f.get("id") for f in frames] == [1, 2, *range(3, 3 + len(CALLS)), 99]
    assert all("result" in f for f in frames)
