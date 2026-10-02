"""Trace records: one StepRecord per LLM call in traces/<run_id>/steps.jsonl, one RunMeta in meta.json.

Every metric, the routing simulation and the failure taxonomy read these files and nothing else.
Nothing nondeterministic beyond latency_ms enters a record.
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

SCHEMA_VERSION = 1
STEPS_FILE = "steps.jsonl"
META_FILE = "meta.json"
PREVIEW_BYTES = 2048

ToolStatus = Literal["ok", "validation_error", "execution_error", "timeout", "denied"]
End = Literal["submitted", "max_steps", "no_tool_call", "provider_error"]


class TraceError(ValueError):
    """A malformed trace line."""


class TruncatedTrace(TraceError):
    """The final line has no newline: the run crashed mid-write."""


class StepRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    task_id: str  # str(question_id)
    tier: Literal["S", "L"]
    model: str
    sample_idx: int = 0
    step: int  # 0-based per (run_id, task_id, tier, sample_idx)
    request_key: str
    cache_hit: bool
    stop_reason: StopReason
    tool: str | None
    args: JsonDict | None
    args_raw: str | None  # set only when args is None
    n_tool_calls: int
    tool_status: ToolStatus | None
    result_hash: str | None  # sha256 of canon.dumps(result) as the model saw it, row order kept
    result_preview: str | None
    text: str
    tokens_in: int | None
    tokens_out: int | None
    tokens_cache_read: int
    tokens_cache_write: int
    latency_ms: int
    usd: str  # nominal, decimal string
    end: End | None = None

    @model_validator(mode="after")
    def _args_raw_only_when_unparsed(self) -> StepRecord:
        if self.args is not None and self.args_raw is not None:
            raise ValueError("args_raw is set only when args is null")
        return self


class RunMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    created_at: str
    git_sha: str
    git_dirty: bool
    prices_sha256: str
    prices_retrieved_on: str
    model_digests: dict[str, str]  # tier name -> exact model id
    prompt_version: str
    configs_sha256: str
    env_lock: dict[str, str]
    manifest_sha256: str
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


# --- RunMeta construction (untested in part 1) -----------------------------


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
    prompt_version: str,
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
        prompt_version=prompt_version,
        configs_sha256=configs_sha256(),
        env_lock=env_lock,
        manifest_sha256=_sha256_file(manifest),
        cache_schema=cache_schema,
        ollama_version=ollama_version,
    )
