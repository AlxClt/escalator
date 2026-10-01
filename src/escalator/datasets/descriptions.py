"""The description overlay: BIRD's `database_description/*.csv`, with Arcwise's files on top.

`env/schema.py` imports `effective_descriptions`; nothing else may re-implement the rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from escalator.datasets.config import REPO_ROOT, arcwise_description_dir, description_dir, load_config

Layer = Literal["bird", "arcwise"]


class OverlayError(ValueError):
    pass


@dataclass(frozen=True)
class Effective:
    path: Path
    layer: Layer


def stem_key(path: Path) -> str:
    return path.stem.casefold()


def layer_files(directory: Path) -> dict[str, Path]:
    """CSV files of one layer keyed by case-folded stem; two files colliding on that key raise."""
    out: dict[str, Path] = {}
    if not directory.is_dir():
        return out
    for p in sorted(directory.glob("*.csv")):
        key = stem_key(p)
        if key in out:
            raise OverlayError(f"{directory}: {out[key].name} and {p.name} collide on case-folded stem {key!r}")
        out[key] = p
    return out


def overlay(bird: dict[str, Path], arcwise: dict[str, Path]) -> dict[str, Effective]:
    """Arcwise replaces a BIRD file with the same key and adds files BIRD lacks; BIRD-only files stay."""
    merged = {k: Effective(p, "bird") for k, p in bird.items()}
    merged.update({k: Effective(p, "arcwise") for k, p in arcwise.items()})
    return dict(sorted(merged.items()))


def effective_layers(db_id: str, root: Path | None = None) -> dict[str, Effective]:
    base = root if root is not None else load_config(root=REPO_ROOT).data_root
    return overlay(layer_files(base / description_dir(db_id)), layer_files(base / arcwise_description_dir(db_id)))


def effective_descriptions(db_id: str, root: Path | None = None) -> dict[str, Path]:
    """Effective description file per table, keyed by case-folded file stem."""
    return {k: e.path for k, e in effective_layers(db_id, root).items()}
