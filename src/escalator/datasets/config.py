"""Load `configs/data.yaml` and the fixed layout of `data/raw`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "data.yaml"

EXPECTED_DB_IDS = (
    "california_schools",
    "card_games",
    "codebase_community",
    "debit_card_specializing",
    "european_football_2",
    "financial",
    "formula_1",
    "student_club",
    "superhero",
    "thrombosis_prediction",
    "toxicology",
)

# Paths under data_root. Bootstrap writes them, verification reads them.
MINIDEV_JSON = "bird/mini_dev_sqlite.json"
PLAT_FULL_JSON = "arcwise/arcwise_plat_full_with_diff.json"
ARCWISE_SCHEMAS_DIR = "arcwise/schemas"
GOLD_CACHE = "_cache/gold_check.json"


def db_path(db_id: str) -> str:
    return f"bird/{db_id}/{db_id}.sqlite"


def description_dir(db_id: str) -> str:
    return f"bird/{db_id}/database_description"


def arcwise_description_dir(db_id: str) -> str:
    return f"{ARCWISE_SCHEMAS_DIR}/{db_id}/database_description"


@dataclass(frozen=True)
class DataConfig:
    data_root: Path
    lock: Path
    env_lock: Path
    gold_timeout_s: float
    sandbox_timeout_s: float

    @property
    def data_dir(self) -> Path:
        """`data/`: holds the locks, NOTICE, allowlist and audits."""
        return self.lock.parent

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    @property
    def allowlist(self) -> Path:
        return self.data_dir / "gold_allowlist.json"

    @property
    def gold_cache(self) -> Path:
        return self.data_root / GOLD_CACHE


def _number(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"configs/data.yaml: {key} must be a positive number")
    return float(value)


def load_config(path: Path = DEFAULT_CONFIG, root: Path = REPO_ROOT) -> DataConfig:
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a mapping")
    raw: dict[str, object] = {str(k): v for k, v in loaded.items()}
    paths = {k: raw.get(k) for k in ("data_root", "lock", "env_lock")}
    if not all(isinstance(v, str) for v in paths.values()):
        raise ValueError(f"{path}: data_root, lock and env_lock must be strings")
    return DataConfig(
        data_root=root / str(paths["data_root"]),
        lock=root / str(paths["lock"]),
        env_lock=root / str(paths["env_lock"]),
        gold_timeout_s=_number(raw, "gold_timeout_s"),
        sandbox_timeout_s=_number(raw, "sandbox_timeout_s"),
    )
