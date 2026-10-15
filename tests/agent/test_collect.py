"""eval.collect: self-consistency, verifier and frontier-repeat collection, the compatibility gate and the
guards (week-3 spec tests 1, 2 and 4). Real MCP server on the fixture db, scripted providers, no network.

Test 1's "identical at concurrency 1 vs 4" is dropped: collection is sequential (week-2 decision A3)."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from agent_helpers import (
    DB_IDS,
    LARGE,
    SMALL,
    ScriptedProvider,
    Server,
    anthropic_body,
    make_adapter,
    prices,
    sql_args,
    step_of,
)
from runner_helpers import RUN_ID, TASKS, run_preset, scripted

from escalator.agent.prompts import (
    PLACEHOLDER,
    SLOTS,
    Prompt,
    ToolText,
    _SLOT,
    has_placeholders,
    load_prompt,
    load_prompt_with_tools,
    parse_prompt_with_tools,
    placeholder_fields,
)
from escalator.agent.tools import build_manifest, manifest_sha256
from escalator.eval import collect as collect_mod
from escalator.eval.collect import Job, JobReport, collect, plan_job, sc_samples
from escalator.eval.compat import MAY_DIFFER, MUST_MATCH, CompatError
from escalator.eval.runner import ENV_LOCK, RunRefused, execute_run, load_agent_config
from escalator.eval.scorers import Score
from escalator.eval.tasks import Task
from escalator.llm.cache import CacheMode
from escalator.llm.models import ModelSpec
from escalator.llm.providers.base import Provider
from escalator.llm.types import Request
from escalator.router.config import RouterConfig, parse_router_config
from escalator.trace.schema import META_FILE, STEPS_FILE, RunMeta, StepRecord, read_meta, read_steps

LARGE_RUN_ID = f"t-{LARGE.name}-v1"
RCFG: RouterConfig = parse_router_config({
    "self_consistency": {"k": 3, "temperature": 0.7, "seeds": [1, 2, 3], "models": [SMALL.name]},
    "verifier": {"prompt_version": "verifier_t", "models": [SMALL.name]},
    "inventory": {"small": {SMALL.name: {"baseline": RUN_ID, "self_consistency": None, "verifier": None}},
                  "frontier": {"model": LARGE.name, "baseline": LARGE_RUN_ID, "repeat": None}},
    "bootstrap": {"B": 10, "seed": 1},
})
FILLED_VERIFIER = b"""
system: "Check the answer to a question about {{db_id}}.\\n\\n{{schema_card}}"
user: "Question: {{question}}{{evidence_section}}"
evidence_section: "\\n\\nExternal knowledge: {{evidence}}"
"""


def env_lock() -> dict[str, str]:
    return {str(k): str(v) for k, v in json.loads(ENV_LOCK.read_text(encoding="utf-8")).items()}


def verifier_prompt(data: bytes = FILLED_VERIFIER) -> Prompt:
    return parse_prompt_with_tools(data, "verifier_t", load_prompt("v1"))


def run_collect(server: Server, tmp_path: Path, provider: Provider, jobs: Sequence[Job], *,
                cache_mode: CacheMode = "readwrite", overwrite: bool = False) -> list[JobReport]:
    cache = server.cache(tmp_path / "llm.sqlite", cache_mode)

    async def go() -> list[JobReport]:
        return await collect(jobs, TASKS, llm=make_adapter(provider, cache), prices=prices(),
                             cfg=load_agent_config(), session=server.session, server_version=server.version,
                             db_ids=DB_IDS, traces_root=tmp_path / "traces", overwrite=overwrite, env_lock=env_lock(),
                             num_parallel="1")

    try:
        return server.run(go)
    finally:
        server.portal.call(cache.close)


def small_job(kind: str = "self_consistency", *, spec: ModelSpec = SMALL, prompt: Prompt | None = None) -> Job:
    p = prompt or (verifier_prompt() if kind == "verifier" else load_prompt("v1"))
    return plan_job(kind, spec, rcfg=RCFG, cfg=load_agent_config(), prompt=p, smoke=False)  # type: ignore[arg-type]


def steps(tmp_path: Path, run_id: str) -> list[StepRecord]:
    return read_steps(tmp_path / "traces" / run_id / STEPS_FILE)


def meta(tmp_path: Path, run_id: str) -> RunMeta:
    return read_meta(tmp_path / "traces" / run_id / META_FILE)


def first_keys(records: Sequence[StepRecord]) -> dict[tuple[str, int], str]:
    return {(r.task_id, r.sample_idx): r.request_key for r in records if r.step == 0}


# --- test 1: self-consistency collection ----------------------------------------------


def test_sc_samples_record_sample_idx_seeds_and_distinct_keys(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    provider = scripted()
    job = small_job()
    [report] = run_collect(server, tmp_path, provider, [job])
    assert job.run_id == f"sc-{SMALL.name}-v1" and len(report.outcomes) == 3 * len(TASKS)

    recs = steps(tmp_path, job.run_id)
    assert [(int(r.task_id), r.sample_idx, r.step) for r in recs] == sorted(
        (int(r.task_id), r.sample_idx, r.step) for r in recs)
    assert {(r.task_id, r.sample_idx) for r in recs} == {(t.question_id, j) for t in TASKS for j in (0, 1, 2)}

    # Each request carries T = 0.7 and the seed of its sample.
    assert {(req.sample_idx, req.temperature, req.seed) for req in provider.requests} == {
        (0, 0.7, 1), (1, 0.7, 2), (2, 0.7, 3)}

    # Distinct seeds give distinct keys, and none replays the baseline's request.
    keys = first_keys(recs)
    base = first_keys(steps(tmp_path, RUN_ID))
    for t in TASKS:
        k = [keys[(t.question_id, j)] for j in (0, 1, 2)]
        assert len(set(k)) == 3 and base[(t.question_id, 0)] not in k

    m = meta(tmp_path, job.run_id)
    assert m.schema_version == 3 and m.run_kind == "self_consistency" and m.baseline_run_id == RUN_ID
    assert m.temperature == 0.7 and m.seed is None and m.sample_idxs == [0, 1, 2]
    assert m.sample_seeds == {"0": 1, "1": 2, "2": 3} and m.ollama_num_parallel == "1"


def test_sc_samples_resume_through_the_cache(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    job = small_job()
    run_collect(server, tmp_path, scripted(), [job])
    first = steps(tmp_path, job.run_id)

    again = scripted()
    run_collect(server, tmp_path, again, [job], cache_mode="readonly", overwrite=True)
    second = steps(tmp_path, job.run_id)
    assert again.calls == 0 and all(r.cache_hit for r in second)
    strip = [r.model_copy(update={"cache_hit": False}) for r in second]
    assert strip == [r.model_copy(update={"cache_hit": False}) for r in first]


def test_existing_run_is_refused_without_overwrite(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    run_collect(server, tmp_path, scripted(), [small_job()])
    with pytest.raises(RunRefused, match="already holds a run"):
        run_collect(server, tmp_path, scripted(), [small_job()])


def test_anthropic_samples_start_at_1() -> None:
    haiku = ModelSpec("haiku", "S", "anthropic", None, None, "claude-haiku-5-5", {})
    assert [(s.sample_idx, s.temperature, s.seed) for s in sc_samples(haiku, RCFG)] == [
        (1, None, None), (2, None, None), (3, None, None)]


# --- test 2: compatibility gate ----------------------------------------------------------


def _changed_tools(p: Prompt) -> Prompt:
    tools = dict(p.tools)
    tools["execute_sql"] = ToolText(tools["execute_sql"].description + " Changed.", tools["execute_sql"].parameters)
    return dataclasses.replace(p, tools=tools)


@pytest.mark.parametrize(("change", "fields"), [
    ("prompt", {"prompt_sha256"}),
    ("digest", {"model_digests", "tier_config"}),
    ("num_ctx", {"tier_config"}),
    ("tool_manifest", {"tool_manifest_sha256"}),
])
def test_gate_aborts_before_any_llm_call_with_a_diff(server: Server, tmp_path: Path, change: str,
                                                     fields: set[str]) -> None:
    run_preset(server, tmp_path, scripted())
    v1 = load_prompt("v1")
    spec, prompt = SMALL, v1
    if change == "prompt":
        prompt = dataclasses.replace(v1, sha256="0" * 64)
    elif change == "digest":
        spec = dataclasses.replace(SMALL, digest="d" * 64)
    elif change == "num_ctx":
        spec = dataclasses.replace(SMALL, settings={**SMALL.settings, "num_ctx": 8192})
    else:
        prompt = _changed_tools(v1)
    provider = scripted()
    job = small_job(spec=spec, prompt=prompt)
    with pytest.raises(CompatError) as err:
        run_collect(server, tmp_path, provider, [job])
    assert provider.calls == 0
    assert not (tmp_path / "traces" / job.run_id).exists()
    for f in fields:
        assert f"  {f} (must match)" in str(err.value)
    assert "configs_sha256" not in str(err.value)


def test_gate_checks_every_job_before_the_first_call(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    provider = scripted()
    bad = small_job(prompt=dataclasses.replace(load_prompt("v1"), sha256="0" * 64))
    with pytest.raises(CompatError):
        run_collect(server, tmp_path, provider, [small_job(), bad])
    assert provider.calls == 0


def test_every_meta_field_is_classified() -> None:
    assert MUST_MATCH.isdisjoint(MAY_DIFFER)
    assert MUST_MATCH | MAY_DIFFER == set(RunMeta.model_fields)


# --- verifier and frontier repeat ----------------------------------------------------------


def test_verifier_run_passes_the_gate_with_its_own_prompt(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    provider = scripted()
    job = small_job("verifier")
    [report] = run_collect(server, tmp_path, provider, [job])
    assert len(report.outcomes) == len(TASKS)
    m, base = meta(tmp_path, job.run_id), meta(tmp_path, RUN_ID)
    assert m.run_kind == "verifier" and m.prompt_version == "verifier_t" and m.prompt_sha256 != base.prompt_sha256
    assert m.tool_manifest_sha256 == base.tool_manifest_sha256
    assert (m.temperature, m.seed, m.sample_idxs) == (base.temperature, base.seed, [0])


def test_verifier_with_the_baseline_prompt_is_refused(server: Server, tmp_path: Path) -> None:
    run_preset(server, tmp_path, scripted())
    provider = scripted()
    job = dataclasses.replace(small_job("verifier"), prompt=load_prompt("v1"))
    with pytest.raises(CompatError, match=r"prompt_sha256 \(must differ\)"):
        run_collect(server, tmp_path, provider, [job])
    assert provider.calls == 0


def frontier_script(req: Request) -> bytes:
    return anthropic_body([("submit_answer", sql_args("SELECT 1"))], step=step_of(req))


def always_right(_task: Task, _sql: str) -> Score:
    return Score(True, 1.0, None)


def test_frontier_repeat_is_sample_1_with_new_keys(server: Server, tmp_path: Path) -> None:
    cache = server.cache(tmp_path / "llm.sqlite")
    base_provider = ScriptedProvider("anthropic", frontier_script)

    async def baseline() -> None:
        await execute_run(preset="t", specs=[LARGE], tasks=TASKS, llm=make_adapter(base_provider, cache),
                          prices=prices(), prompt=load_prompt("v1"), cfg=load_agent_config(), session=server.session,
                          server_version=server.version, db_ids=DB_IDS, traces_root=tmp_path / "traces",
                          overwrite=False, score=always_right, ollama_version=lambda: None)

    try:
        server.run(baseline)
    finally:
        server.portal.call(cache.close)

    provider = ScriptedProvider("anthropic", frontier_script)
    job = plan_job("frontier_repeat", LARGE, rcfg=RCFG, cfg=load_agent_config(), prompt=load_prompt("v1"), smoke=False)
    run_collect(server, tmp_path, provider, [job])
    assert provider.calls == len(TASKS)  # sample 1 never hits sample 0's cache entry
    assert {(r.sample_idx, r.temperature, r.seed) for r in provider.requests} == {(1, None, None)}
    rep, base = first_keys(steps(tmp_path, job.run_id)), first_keys(steps(tmp_path, LARGE_RUN_ID))
    for t in TASKS:
        assert rep[(t.question_id, 1)] != base[(t.question_id, 0)]
    assert meta(tmp_path, job.run_id).run_kind == "frontier_repeat"


# --- test 4: guards -------------------------------------------------------------------------


# verifier_v1.yaml as committed in ca36d4f, before the owner wrote its prose (d034aa6).
PLACEHOLDER_VERIFIER = b"""system: |-
  <<PROMPT_PLACEHOLDER: system>>
  {{db_id}}
  {{schema_card}}

user: "<<PROMPT_PLACEHOLDER: user>> {{question}}{{evidence_section}}"

evidence_section: "<<PROMPT_PLACEHOLDER: evidence_section>> {{evidence}}"
"""


def test_a_placeholder_verifier_holds_only_placeholders_and_slots() -> None:
    v = parse_prompt_with_tools(PLACEHOLDER_VERIFIER, "verifier_v1", load_prompt("v1"))
    assert placeholder_fields(v) == ["evidence_section", "system", "user"]
    for name in SLOTS:
        rest = _SLOT.sub("", PLACEHOLDER.sub("", getattr(v, name)))
        assert rest.strip() == "", f"{name} holds prose: {rest!r}"
    assert not has_placeholders(load_prompt("v1"))


def test_written_verifier_has_no_placeholders() -> None:
    assert placeholder_fields(load_prompt_with_tools("verifier_v1", load_prompt("v1"))) == []


def test_verifier_manifest_equals_the_baseline() -> None:
    v1 = load_prompt("v1")
    v = load_prompt_with_tools("verifier_v1", v1)
    assert v.sha256 != v1.sha256
    assert manifest_sha256(build_manifest(DB_IDS, v)) == manifest_sha256(build_manifest(DB_IDS, v1))


def test_verifier_runs_refuse_a_placeholder_prompt(monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(collect_mod, "load_prompt_with_tools",
                        lambda version, base: parse_prompt_with_tools(PLACEHOLDER_VERIFIER, version, base))
    assert collect_mod.main(["verifier-runs"]) == 2
    err = capsys.readouterr().err
    assert "placeholders" in err and "'system'" in err


def _fake_frontier_baseline(root: Path, usd: Sequence[str]) -> None:
    """A minimal published-layout baseline: the projected cost reads only its usd."""
    run_dir = root / "baselines-sonnet-5-5-v1"
    run_dir.mkdir(parents=True)
    m = RunMeta.model_validate({
        "run_id": "baselines-sonnet-5-5-v1", "created_at": "x", "git_sha": "x", "git_dirty": False,
        "prices_sha256": "x", "prices_retrieved_on": "2026-10-14", "model_digests": {}, "tier_config": {
            "name": "sonnet-5-5", "tier": "L", "provider": "anthropic", "model_id": "claude-sonnet-5-5",
            "settings": {}}, "prompt_version": "v1", "prompt_sha256": "x", "tool_manifest_sha256": "x",
        "mcp_server_version": "1", "mcp_sdk_version": "x", "temperature": None, "seed": None, "sample_idxs": [0],
        "max_steps": 8, "retry_cap": 2, "render_max_rows": 50, "render_max_chars": 4000, "overflow_margin": 1024,
        "max_tokens": 16000, "configs_sha256": "x", "env_lock": {}, "manifest_sha256": "x", "cache_schema": 1,
        "ollama_version": None})
    (run_dir / META_FILE).write_text(m.model_dump_json(), encoding="utf-8")
    lines = []
    for i, u in enumerate(usd):
        lines.append(StepRecord(
            run_id=m.run_id, task_id=str(i + 1), tier="L", model="claude-sonnet-5-5", step=0, request_key="k",
            cache_hit=False, stop_reason="tool_use", tool=[], args=[], args_raw=[], n_tool_calls=0, tool_status=[],
            result_hash=[], result_preview=[], text="", tokens_in=1, tokens_out=1, tokens_cache_read=0,
            tokens_cache_write=0, latency_ms=1, usd=u, end="submitted").model_dump_json())
    (run_dir / STEPS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_frontier_repeat_without_paid_exits_with_the_projected_cost(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_frontier_baseline(tmp_path, ["0.50", "0.25", "1.125"])
    monkeypatch.setenv("ESCALATOR_TRACES_DIR", str(tmp_path))
    monkeypatch.setenv("ESCALATOR_CACHE_MODE", "readwrite")
    assert collect_mod.main(["frontier-repeat"]) == 2
    out = capsys.readouterr()
    assert f"projected cost ${Decimal('1.875')}" in out.out
    assert "need --paid" in out.err
