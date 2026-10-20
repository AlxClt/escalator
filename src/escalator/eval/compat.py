from __future__ import annotations

from dataclasses import dataclass

from escalator.trace.schema import RunKind, RunMeta

MUST_MATCH: frozenset[str] = frozenset({
    "prompt_version", "prompt_sha256", "tool_manifest_sha256", "mcp_server_version", "mcp_sdk_version",
    "model_digests", "tier_config", "max_steps", "retry_cap", "render_max_rows", "render_max_chars",
    "overflow_margin", "max_tokens", "manifest_sha256", "env_lock", "cache_schema", "prices_sha256",
    "prices_retrieved_on",
})
MAY_DIFFER: frozenset[str] = frozenset({
    "schema_version", "run_id", "created_at", "git_sha", "git_dirty", "configs_sha256", "temperature", "seed",
    "sample_idxs", "sample_seeds", "run_kind", "baseline_run_id", "ollama_version", "ollama_modelfile_params",
    "ollama_num_parallel",
})
PROMPT_FIELDS: frozenset[str] = frozenset({"prompt_version", "prompt_sha256"})


class CompatError(RuntimeError):
    """The new run is not the baseline's agent; the message holds a field-level diff."""


@dataclass(frozen=True)
class FieldDiff:
    field: str
    baseline: object
    new: object
    rule: str  # "must match" | "must differ" | "must equal <value>"

    def __str__(self) -> str:
        return f"  {self.field} ({self.rule}): baseline={self.baseline!r} new={self.new!r}"


def compat_diff(new: RunMeta, base: RunMeta, *, kind: RunKind, expected_prompt: tuple[str, str] | None = None,
                ) -> list[FieldDiff]:
    """`expected_prompt` = (version, sha256) the new run must carry; required for a verifier run."""
    a, b = base.model_dump(mode="json"), new.model_dump(mode="json")
    out: list[FieldDiff] = []
    for f in sorted(MUST_MATCH):
        if kind == "verifier" and f in PROMPT_FIELDS:
            if a[f] == b[f]:
                out.append(FieldDiff(f, a[f], b[f], "must differ"))
            continue
        if a[f] != b[f]:
            out.append(FieldDiff(f, a[f], b[f], "must match"))
    if kind == "verifier":
        if expected_prompt is None:
            raise ValueError("a verifier run is checked against its expected prompt (version, sha256)")
        for f, want in zip(("prompt_version", "prompt_sha256"), expected_prompt, strict=True):
            if b[f] != want:
                out.append(FieldDiff(f, a[f], b[f], f"must equal {want}"))
    if new.run_kind != kind:
        out.append(FieldDiff("run_kind", base.run_kind, new.run_kind, f"must equal {kind}"))
    return out


def check_compat(new: RunMeta, base: RunMeta, *, kind: RunKind,
                 expected_prompt: tuple[str, str] | None = None) -> None:
    diffs = compat_diff(new, base, kind=kind, expected_prompt=expected_prompt)
    if diffs:
        lines = "\n".join(str(d) for d in diffs)
        raise CompatError(f"{new.run_id} is not compatible with baseline {base.run_id}:\n{lines}")
