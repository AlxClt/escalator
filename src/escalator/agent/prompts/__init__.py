"""Versioned prompts: `agent/prompts/<version>.yaml`, one file per version, shared by every tier.

A version file holds every piece of model-facing natural-language text: the `system`, `user` and
`evidence_section` templates and the tool descriptions. Templates use `{{slot}}` placeholders,
filled by plain substitution (no Jinja, no str.format: schema cards and SQL contain braces).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

PROMPTS_DIR = Path(__file__).parent

# The slots each template must contain, exactly.
SLOTS: dict[str, frozenset[str]] = {
    "system": frozenset({"db_id", "schema_card"}),
    "user": frozenset({"question", "evidence_section"}),
    "evidence_section": frozenset({"evidence"}),
}
_SLOT = re.compile(r"\{\{(.*?)\}\}")


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class ToolText:
    description: str
    parameters: dict[str, str]  # parameter name -> description


@dataclass(frozen=True)
class Prompt:
    version: str
    sha256: str  # of the raw file bytes
    system: str
    user: str
    evidence_section: str
    tools: dict[str, ToolText]

    def system_text(self, *, db_id: str, schema_card: str) -> str:
        return render(self.system, {"db_id": db_id, "schema_card": schema_card}, SLOTS["system"])

    def user_text(self, *, question: str, evidence: str | None) -> str:
        """The evidence section is rendered only when evidence is not None; otherwise it is omitted."""
        section = "" if evidence is None else render(
            self.evidence_section, {"evidence": evidence}, SLOTS["evidence_section"]
        )
        return render(self.user, {"question": question, "evidence_section": section}, SLOTS["user"])


def slots(template: str) -> set[str]:
    return set(_SLOT.findall(template))


def check_template(name: str, template: str, declared: frozenset[str]) -> None:
    found = slots(template)
    unknown, missing = sorted(found - declared), sorted(declared - found)
    if unknown:
        raise PromptError(f"{name}: unknown slot(s) {unknown}")
    if missing:
        raise PromptError(f"{name}: missing slot(s) {missing}")


def render(template: str, values: Mapping[str, str], declared: frozenset[str]) -> str:
    """One pass over the template only: braces inside the values are never substituted."""
    unfilled, unknown = sorted(declared - set(values)), sorted(set(values) - declared)
    if unfilled:
        raise PromptError(f"slot(s) left unfilled: {unfilled}")
    if unknown:
        raise PromptError(f"unknown slot(s): {unknown}")

    def fill(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in declared:
            raise PromptError(f"unknown slot {name!r} in template")
        return values[name]

    return _SLOT.sub(fill, template)


def _text(raw: dict[str, object], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise PromptError(f"{where}: {key} must be a string")
    return value


def _tools(value: object, where: str) -> dict[str, ToolText]:
    if not isinstance(value, dict):
        raise PromptError(f"{where}: tool_descriptions must map tool names to {{description, parameters}}")
    out: dict[str, ToolText] = {}
    for name, entry in value.items():
        if not isinstance(entry, dict) or set(entry) != {"description", "parameters"}:
            raise PromptError(f"{where}: tool_descriptions.{name} must be {{description, parameters}}")
        desc, params = entry["description"], entry["parameters"]
        if not isinstance(desc, str) or not isinstance(params, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in params.items()
        ):
            raise PromptError(f"{where}: tool_descriptions.{name} needs a string description and string parameters")
        out[str(name)] = ToolText(desc, dict(params))
    return out


def parse_prompt(data: bytes, version: str, where: str = "prompt") -> Prompt:
    raw: object = yaml.safe_load(data)
    if not isinstance(raw, dict):
        raise PromptError(f"{where}: expected a mapping")
    expected = {"system", "user", "evidence_section", "tool_descriptions"}
    if set(raw) != expected:
        raise PromptError(f"{where}: keys must be {sorted(expected)}, got {sorted(map(str, raw))}")
    texts = {k: _text(raw, k, where) for k in SLOTS}
    for name, template in texts.items():
        check_template(f"{where}: {name}", template, SLOTS[name])
    return Prompt(version, hashlib.sha256(data).hexdigest(), texts["system"], texts["user"],
                  texts["evidence_section"], _tools(raw["tool_descriptions"], where))


def load_prompt(version: str, root: Path = PROMPTS_DIR) -> Prompt:
    path = root / f"{version}.yaml"
    return parse_prompt(path.read_bytes(), version, where=str(path))
