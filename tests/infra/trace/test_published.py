"""trace/published.py: deterministic gzipped publication, published-first lookup, and RunMeta v3 reading
week-2 (v2) meta files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_trace_io import RECORDS

from escalator.trace.published import STEPS_GZ, RunNotFound, load_meta, load_steps, publish, run_dir
from escalator.trace.schema import META_FILE, STEPS_FILE, RunMeta, TraceWriter, TruncatedTrace

V2_META: dict[str, object] = {
    "schema_version": 2, "run_id": "r1", "created_at": "2026-10-14T06:19:24+00:00", "git_sha": "a" * 40,
    "git_dirty": False, "prices_sha256": "p", "prices_retrieved_on": "2026-10-14", "model_digests": {"m": "id"},
    "tier_config": {"name": "m", "tier": "S", "provider": "ollama", "model_id": "id", "settings": {"num_ctx": 1}},
    "prompt_version": "v1", "prompt_sha256": "s", "tool_manifest_sha256": "t", "mcp_server_version": "1",
    "mcp_sdk_version": "2.3.0", "temperature": 0.0, "seed": 1, "sample_idxs": [0], "max_steps": 8, "retry_cap": 2,
    "render_max_rows": 50, "render_max_chars": 4000, "overflow_margin": 1024, "max_tokens": 2048,
    "configs_sha256": "c", "env_lock": {"python_version": "3.12.14"}, "manifest_sha256": "m", "cache_schema": 1,
    "ollama_version": None,
}


def _run(root: Path, run_id: str = "r1") -> Path:
    d = root / run_id
    with TraceWriter(d) as w:
        w.write_meta(RunMeta.model_validate({**V2_META, "run_id": run_id}))
        for r in RECORDS:
            w.append(r)
    return d


def test_v2_meta_reads_as_a_baseline() -> None:
    m = RunMeta.model_validate(V2_META)
    assert m.schema_version == 2 and m.run_kind == "baseline"
    assert m.sample_seeds is None and m.baseline_run_id is None and m.ollama_modelfile_params is None


def test_publish_round_trips_and_is_deterministic(tmp_path: Path) -> None:
    src = _run(tmp_path)
    dst = publish("r1", tmp_path)
    assert dst == tmp_path / "published" / "r1"
    first = (dst / STEPS_GZ).read_bytes()
    assert (dst / META_FILE).read_bytes() == (src / META_FILE).read_bytes()
    assert publish("r1", tmp_path) == dst and (dst / STEPS_GZ).read_bytes() == first
    assert run_dir("r1", tmp_path) == dst  # published first
    assert load_steps("r1", tmp_path) == RECORDS
    assert load_meta("r1", tmp_path).run_id == "r1"
    assert (src / STEPS_FILE).is_file()  # the original run is untouched


def test_unpublished_run_is_read_in_place(tmp_path: Path) -> None:
    src = _run(tmp_path)
    assert run_dir("r1", tmp_path) == src and load_steps("r1", tmp_path) == RECORDS


def test_missing_run_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RunNotFound):
        run_dir("nope", tmp_path)
    with pytest.raises(RunNotFound):
        publish("nope", tmp_path)


def test_a_truncated_trace_is_never_published(tmp_path: Path) -> None:
    src = _run(tmp_path)
    data = (src / STEPS_FILE).read_text(encoding="utf-8")
    (src / STEPS_FILE).write_text(data.rstrip("\n"), encoding="utf-8")
    with pytest.raises(TruncatedTrace):
        publish("r1", tmp_path)
    assert not (tmp_path / "published" / "r1").exists()


def test_meta_v3_round_trip(tmp_path: Path) -> None:
    m = RunMeta.model_validate({**V2_META, "schema_version": 3, "run_kind": "self_consistency",
                                "sample_seeds": {"0": 1, "1": 2}, "baseline_run_id": "b",
                                "ollama_modelfile_params": {"stop": ["<a>", "<b>"]}, "ollama_num_parallel": "1"})
    assert RunMeta.model_validate(json.loads(m.model_dump_json())) == m
