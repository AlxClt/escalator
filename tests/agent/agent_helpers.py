"""Shared helpers for tests/agent: scripted providers, wire bodies, a server session, fixture tasks."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client.session import ClientSession

from escalator.agent.loop import LLM, LoopConfig, RunContext, TaskOutcome, ToolSession, run_task
from escalator.agent.prompts import load_prompt
from escalator.agent.tools import build_manifest, input_schemas, manifest_sha256
from escalator.eval.runner import server_session
from escalator.eval.tasks import Task
from escalator.llm.adapter import Adapter
from escalator.llm.cache import Cache, CacheMode
from escalator.llm.cost import PriceTable
from escalator.llm.errors import TransientProviderError
from escalator.llm.models import ModelSpec
from escalator.llm.providers.anthropic import AnthropicProvider
from escalator.llm.providers.base import RawResponse
from escalator.llm.providers.ollama import OllamaProvider
from escalator.llm.types import JsonDict, Normalized, Request

T = TypeVar("T")

DB_ID = "fixture"
DB_IDS = [DB_ID]
SMALL_MODEL = "ollama/fake:tag@" + "c" * 64
LARGE_MODEL = "claude-sonnet-5-5"
SMALL = ModelSpec("fake-small", "S", "ollama", "fake:tag", "c" * 64, None, {"num_ctx": 16384, "think": False})
LARGE = ModelSpec("fake-large", "L", "anthropic", None, None, LARGE_MODEL,
                  {"thinking": {"type": "between_tools"}, "effort": "medium"})


def prices(small_input: str = "1.00", small_output: str = "4.00") -> PriceTable:
    """Non-zero by default, so "$0 on rerun" is a real assertion, not a property of free models."""
    return PriceTable.parse(f"""
retrieved_on: 2026-10-02
source: test
unit: usd_per_million_tokens
models:
  {SMALL_MODEL}: {{input: "{small_input}", output: "{small_output}"}}
  {LARGE_MODEL}: {{input: "2.00", output: "10.00"}}
""".encode())


# A tool call as scripted: (name, arguments). A str argument is sent raw (it may be unparsable JSON).
Call = tuple[str, object]


def sql_args(sql: str) -> JsonDict:
    return {"db_id": DB_ID, "sql": sql}


def ollama_body(calls: Sequence[Call] = (), *, text: str = "", prompt_tokens: int = 100, out_tokens: int = 20,
                done_reason: str = "stop") -> bytes:
    return json.dumps({
        "model": "fake:tag", "done": True, "done_reason": done_reason,
        "message": {"role": "assistant", "content": text,
                    "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]},
        "prompt_eval_count": prompt_tokens, "eval_count": out_tokens,
    }).encode()


def anthropic_content(calls: Sequence[Call], *, step: int, thinking: str, signature: str) -> list[JsonDict]:
    blocks: list[JsonDict] = [{"type": "thinking", "thinking": thinking, "signature": signature},
                              {"type": "text", "text": f"step {step}"}]
    blocks += [{"type": "tool_use", "id": f"toolu_{step}_{i}", "name": n, "input": a} for i, (n, a) in enumerate(calls)]
    return blocks


def anthropic_body(calls: Sequence[Call], *, step: int, thinking: str = "Let me think.",
                   signature: str = "sig") -> bytes:
    return json.dumps({
        "id": f"msg_{step}", "type": "message", "role": "assistant", "model": LARGE_MODEL,
        "content": anthropic_content(calls, step=step, thinking=thinking, signature=signature),
        "stop_reason": "tool_use", "usage": {"input_tokens": 1000, "output_tokens": 50},
    }).encode()


def step_of(req: Request) -> int:
    return sum(1 for m in req.messages if m.role == "assistant")


def question_of(req: Request) -> str:
    return req.messages[1].content


Script = Callable[[Request], bytes]


def by_step(*bodies: bytes) -> Script:
    """The i-th body for step i; the last body repeats."""
    return lambda req: bodies[min(step_of(req), len(bodies) - 1)]


class ScriptedProvider:
    """A provider whose wire bodies come from a script; parsing is the real provider's."""

    def __init__(self, name: Literal["ollama", "anthropic"], script: Script,
                 fail: Callable[[Request], bool] = lambda _req: False) -> None:
        self.name = name
        self._script = script
        self._fail = fail
        self._parser: OllamaProvider | AnthropicProvider = (
            OllamaProvider(base_url="http://unused", client=object())  # type: ignore[arg-type]
            if name == "ollama" else AnthropicProvider(client=object())  # type: ignore[arg-type]
        )
        self.calls = 0
        self.requests: list[Request] = []

    def call(self, req: Request) -> RawResponse:
        self.calls += 1
        self.requests.append(req)
        if self._fail(req):
            raise TransientProviderError("scripted outage")
        return RawResponse(self._script(req), 100 + 10 * step_of(req))

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return self._parser.normalize(body, req)


def make_adapter(provider: ScriptedProvider, cache: Cache, table: PriceTable | None = None) -> Adapter:
    return Adapter({provider.name: provider}, cache, table or prices(), sleep=lambda _s: None)


def make_run_ctx(llm: LLM, *, config: LoopConfig | None = None, run_id: str = "test") -> RunContext:
    prompt = load_prompt("v1")
    tools = build_manifest(DB_IDS, prompt)
    return RunContext(run_id=run_id, llm=llm, prompt=prompt, tools=tools, schemas=input_schemas(DB_IDS),
                      cache_context={"tool_manifest_sha": manifest_sha256(tools), "mcp_server_version": "1"},
                      config=config or LoopConfig())


def make_task(qid: str = "1", question: str = "How many players are there?", evidence: str | None = None) -> Task:
    return Task(qid, DB_ID, "simple", "SELECT COUNT(*) FROM player", "unused", False,
                question=question, evidence=evidence)


@dataclass(frozen=True)
class Server:
    """One real MCP server for the test session, driven from a blocking portal's event-loop thread.

    Everything that touches the session or the LLM cache runs in that thread (sqlite connections
    are single-threaded), hence `run` and `cache`.
    """

    portal: BlockingPortal
    session: ClientSession
    version: str

    def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        return self.portal.call(fn)

    def cache(self, path: Path, mode: CacheMode = "readwrite") -> Cache:
        return self.portal.call(Cache, path, mode)


@contextmanager
def start_server(db_root: Path) -> Iterator[Server]:
    """Start the real MCP server on the fixture db (startup check included)."""
    with start_blocking_portal() as portal, portal.wrap_async_context_manager(server_session(db_root)) as sv:
        yield Server(portal, sv[0], sv[1])


def run_one(server: Server, llm: LLM, task: Task | None = None, tier: ModelSpec = SMALL, *,
            config: LoopConfig | None = None, session: ToolSession | None = None) -> TaskOutcome:
    """One task through run_task, on the shared server (or a stub session)."""
    temperature, seed = (0.0, 7) if tier.tier == "S" else (None, None)
    ctx = make_run_ctx(llm, config=config)
    t = task or make_task()

    async def go() -> TaskOutcome:
        return await run_task(t, tier, sample_idx=0, temperature=temperature, seed=seed,
                              session=session or server.session, run_ctx=ctx)

    return server.run(go)
