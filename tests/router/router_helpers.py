"""Hand-built trace records, runs, metas and executors for the router tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from escalator.env.sandbox import Result, SqlError, SqlValue
from escalator.router.inventory import Run, RunSet, group_runs
from escalator.trace.schema import End, RunMeta, StepRecord, ToolStatus

FIXTURE_DB = "fixture"


def record(run_id: str, task_id: str, step: int, *, sample_idx: int = 0, tier: str = "S",
           calls: Sequence[tuple[str, str | None, ToolStatus]] = (), end: End | None = None,
           usd: str = "0", tokens: tuple[int, int] = (100, 10), latency_ms: int = 1000) -> StepRecord:
    """`calls`: (tool, sql or None, status); sql None -> args None (unparsable)."""
    failed = end == "provider_error"
    return StepRecord(
        run_id=run_id, task_id=task_id, tier=tier, model="m", sample_idx=sample_idx, step=step,  # type: ignore[arg-type]
        request_key=f"{run_id}/{task_id}/{sample_idx}/{step}", cache_hit=False,
        stop_reason=None if failed else ("tool_use" if calls else "end_turn"),
        tool=[c[0] for c in calls],
        args=[None if c[1] is None else {"db_id": FIXTURE_DB, "sql": c[1]} for c in calls],
        args_raw=["{" if c[1] is None else None for c in calls],
        n_tool_calls=len(calls), tool_status=[c[2] for c in calls],
        result_hash=[None for _ in calls], result_preview=[None for _ in calls], text="",
        tokens_in=None if failed else tokens[0], tokens_out=None if failed else tokens[1],
        tokens_cache_read=None if failed else 0, tokens_cache_write=None if failed else 0,
        latency_ms=None if failed else latency_ms, usd=None if failed else usd, end=end,
    )


def submitted(sql: str, *, task_id: str = "1", sample_idx: int = 0, run_id: str = "r", status: ToolStatus = "ok",
              usd: str = "0", explore: bool = True, latency_ms: int = 1000, tokens: tuple[int, int] = (100, 10),
              tier: str = "S") -> list[StepRecord]:
    """An explore step (optional), then the submit."""
    recs: list[StepRecord] = []
    if explore:
        recs.append(record(run_id, task_id, 0, sample_idx=sample_idx, calls=[("execute_sql", "SELECT 0", "ok")],
                           usd=usd, latency_ms=latency_ms, tokens=tokens, tier=tier))
    recs.append(record(run_id, task_id, len(recs), sample_idx=sample_idx, calls=[("submit_answer", sql, status)],
                       end="submitted", usd=usd, latency_ms=latency_ms, tokens=tokens, tier=tier))
    return recs


def ended(end: End, *, task_id: str = "1", sample_idx: int = 0, run_id: str = "r", usd: str = "0",
          tier: str = "S") -> list[StepRecord]:
    calls: list[tuple[str, str | None, ToolStatus]] = []
    if end == "max_steps":
        calls = [("execute_sql", "SELECT 0", "ok")]
    elif end == "validation_exhausted":
        calls = [("execute_sql", None, "validation_error")]
    elif end == "context_overflow":
        calls = [("execute_sql", "SELECT 0", "ignored")]
    return [record(run_id, task_id, 0, sample_idx=sample_idx, calls=calls, end=end, usd=usd, tier=tier)]


def run_of(records: Sequence[StepRecord]) -> Run:
    [runs] = group_runs(records[0].run_id, records).values()
    [run] = runs
    return run


META_BASE: dict[str, object] = {
    "created_at": "2026-10-15T00:00:00+00:00", "git_sha": "a" * 40, "git_dirty": False, "prices_sha256": "p",
    "prices_retrieved_on": "2026-10-14", "prompt_version": "v1", "prompt_sha256": "s", "tool_manifest_sha256": "t",
    "mcp_server_version": "1", "mcp_sdk_version": "2.3.0", "max_steps": 8, "retry_cap": 2, "render_max_rows": 50,
    "render_max_chars": 4000, "overflow_margin": 1024, "max_tokens": 2048, "configs_sha256": "c",
    "env_lock": {}, "manifest_sha256": "m", "cache_schema": 1, "ollama_version": None,
}


def meta(run_id: str, model: str, *, tier: str = "S", provider: str = "ollama", **kw: object) -> RunMeta:
    fields: dict[str, object] = {
        **META_BASE, "run_id": run_id, "model_digests": {model: model},
        "tier_config": {"name": model, "tier": tier, "provider": provider, "model_id": model, "settings": {}},
        "temperature": 0.0 if provider == "ollama" else None, "seed": 20261001 if provider == "ollama" else None,
        "sample_idxs": [0],
    }
    fields.update(kw)
    return RunMeta.model_validate(fields)


def runset(run_id: str, model: str, records: Sequence[StepRecord], **meta_kw: object) -> RunSet:
    return RunSet(run_id, meta(run_id, model, **meta_kw), "0" * 64, group_runs(run_id, records))


class FakeExecutor:
    """sql -> outcome; counts calls. Rows are given directly, so the comparison semantics are tested
    without a database."""

    def __init__(self, outcomes: Mapping[str, Sequence[Sequence[SqlValue]] | SqlError]) -> None:
        self.outcomes = dict(outcomes)
        self.calls: list[str] = []

    def execute(self, db_id: str, sql: str) -> Result | SqlError:
        self.calls.append(sql)
        out = self.outcomes[sql]
        if isinstance(out, SqlError):
            return out
        return Result(columns=[f"c{i}" for i in range(len(out[0]) if out else 0)],
                      rows=[list(r) for r in out], truncated=False)
