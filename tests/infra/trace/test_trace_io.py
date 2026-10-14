"""U3: trace round-trip, strict schema, and a crashed run reported as TruncatedTrace."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from escalator.trace.schema import STEPS_FILE, StepRecord, TraceError, TraceWriter, TruncatedTrace, read_steps


def _step(step: int, **kw: object) -> StepRecord:
    fields: dict[str, object] = dict(
        run_id="r1", task_id="42", tier="S", model="ollama/gemma4:e4b-it-qat@" + "a" * 64, step=step,
        request_key="k" * 64, cache_hit=False, stop_reason="tool_use", tool=["execute_sql"],
        args=[{"sql": "SELECT 'é', 1.5"}], args_raw=[None], n_tool_calls=1, tool_status=["ok"],
        result_hash=["h" * 64], result_preview=["[[1]]"], text="", tokens_in=812, tokens_out=45,
        tokens_cache_read=0, tokens_cache_write=0, latency_ms=1820, usd="0",
    )
    fields.update(kw)
    return StepRecord.model_validate(fields)


RECORDS = [
    _step(0),
    _step(1, tool=["execute_sql"], args=[None], args_raw=['{"sql": '], tool_status=["validation_error"],
          usd="0.008138"),
    _step(2, tool=["submit_answer", "execute_sql"], args=[{"sql": "SELECT 1"}, {"sql": "SELECT 2"}],
          args_raw=[None, None], n_tool_calls=2, tool_status=["ok", "ignored"], result_hash=["h" * 64, None],
          result_preview=["[[1]]", None], end="submitted"),
    _step(3, stop_reason=None, tool=[], args=[], args_raw=[], n_tool_calls=0, tool_status=[], result_hash=[],
          result_preview=[], tokens_in=None, tokens_out=None, tokens_cache_read=None, tokens_cache_write=None,
          latency_ms=None, usd=None, end="provider_error"),
]


def test_round_trip(tmp_path: Path) -> None:
    with TraceWriter(tmp_path) as w:
        for r in RECORDS:
            w.append(r)
    assert read_steps(tmp_path / STEPS_FILE) == RECORDS


def test_unknown_field_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _step(0, surprise=1)
    line = json.loads(RECORDS[0].model_dump_json())
    line["surprise"] = 1
    (tmp_path / STEPS_FILE).write_text(json.dumps(line) + "\n", encoding="utf-8")
    with pytest.raises(TraceError):
        read_steps(tmp_path / STEPS_FILE)


def test_per_call_lists_must_agree() -> None:
    with pytest.raises(ValidationError):
        _step(0, n_tool_calls=2)
    with pytest.raises(ValidationError):
        _step(0, args_raw=["{}"])  # args parsed, so args_raw must be null


def test_final_line_without_newline_is_truncated(tmp_path: Path) -> None:
    complete = RECORDS[0].model_dump_json() + "\n"
    partial = RECORDS[1].model_dump_json()[:40]
    (tmp_path / STEPS_FILE).write_text(complete + partial, encoding="utf-8")
    with pytest.raises(TruncatedTrace):
        read_steps(tmp_path / STEPS_FILE)
