"""trace/replay.py: a task re-run from its trace reproduces every request key (spec test 15)."""

from __future__ import annotations

from pathlib import Path

from agent_helpers import DB_IDS, Server, make_task, prices, question_of, step_of
from runner_helpers import RUN_ID, TASKS, run_preset, scripted

from escalator.eval.tasks import Task
from escalator.trace.replay import KeyMismatch, replay_task


def replay(server: Server, tmp_path: Path, task: Task) -> list[KeyMismatch]:
    cache = server.cache(tmp_path / "llm.sqlite", "readonly")

    async def go() -> list[KeyMismatch]:
        return await replay_task(tmp_path / "traces" / RUN_ID, task, 0, session=server.session, cache=cache,
                                 prices=prices(), db_ids=DB_IDS)

    try:
        return server.run(go)
    finally:
        server.portal.call(cache.close)


def test_replay_reproduces_request_keys(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    for task in TASKS:
        assert replay(server, tmp_path, task) == []


def test_replay_reports_a_changed_input(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    changed = make_task("1", "Q one, reworded")
    mismatches = replay(server, tmp_path, changed)
    assert [m.step for m in mismatches][:1] == [0]


def test_replay_reproduces_a_provider_error_trace(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted(fail=lambda req: question_of(req).endswith("Q two") and step_of(req) == 1))
    assert replay(server, tmp_path, TASKS[1]) == []
