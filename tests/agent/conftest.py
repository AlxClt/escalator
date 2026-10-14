"""Agent tests are hermetic: no network, no data/raw, no Ollama. A real MCP server subprocess serves
a fixture database built from tests/fixtures/agent_db.sql; the LLM is a scripted fake provider."""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from agent_helpers import DB_ID, Server, start_server

FIXTURE_SQL = Path(__file__).resolve().parents[1] / "fixtures" / "agent_db.sql"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No outbound connections. socket.connect stays usable: event loops build local socketpairs."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("agent tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(scope="session")
def db_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`<tmp>/bird`, laid out as the server's --db-root expects: bird/<id>/<id>.sqlite."""
    root = tmp_path_factory.mktemp("db") / "bird"
    path = root / DB_ID / f"{DB_ID}.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return root


@pytest.fixture(scope="session")
def server(db_root: Path) -> Iterator[Server]:
    """One MCP server subprocess for the whole session: the tools are stateless."""
    with start_server(db_root) as sv:
        yield sv
