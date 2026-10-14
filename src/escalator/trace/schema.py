"""Trace records: one StepRecord per LLM call in traces/<run_id>/steps.jsonl, one RunMeta in meta.json.

Every metric, the routing simulation and the failure taxonomy read these files and nothing else.
Nothing nondeterministic beyond latency_ms enters a record.

Schema v2 (week 2): the per-call fields are lists in call order, so a response with several tool
calls is described completely; `end` gained `validation_exhausted` and `context_overflow`, and
`tool_status` gained `ignored` (a call the harness did not execute).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from escalator.datasets.config import REPO_ROOT
from escalator.llm.types import JsonDict, StopReason

SCHEMA_VERSION = 2
STEPS_FILE = "steps.jsonl"
META_FILE = "meta.json"
PREVIEW_BYTES = 2048

ToolStatus = Literal["ok", "validation_error", "execution_error", "timeout", "denied", "ignored"]
End = Literal["submitted", "max_steps", "no_tool_call", "validation_exhausted", "context_overflow", "provider_error"]
# Ends that count as a failure of the model (week-3 error signal); provider_error is infra.
FAILED_ENDS: frozenset[End] = frozenset({"no_tool_call", "max_steps", "validation_exhausted", "context_overflow"})


class TraceError(ValueError):
    """A malformed trace line."""


class TruncatedTrace(TraceError):
    """The final line has no newline: the run crashed mid-write."""


class StepRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = SCHEMA_VERSION
    run_id: str
    task_id: str  # str(question_id)
    tier: Literal["S", "L"]
    model: str
    sample_idx: int = 0
    step: int  # 0-based per (run_id, task_id, tier, sample_idx)
    request_key: str
    cache_hit: bool
    stop_reason: StopReason | None  # None only when the call failed (end = provider_error)
    # One entry per tool call, in call order; all of length n_tool_calls.
    tool: list[str]
    args: list[JsonDict | None]  # None when the arguments are not a JSON object
    args_raw: list[str | None]  # set exactly where args is None
    n_tool_calls: int
    tool_status: list[ToolStatus]
    result_hash: list[str | None]  # sha256 of canon.dumps(rendered result) as the model saw it
    result_preview: list[str | None]
    text: str
    tokens_in: int | None
    tokens_out: int | None
    tokens_cache_read: int | None
    tokens_cache_write: int | None
    latency_ms: int | None  # None only for a failed call
    usd: str | None  # nominal, decimal string; None only for a failed call
    end: End | None = None

    @model_validator(mode="after")
    def _per_call_lists(self) -> StepRecord:
        n = self.n_tool_calls
        for name in ("tool", "args", "args_raw", "tool_status", "result_hash", "result_preview"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} has {len(getattr(self, name))} entries, n_tool_calls is {n}")
        for a, raw in zip(self.args, self.args_raw, strict=True):
            if (a is None) != (raw is not None):
                raise ValueError("args_raw is set exactly where args is null")
        return self


class TierMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str  # key of configs/models.yaml
    tier: Literal["S", "L"]
    provider: Literal["ollama", "anthropic"]
    model_id: str  # exact version, or ollama/<tag>@<digest>
    settings: JsonDict  # num_ctx / think, or thinking / effort


class RunMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = SCHEMA_VERSION
    run_id: str
    created_at: str
    git_sha: str
    git_dirty: bool
    prices_sha256: str
    prices_retrieved_on: str
    model_digests: dict[str, str]  # tier name -> exact model id
    tier_config: TierMeta
    prompt_version: str
    prompt_sha256: str
    tool_manifest_sha256: str
    mcp_server_version: str
    mcp_sdk_version: str
    temperature: float | None
    seed: int | None
    sample_idxs: list[int]
    max_steps: int
    retry_cap: int
    render_max_rows: int
    render_max_chars: int
    overflow_margin: int
    max_tokens: int
    configs_sha256: str
    env_lock: dict[str, str]
    manifest_sha256: str  # data/manifest.json
    cache_schema: int
    ollama_version: str | None


def preview(rendered: str) -> str:
    """The first 2 KB of the rendered tool result, cut on a UTF-8 character boundary."""
    return rendered.encode("utf-8")[:PREVIEW_BYTES].decode("utf-8", errors="ignore")


class TraceWriter:
    """Appends records to traces/<run_id>/steps.jsonl: one write() of a complete line, then flush."""

    def __init__(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_dir
        self._f = (run_dir / STEPS_FILE).open("a", encoding="utf-8", newline="\n")

    def write_meta(self, meta: RunMeta) -> None:
        (self.run_dir / META_FILE).write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def append(self, rec: StepRecord) -> None:
        self._f.write(rec.model_dump_json() + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> TraceWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_steps(path: Path) -> list[StepRecord]:
    """Strict: a malformed line raises TraceError; a final line without newline raises TruncatedTrace."""
    data = path.read_text(encoding="utf-8")
    if data and not data.endswith("\n"):
        n = data.count("\n") + 1
        raise TruncatedTrace(f"{path}: line {n} has no trailing newline (crashed run?)")
    out: list[StepRecord] = []
    for i, line in enumerate(data.splitlines(), 1):
        try:
            out.append(StepRecord.model_validate_json(line))
        except ValidationError as exc:
            raise TraceError(f"{path}:{i}: {exc}") from exc
    return out


def read_meta(path: Path) -> RunMeta:
    return RunMeta.model_validate_json(path.read_text(encoding="utf-8"))


# --- RunMeta construction ---------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configs_sha256(configs: Path = REPO_ROOT / "configs") -> str:
    """Hash of every file under configs/, by relative path then content."""
    h = hashlib.sha256()
    for p in sorted(q for q in configs.rglob("*") if q.is_file()):
        h.update(p.relative_to(configs).as_posix().encode("utf-8") + b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def build_run_meta(
    run_id: str,
    *,
    prices_sha256: str,
    prices_retrieved_on: dt.date,
    model_digests: dict[str, str],
    tier_config: TierMeta,
    prompt_version: str,
    prompt_sha256: str,
    tool_manifest_sha256: str,
    mcp_server_version: str,
    mcp_sdk_version: str,
    temperature: float | None,
    seed: int | None,
    sample_idxs: list[int],
    max_steps: int,
    retry_cap: int,
    render_max_rows: int,
    render_max_chars: int,
    overflow_margin: int,
    max_tokens: int,
    cache_schema: int,
    ollama_version: str | None,
    env_lock: dict[str, str],
    manifest: Path = REPO_ROOT / "data" / "manifest.json",
) -> RunMeta:
    return RunMeta(
        run_id=run_id,
        created_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        git_sha=_git("rev-parse", "HEAD").strip(),
        git_dirty=bool(_git("status", "--porcelain").strip()),
        prices_sha256=prices_sha256,
        prices_retrieved_on=prices_retrieved_on.isoformat(),
        model_digests=model_digests,
        tier_config=tier_config,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        tool_manifest_sha256=tool_manifest_sha256,
        mcp_server_version=mcp_server_version,
        mcp_sdk_version=mcp_sdk_version,
        temperature=temperature,
        seed=seed,
        sample_idxs=sample_idxs,
        max_steps=max_steps,
        retry_cap=retry_cap,
        render_max_rows=render_max_rows,
        render_max_chars=render_max_chars,
        overflow_margin=overflow_margin,
        max_tokens=max_tokens,
        configs_sha256=configs_sha256(),
        env_lock=env_lock,
        manifest_sha256=_sha256_file(manifest),
        cache_schema=cache_schema,
        ollama_version=ollama_version,
    )
