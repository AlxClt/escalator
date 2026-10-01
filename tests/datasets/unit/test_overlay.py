from __future__ import annotations

from pathlib import Path

import pytest

from escalator.datasets.descriptions import OverlayError, effective_descriptions, effective_layers


def _write(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original_column_name,column_name,column_description,data_format,value_description\n",
                    encoding="utf-8")
    return path


BIRD = "bird/db/database_description"
ARC = "arcwise/schemas/db/database_description"


# U14
def test_u14_overlay_replaces_adds_and_keeps(tmp_path: Path) -> None:
    _write(tmp_path, f"{BIRD}/Budget.csv")
    kept = _write(tmp_path, f"{BIRD}/member.csv")
    replacement = _write(tmp_path, f"{ARC}/budget.csv")
    added = _write(tmp_path, f"{ARC}/event.csv")
    assert effective_descriptions("db", tmp_path) == {"budget": replacement, "event": added, "member": kept}
    layers = effective_layers("db", tmp_path)
    assert {k: e.layer for k, e in layers.items()} == {"budget": "arcwise", "event": "arcwise", "member": "bird"}


@pytest.mark.parametrize("layer", [BIRD, ARC])
def test_u14_collision_within_a_layer_raises(tmp_path: Path, layer: str) -> None:
    # "Straße" and "strasse" are distinct files even on case-insensitive filesystems,
    # but share the case-folded stem "strasse".
    _write(tmp_path, f"{layer}/Straße.csv")
    _write(tmp_path, f"{layer}/strasse.csv")
    assert len(list((tmp_path / layer).glob("*.csv"))) == 2
    with pytest.raises(OverlayError):
        effective_descriptions("db", tmp_path)
