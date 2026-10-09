"""Client-side tool layer: canonical manifest, startup check, validation, result classification and
model-facing rendering. The single validation site; pure functions, no state across calls.

Argument structure is code-defined here (it must equal what the server lists); descriptions come
from the active prompt version. The server never re-validates argument shape, so a server-side
validation failure on a call validated here is a harness bug and raises.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import mcp_types as types
from jsonschema import Draft202012Validator

from escalator.agent.prompts import Prompt, PromptError
from escalator.llm.types import JsonDict, ToolCall, ToolSpec
from escalator.trace.schema import ToolStatus
from escalator.util import canon

GET_SCHEMA = "get_schema"
SAMPLE_ROWS = "sample_rows"
EXECUTE_SQL = "execute_sql"
SUBMIT_ANSWER = "submit_answer"
RESULT_SET_TOOLS = frozenset({SAMPLE_ROWS, EXECUTE_SQL, SUBMIT_ANSWER})

# Server error kinds (env/server.py) a validated call can legitimately produce.
_SERVER_STATUS: dict[str, ToolStatus] = {
    "sql_error": "execution_error",
    "unknown_table": "execution_error",  # table names are free strings: a model error, not ours
    "timeout": "timeout",
    "forbidden": "denied",
}

IGNORED_AFTER_SUBMIT = canon.dumps({"error_type": "ignored_after_submit"}).decode("utf-8")


class HarnessError(RuntimeError):
    """A failure of the harness, not of the model: never retried, never scored."""


class ManifestMismatch(HarnessError):
    """The server's tools differ from the canonical ones."""


@dataclass(frozen=True)
class RenderCaps:
    max_rows: int = 50
    max_chars: int = 4000


# --- canonical manifest ------------------------------------------------------


def db_ids_under(db_root: Path) -> list[str]:
    """The database ids the server exposes for this --db-root (same rule as env.server.open_sandbox)."""
    return sorted({p.stem for p in db_root.rglob("*.sqlite")})


def input_schemas(db_ids: Sequence[str]) -> dict[str, JsonDict]:
    """Argument structure of the four tools; the db_id enum is the sorted database ids."""

    def db_id() -> JsonDict:
        return {"type": "string", "enum": sorted(db_ids)}

    def obj(props: JsonDict, required: list[str]) -> JsonDict:
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    return {
        GET_SCHEMA: obj({"db_id": db_id(), "tables": {"type": "array", "items": {"type": "string"}}}, ["db_id"]),
        SAMPLE_ROWS: obj({"db_id": db_id(), "table": {"type": "string"}}, ["db_id", "table"]),
        EXECUTE_SQL: obj({"db_id": db_id(), "sql": {"type": "string"}}, ["db_id", "sql"]),
        SUBMIT_ANSWER: obj({"db_id": db_id(), "sql": {"type": "string"}}, ["db_id", "sql"]),
    }


def build_manifest(db_ids: Sequence[str], prompt: Prompt) -> tuple[ToolSpec, ...]:
    """Schemas plus the prompt's tool and parameter descriptions, sorted by tool name."""
    schemas = input_schemas(db_ids)
    if set(prompt.tools) != set(schemas):
        raise PromptError(f"prompt {prompt.version}: tool_descriptions must cover exactly {sorted(schemas)}")
    out: list[ToolSpec] = []
    for name in sorted(schemas):
        text = prompt.tools[name]
        schema = copy.deepcopy(schemas[name])
        props = schema["properties"]
        assert isinstance(props, dict)
        if set(text.parameters) != set(props):
            raise PromptError(f"prompt {prompt.version}: {name} parameters must be exactly {sorted(props)}")
        for param, desc in text.parameters.items():
            prop = props[param]
            assert isinstance(prop, dict)
            prop["description"] = desc
        out.append(ToolSpec(name=name, description=text.description, input_schema=schema))
    return tuple(out)


def manifest_sha256(tools: Iterable[ToolSpec]) -> str:
    """Canonical: tools sorted by name, keys sorted, no insignificant whitespace."""
    return canon.sha256([t.model_dump(mode="json") for t in sorted(tools, key=lambda t: t.name)])


def _strip_descriptions(value: object) -> object:
    if isinstance(value, dict):
        return {k: _strip_descriptions(v) for k, v in value.items() if not (k == "description" and isinstance(v, str))}
    if isinstance(value, list):
        return [_strip_descriptions(v) for v in value]
    return value


def check_server_tools(listed: Sequence[types.Tool], db_ids: Sequence[str]) -> None:
    """After initialize and tools/list: names and input schemas must equal the canonical ones."""
    want = {name: _strip_descriptions(s) for name, s in input_schemas(db_ids).items()}
    got = {t.name: _strip_descriptions(t.input_schema) for t in listed}
    if len(got) != len(listed):
        raise ManifestMismatch(f"server lists duplicate tool names: {sorted(t.name for t in listed)}")
    if set(got) != set(want):
        raise ManifestMismatch(f"server tools {sorted(got)} != canonical {sorted(want)}")
    for name in sorted(want):
        if canon.dumps(got[name]) != canon.dumps(want[name]):
            raise ManifestMismatch(f"{name}: server input schema differs from the canonical one")


# --- validation --------------------------------------------------------------


def _invalid(tool: str, detail: JsonDict) -> JsonDict:
    return {"error_type": "validation_error", "tool": tool, "detail": detail}


def validate(call: ToolCall, schemas: Mapping[str, JsonDict]) -> JsonDict | None:
    """None when the call is valid; otherwise a typed, deterministic error object for the model."""
    schema = schemas.get(call.name)
    if schema is None:
        return _invalid(call.name, {"reason": "unknown_tool"})
    if call.args is None:
        return _invalid(call.name, {"reason": "unparsable_arguments"})
    errors = sorted(
        ({"path": "/" + "/".join(str(p) for p in e.absolute_path), "message": e.message}
         for e in Draft202012Validator(schema).iter_errors(call.args)),
        key=lambda d: (d["path"], d["message"]),
    )
    if errors:
        return _invalid(call.name, {"reason": "schema", "errors": list(errors)})
    return None


def error_text(error: JsonDict) -> str:
    return canon.dumps(error).decode("utf-8")


# --- server results ----------------------------------------------------------


def server_text(name: str, result: types.CallToolResult) -> str:
    if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
        raise HarnessError(f"{name}: expected one text content block from the server")
    return result.content[0].text


def classify(name: str, result: types.CallToolResult) -> tuple[ToolStatus, str]:
    """`tool_status` and the server's text. A rejection of an already-validated call raises."""
    text = server_text(name, result)
    if not result.is_error:
        return "ok", text
    try:
        obj: object = json.loads(text)
    except ValueError:
        obj = None
    kind = obj.get("error") if isinstance(obj, dict) and set(obj) == {"error", "message"} else None
    status = _SERVER_STATUS.get(kind) if isinstance(kind, str) else None
    if status is None:
        raise HarnessError(f"server rejected an already-validated {name} call: {text[:500]}")
    return status, text


def render_result(name: str, status: ToolStatus, text: str, caps: RenderCaps) -> str:
    """The text the model sees. Result sets are re-serialized with display caps and explicit counts;
    the schema card and error objects pass through unchanged."""
    if status != "ok" or name not in RESULT_SET_TOOLS:
        return text
    obj: object = json.loads(text)
    if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
        raise HarnessError(f"{name}: unexpected result shape from the server")
    rows: list[object] = list(obj["rows"])[: caps.max_rows]
    n_rows = obj.get("n_rows")
    if not isinstance(n_rows, int):
        raise HarnessError(f"{name}: result has no n_rows")

    def dump(shown: list[object]) -> str:
        return canon.dumps({
            "columns": obj.get("columns"),
            "rows": shown,
            "n_rows": n_rows,
            "n_rows_is_lower_bound": obj.get("n_rows_is_lower_bound"),
            "rows_shown": len(shown),
            "truncated": len(shown) < n_rows,
        }).decode("utf-8")

    out = dump(rows)
    while len(out) > caps.max_chars and rows:
        rows.pop()
        out = dump(rows)
    return out
