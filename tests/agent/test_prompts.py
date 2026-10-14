"""Prompt loader and renderer (spec test 12)."""

from __future__ import annotations

import hashlib

import pytest

from escalator.agent.prompts import PROMPTS_DIR, SLOTS, PromptError, load_prompt, parse_prompt, render

TOOLS = """
tool_descriptions:
  get_schema: {description: d, parameters: {db_id: p, tables: p}}
"""


def prompt_yaml(system: str = "S {{db_id}} {{schema_card}}", user: str = "U {{question}}{{evidence_section}}",
                evidence: str = "E {{evidence}}") -> bytes:
    return f"system: {system!r}\nuser: {user!r}\nevidence_section: {evidence!r}\n{TOOLS}".encode()


def test_v1_loads_and_hashes_the_raw_file() -> None:
    p = load_prompt("v1")
    assert p.version == "v1"
    assert p.sha256 == hashlib.sha256((PROMPTS_DIR / "v1.yaml").read_bytes()).hexdigest()
    assert set(p.tools) == {"execute_sql", "get_schema", "sample_rows", "submit_answer"}


@pytest.mark.parametrize("bad", [
    prompt_yaml(system="S {{db_id}}"),  # missing schema_card
    prompt_yaml(user="U {{question}}{{evidence_section}} {{oops}}"),  # unknown slot
    prompt_yaml(evidence="E"),  # missing evidence
])
def test_template_slots_checked_at_load(bad: bytes) -> None:
    with pytest.raises(PromptError):
        parse_prompt(bad, "t")


def test_render_rejects_missing_and_unknown_values() -> None:
    with pytest.raises(PromptError, match="unfilled"):
        render("A {{db_id}} {{schema_card}}", {"db_id": "x"}, SLOTS["system"])
    with pytest.raises(PromptError, match="unknown"):
        render("A {{db_id}} {{schema_card}}", {"db_id": "x", "schema_card": "c", "extra": "e"}, SLOTS["system"])


def test_null_evidence_omits_the_section() -> None:
    p = load_prompt("v1")
    text = p.user_text(question="How many?", evidence=None)
    assert "None" not in text and "External knowledge" not in text
    assert text == "Question: How many?"
    assert p.user_text(question="How many?", evidence="a = b").endswith("External knowledge: a = b")


def test_braces_in_values_survive() -> None:
    card = "## t\nc TEXT — Values: {0, 1} or {{schema_card}} or {db_id}"
    sql_ish = "SELECT '{{question}}' FROM t"
    p = load_prompt("v1")
    system = p.system_text(db_id="db", schema_card=card)
    assert card in system
    assert p.user_text(question=sql_ish, evidence="{{evidence}}") == f"Question: {sql_ish}\n\nExternal knowledge: {{{{evidence}}}}"
