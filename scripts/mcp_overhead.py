"""Per-call MCP overhead: 200 execute_sql("SELECT 1") through the server vs 200 direct sandbox calls.

    uv run --locked python scripts/mcp_overhead.py
"""

from __future__ import annotations

import statistics
import sys
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from escalator.datasets.config import REPO_ROOT
from escalator.env.server import MAX_ROWS, open_sandbox

N = 200
DB_ROOT = REPO_ROOT / "data" / "raw" / "bird"
DB_ID = "superhero"
SQL = "SELECT 1"


def p95(xs: list[float]) -> float:
    return statistics.quantiles(xs, n=100, method="inclusive")[94]


async def mcp_timings() -> list[float]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "escalator.env.server", "--db-root", str(DB_ROOT)], cwd=REPO_ROOT
    )
    out: list[float] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for _ in range(N):
            start = time.perf_counter()
            res = await session.call_tool("execute_sql", {"db_id": DB_ID, "sql": SQL})
            out.append(time.perf_counter() - start)
            assert not res.is_error
    return out


def direct_timings() -> list[float]:
    sandbox = open_sandbox(DB_ROOT)
    out: list[float] = []
    for _ in range(N):
        start = time.perf_counter()
        sandbox.execute(DB_ID, SQL, row_cap=MAX_ROWS)
        out.append(time.perf_counter() - start)
    return out


def main() -> None:
    via_mcp = anyio.run(mcp_timings)
    direct = direct_timings()
    ms = 1000.0
    print(f"{N} calls of execute_sql({SQL!r}) on {DB_ID}, ms per call")
    print(f"  mcp     median {statistics.median(via_mcp) * ms:7.3f}  p95 {p95(via_mcp) * ms:7.3f}")
    print(f"  direct  median {statistics.median(direct) * ms:7.3f}  p95 {p95(direct) * ms:7.3f}")
    print(
        f"  overhead (mcp - direct)  median {(statistics.median(via_mcp) - statistics.median(direct)) * ms:7.3f}"
        f"  p95 {(p95(via_mcp) - p95(direct)) * ms:7.3f}"
    )


if __name__ == "__main__":
    main()
