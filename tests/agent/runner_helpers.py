"""execute_run on the shared fixture server, with a provider scripted per question."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from agent_helpers import (
    DB_IDS,
    SMALL,
    ScriptedProvider,
    Server,
    make_adapter,
    make_task,
    ollama_body,
    prices,
    question_of,
    sql_args,
    step_of,
)

from escalator.agent.prompts import load_prompt
from escalator.eval.runner import RunReport, execute_run, load_agent_config
from escalator.eval.scorers import Score
from escalator.eval.tasks import Task
from escalator.llm.cache import CacheMode
from escalator.llm.types import Request

PRESET = "t"
RUN_ID = f"{PRESET}-{SMALL.name}-v1"
TASKS = [make_task("10", "Q ten"), make_task("2", "Q two"), make_task("1", "Q one")]


def per_question(req: Request) -> bytes:
    """Step 0 explores, step 1 submits a query that depends on the question."""
    if step_of(req) == 0:
        return ollama_body([("execute_sql", sql_args("SELECT COUNT(*) FROM player"))], prompt_tokens=300)
    n = len(question_of(req))
    return ollama_body([("submit_answer", sql_args(f"SELECT {n}"))], prompt_tokens=360)


def scripted(fail: Callable[[Request], bool] = lambda _r: False) -> ScriptedProvider:
    return ScriptedProvider("ollama", per_question, fail=fail)


def always_right(_task: Task, _sql: str) -> Score:
    return Score(True, 1.0, None)


def run_preset(server: Server, tmp_path: Path, provider: ScriptedProvider, *, tasks: Sequence[Task] = TASKS,
               cache_mode: CacheMode = "readwrite", overwrite: bool = False,
               max_usd: Decimal | None = None) -> RunReport:
    cache = server.cache(tmp_path / "llm.sqlite", cache_mode)

    async def go() -> RunReport:
        return await execute_run(
            preset=PRESET, specs=[SMALL], tasks=tasks, llm=make_adapter(provider, cache), prices=prices(),
            prompt=load_prompt("v1"), cfg=load_agent_config(), session=server.session,
            server_version=server.version, db_ids=DB_IDS, traces_root=tmp_path / "traces", overwrite=overwrite,
            score=always_right, ollama_version=None, max_usd=max_usd,
        )

    try:
        return server.run(go)
    finally:
        server.portal.call(cache.close)
