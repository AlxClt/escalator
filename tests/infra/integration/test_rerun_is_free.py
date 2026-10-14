"""I1: a 2-step trajectory (adapter -> sandbox -> trace) rerun from a readonly cache makes zero
provider calls, spends $0, and yields the same trace except run_id and cache_hit."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from escalator.env.sandbox import Result, Sandbox
from escalator.llm.adapter import Adapter
from escalator.llm.cache import Cache
from escalator.llm.cost import PriceTable
from escalator.llm.providers.base import RawResponse
from escalator.llm.providers.ollama import OllamaProvider
from escalator.llm.types import Message, Normalized, Request, ToolSpec
from escalator.trace.schema import STEPS_FILE, StepRecord, ToolStatus, TraceWriter, preview, read_steps
from escalator.util import canon

MODEL = "ollama/fake:tag@" + "c" * 64
DB_ID = "fixture"
TOOLS = (
    ToolSpec(name="execute_sql", description="Run SQL", input_schema={"type": "object"}),
    ToolSpec(name="submit_answer", description="Submit", input_schema={"type": "object"}),
)
CONTEXT = {"tool_manifest_sha": canon.sha256([t.model_dump(mode="json") for t in TOOLS])}
# A non-zero price so "$0 on rerun" is a real assertion, not a property of free models.
PRICES = f"""
retrieved_on: 2026-10-02
source: test
unit: usd_per_million_tokens
models:
  {MODEL}: {{input: "1.00", output: "4.00"}}
""".encode()


def _body(name: str, sql: str, prompt_tokens: int) -> bytes:
    return json.dumps({
        "model": "fake:tag", "done": True, "done_reason": "stop",
        "message": {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": {"sql": sql}}}]},
        "prompt_eval_count": prompt_tokens, "eval_count": 20,
    }).encode()


class ScriptedProvider:
    """Ollama wire format, scripted by turn: step 0 executes SQL, step 1 submits."""

    name = "ollama"

    def __init__(self) -> None:
        self._parser = OllamaProvider(base_url="http://unused", client=object())  # type: ignore[arg-type]
        self.calls = 0

    def call(self, req: Request) -> RawResponse:
        self.calls += 1
        if len(req.messages) == 1:
            return RawResponse(_body("execute_sql", "SELECT COUNT(*) FROM t", 300), 120)
        return RawResponse(_body("submit_answer", "SELECT COUNT(*) FROM t", 360), 95)

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return self._parser.normalize(body, req)


def run(adapter: Adapter, sandbox: Sandbox, run_dir: Path, run_id: str) -> list[StepRecord]:
    messages: list[Message] = [Message(role="user", content="How many rows are in t?")]
    with TraceWriter(run_dir) as w:
        for step in range(8):
            req = Request(model=MODEL, messages=tuple(messages), tools=TOOLS, max_tokens=512, num_ctx=16384,
                          think=False)
            r = adapter.complete(req, context=CONTEXT)
            call = r.tool_calls[0]
            result: object = None
            status: ToolStatus = "ok"
            if call.name == "execute_sql":
                assert call.args is not None
                out = sandbox.execute(DB_ID, str(call.args["sql"]))
                assert isinstance(out, Result)
                result = {"columns": out.columns, "rows": out.rows, "truncated": out.truncated}
            rendered = canon.dumps(result).decode("utf-8")
            submitted = call.name == "submit_answer"
            w.append(StepRecord(
                run_id=run_id, task_id="1", tier="S", model=MODEL, step=step, request_key=r.request_key,
                cache_hit=r.cache_hit, stop_reason=r.stop_reason, tool=[call.name], args=[call.args],
                args_raw=[None if call.args is not None else call.args_raw], n_tool_calls=len(r.tool_calls),
                tool_status=[status], result_hash=[canon.sha256(result) if result is not None else None],
                result_preview=[preview(rendered) if result is not None else None], text=r.text,
                tokens_in=r.usage.uncached_in, tokens_out=r.usage.out, tokens_cache_read=r.usage.cache_read,
                tokens_cache_write=r.usage.cache_write, latency_ms=r.latency_ms, usd=str(r.usd),
                end="submitted" if submitted else None,
            ))
            if submitted:
                break
            messages += [r.as_message(), Message(role="tool", content=rendered, tool_call_id=call.id)]
    return read_steps(run_dir / STEPS_FILE)


def spend(steps: list[StepRecord]) -> Decimal:
    return sum((Decimal(s.usd) for s in steps if not s.cache_hit and s.usd is not None), Decimal(0))


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    path = tmp_path / "db" / "bird" / DB_ID / f"{DB_ID}.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(7)])
    conn.commit()
    conn.close()
    return Sandbox(tmp_path / "db", {DB_ID}, expected_sqlite=sqlite3.sqlite_version)


def test_rerun_is_free(tmp_path: Path, sandbox: Sandbox) -> None:
    prices = PriceTable.parse(PRICES)
    cache_path = tmp_path / "cache" / "llm.sqlite"

    first_provider = ScriptedProvider()
    first_cache = Cache(cache_path, "readwrite")
    first = run(Adapter({"ollama": first_provider}, first_cache, prices), sandbox, tmp_path / "run1", "run1")
    first_cache.close()
    assert [s.tool for s in first] == [["execute_sql"], ["submit_answer"]]
    assert first_provider.calls == 2 and spend(first) > 0
    assert first[0].result_preview[0] is not None and "[[7]]" in first[0].result_preview[0]

    second_provider = ScriptedProvider()
    second_adapter = Adapter({"ollama": second_provider}, Cache(cache_path, "readonly"), prices)
    second = run(second_adapter, sandbox, tmp_path / "run2", "run2")
    assert second_provider.calls == 0 and second_adapter.provider_calls == 0
    assert spend(second) == 0
    assert all(s.cache_hit for s in second)

    def strip(steps: list[StepRecord]) -> list[dict[str, object]]:
        return [s.model_dump(exclude={"run_id", "cache_hit"}) for s in steps]

    assert strip(first) == strip(second)
