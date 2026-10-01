"""data/manifest.json against the pinned data. Run by `make manifest` (marker `manifest`)."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from escalator.datasets import manifest, verify
from escalator.datasets.config import load_config
from escalator.datasets.gold import execute_with_deadline

pytestmark = pytest.mark.manifest


@pytest.fixture(scope="module")
def pinned() -> dict[str, object]:
    path = load_config().data_dir / "manifest.json"
    if not path.is_file():
        pytest.fail(f"{path} is missing: run make manifest")
    raw: object = json.loads(path.read_bytes())
    assert isinstance(raw, dict)
    return {str(k): v for k, v in raw.items()}


def _tasks(m: dict[str, object]) -> list[dict[str, object]]:
    tasks = m["tasks"]
    assert isinstance(tasks, list)
    return [t for t in tasks if isinstance(t, dict)]


def test_building_twice_is_byte_identical_to_the_pinned_file() -> None:
    cfg, mcfg = load_config(), manifest.load_manifest_config()
    first = manifest.build_manifest(cfg, mcfg)
    assert first == manifest.build_manifest(cfg, mcfg)
    assert first == (cfg.data_dir / "manifest.json").read_bytes()


def test_exactly_200_tasks_and_strata_match_allocation(pinned: dict[str, object]) -> None:
    tasks = _tasks(pinned)
    strata = pinned["strata"]
    assert isinstance(strata, dict)
    assert len(tasks) == 200 == sum(s["allocation"] for s in strata.values())
    counts = Counter(t["difficulty"] for t in tasks)
    assert {s: counts.get(s, 0) for s in strata} == {s: v["allocation"] for s, v in strata.items()}


def test_no_excluded_id_is_sampled_and_all_are_in_the_frame(ctx: verify.DataContext, pinned: dict[str, object]) -> None:
    exclusions = pinned["exclusions"]
    assert isinstance(exclusions, list)
    excluded = {e["question_id"] for e in exclusions}
    frame = set(ctx.platfull_by_id) - excluded
    sampled = [str(t["question_id"]) for t in _tasks(pinned)]
    assert len(frame) == pinned["frame_size"]
    assert not excluded & set(sampled)
    assert set(sampled) <= frame
    assert sampled == verify.sort_ids(sampled)


def test_reload_recomputes_task_and_gold_result_hashes(ctx: verify.DataContext, pinned: dict[str, object]) -> None:
    mismatched: list[str] = []
    for t in _tasks(pinned):
        qid = str(t["question_id"])
        rec = ctx.platfull_by_id[qid]
        rows = execute_with_deadline(ctx.connection(str(rec["db_id"])), str(rec["SQL"]), ctx.cfg.gold_timeout_s)
        if (manifest.task_hash(rec), manifest.result_hash(rows), len(rows)) != (
                t["task_hash"], t["gold_result_hash"], t["gold_row_count"]):
            mismatched.append(qid)
    assert not mismatched, f"hash mismatch for {mismatched}"
