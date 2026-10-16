"""Week-3 spec test 3, on the real week-2 runs: replaying a run against the readonly LLM cache reproduces
every recorded request key, so the week-3 code builds byte-identical keys. Local only (`make test-local`):
the traces, the cache and the databases are not in the repo. The 10 smoke tasks of each baseline run of
the configs/router.yaml inventory are replayed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from agent_helpers import Server, start_server

from escalator.agent.tools import db_ids_under
from escalator.datasets.config import REPO_ROOT, load_config
from escalator.eval.collect import SMOKE_TASKS
from escalator.eval.tasks import Task, load_tasks
from escalator.llm.cache import DEFAULT_CACHE
from escalator.llm.cost import PriceTable
from escalator.router.config import load_router_config
from escalator.trace.replay import replay_task

pytestmark = pytest.mark.local

RUN_IDS = [r.baseline for r in load_router_config().small.values()] + [load_router_config().frontier.baseline]


@pytest.fixture(scope="module")
def real_server() -> Iterator[Server]:
    with start_server(load_config().data_root / "bird") as sv:
        yield sv


@pytest.fixture(scope="module")
def smoke_tasks() -> list[Task]:
    return load_tasks()[:SMOKE_TASKS]


@pytest.mark.parametrize("run_id", RUN_IDS)
def test_week2_request_keys_reproduce(real_server: Server, smoke_tasks: list[Task], run_id: str) -> None:
    run_dir = REPO_ROOT / "traces" / run_id
    db_ids = db_ids_under(load_config().data_root / "bird")
    cache = real_server.cache(DEFAULT_CACHE, "readonly")
    try:
        for task in smoke_tasks:
            async def go(t: Task = task) -> list[object]:
                return list(await replay_task(run_dir, t, 0, session=real_server.session, cache=cache,
                                              prices=PriceTable.load(), db_ids=db_ids))

            assert real_server.run(go) == [], f"{run_id} task {task.question_id}"
    finally:
        real_server.portal.call(cache.close)
