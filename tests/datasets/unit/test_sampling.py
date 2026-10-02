from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from escalator.datasets.config import DataConfig
from escalator.datasets.lock import current_env, write_env_lock
from escalator.datasets.manifest import (
    ManifestConfig,
    ManifestError,
    allocate,
    build_manifest,
    encode_value,
    load_manifest_config,
    rank_key,
    result_hash,
    select,
    task_hash,
)

DIFFS = ("simple", "moderate", "challenging")
PINNED = ManifestConfig(11, {"date_dependent": ("1", "2"), "gold_too_slow": ("2", "3"), "allowlisted": ("5",)})


# --- allocation and selection ----------------------------------------------


def test_allocation_is_largest_remainder_and_sums_exactly() -> None:
    # quotas 200 * (148, 248, 102) / 498 = 59.44, 99.60, 40.96 -> floors 59, 99, 40 (198); +1 to moderate, challenging
    assert allocate({"simple": 148, "moderate": 248, "challenging": 102}, 200) == {
        "simple": 59, "moderate": 100, "challenging": 41}
    for sizes in ({"simple": 1, "moderate": 1, "challenging": 1}, {"simple": 7, "moderate": 0, "challenging": 3}):
        assert sum(allocate(sizes, 2).values()) == 2


def test_allocation_remainder_ties_go_to_the_larger_stratum_then_fixed_order() -> None:
    # quotas 0.5 and 1.5: equal remainders, the larger stratum gets the extra unit
    assert allocate({"simple": 1, "moderate": 0, "challenging": 3}, 2) == {"simple": 0, "moderate": 0, "challenging": 2}
    # equal remainders and sizes: simple < moderate < challenging
    assert allocate({"simple": 1, "moderate": 2, "challenging": 1}, 2) == {"simple": 1, "moderate": 1, "challenging": 0}


def test_allocation_refuses_more_than_the_frame() -> None:
    with pytest.raises(ManifestError):
        allocate({"simple": 1, "moderate": 1, "challenging": 1}, 4)


def test_selection_takes_lowest_seeded_hashes() -> None:
    ids = [str(i) for i in range(50)]
    chosen = select({"simple": ids}, {"simple": 5}, seed=7)["simple"]
    expected = sorted(ids, key=lambda q: hashlib.sha256(f"7:{q}".encode()).hexdigest())[:5]
    assert chosen == expected and rank_key(7, "3") == hashlib.sha256(b"7:3").hexdigest()
    assert select({"simple": ids}, {"simple": 5}, seed=8)["simple"] != chosen


# --- hashing ---------------------------------------------------------------


def test_values_are_typed_and_floats_exact() -> None:
    assert encode_value(None) == ["null", None]
    assert encode_value(1) == ["int", 1]
    assert encode_value(0.1) == ["float", (0.1).hex()]
    assert encode_value("1") == ["str", "1"]
    assert encode_value(b"\x01") == ["bytes", "01"]


def test_result_hash_is_a_multiset_hash() -> None:
    rows: list[tuple[object, ...]] = [(1, "a"), (2, None), (1, "a")]
    assert result_hash(rows) == result_hash(list(reversed(rows)))
    assert result_hash(rows) != result_hash(rows[:2])            # multiplicity counts
    assert result_hash([(1,)]) != result_hash([(1.0,)])           # int and float differ
    assert result_hash([(1,)]) != result_hash([("1",)])
    assert result_hash([]) != result_hash([(None,)])


def test_task_hash_is_canonical_and_keeps_null_evidence() -> None:
    rec: dict[str, object] = {"question_id": "12", "db_id": "d", "question": "q", "evidence": None, "SQL": "SELECT 1", "extra": 1}
    reordered: dict[str, object] = {k: rec[k] for k in reversed(list(rec))}
    canonical = json.dumps({"SQL": "SELECT 1", "db_id": "d", "evidence": None, "question": "q", "question_id": "12"},
                           separators=(",", ":")).encode()
    assert task_hash(rec) == task_hash(reordered) == hashlib.sha256(canonical).hexdigest()
    with_empty: dict[str, object] = rec | {"evidence": ""}
    assert task_hash(rec) != task_hash(with_empty)


# --- build on a synthetic data root ----------------------------------------


def _setup(tmp_path: Path, n: int = 30) -> DataConfig:
    data = tmp_path / "data"
    raw = data / "raw"
    db = raw / "bird" / "superhero" / "superhero.sqlite"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER, y REAL, z TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i / 3, f"v{i}") for i in range(n)])
    conn.commit()
    conn.close()
    tasks = [{"question_id": str(i), "db_id": "superhero", "question": f"q{i}", "evidence": None,
              "SQL": f"SELECT * FROM t WHERE x >= {i}"} for i in range(n)]
    (raw / "arcwise").mkdir()
    (raw / "arcwise" / "arcwise_plat_full_with_diff.json").write_text(json.dumps(tasks), encoding="utf-8")
    labels = [t | {"difficulty": DIFFS[i % 3]} for i, t in enumerate(tasks)]
    (raw / "bird" / "mini_dev_sqlite.json").write_text(json.dumps(labels), encoding="utf-8")
    ref = "a" * 40
    lock = {"lock_version": 2, "created_on": "2026-01-01", "sources": [{
        "name": "arcwise_plat_full", "url": f"https://raw.githubusercontent.com/o/r/{ref}/x.json", "ref": ref,
        "path": "arcwise/arcwise_plat_full_with_diff.json", "sha256": "0" * 64, "bytes": 1,
        "license": "x", "retrieved_on": "2026-01-01"}]}
    (data / "sources.lock").write_text(json.dumps(lock), encoding="utf-8")
    write_env_lock(current_env(), data / "env.lock")
    audit = data / "audit"
    audit.mkdir()
    lock_sha = hashlib.sha256((data / "sources.lock").read_bytes()).hexdigest()
    (audit / "environment.json").write_text(json.dumps({"lock_sha256": lock_sha}), encoding="utf-8")
    (audit / "date_dependent.json").write_text(json.dumps({"ids": ["1", "2"]}), encoding="utf-8")
    # timing that disagrees with the pinned gold_too_slow list: it must not affect the frame
    (audit / "gold_runtime.json").write_text(
        json.dumps({"sandbox_timeout_s": 5.0, "wall_ms": {"3": 10.0, "4": 9000.0}}), encoding="utf-8")
    (data / "gold_allowlist.json").write_text(
        json.dumps({"entries": [{"question_id": "5", "kind": "empty", "reason": "r"}]}), encoding="utf-8")
    return DataConfig(raw, data / "sources.lock", data / "env.lock", 60.0, 5.0)


def test_build_is_byte_identical_and_uses_only_pinned_exclusions(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    first = build_manifest(cfg, PINNED, sample_size=12)
    assert first == build_manifest(cfg, PINNED, sample_size=12)
    m = json.loads(first)
    assert m["exclusions"] == [
        {"question_id": "1", "reasons": ["date_dependent"]},
        {"question_id": "2", "reasons": ["date_dependent", "gold_too_slow"]},
        {"question_id": "3", "reasons": ["gold_too_slow"]},
        {"question_id": "5", "reasons": ["allowlisted"]},
    ]
    assert m["frame_size"] == 26 and len(m["tasks"]) == 12
    sampled = [t["question_id"] for t in m["tasks"]]
    assert not {"1", "2", "3", "5"} & set(sampled) and sampled == sorted(sampled, key=int)
    assert m["frame_size"] == 26  # id 4 is slow in A2 but not pinned, so it stays in the frame
    for s in DIFFS:
        assert sum(t["difficulty"] == s for t in m["tasks"]) == m["strata"][s]["allocation"]
    assert all(t["gold_row_count"] == 30 - int(t["question_id"]) for t in m["tasks"])
    assert "q0" not in first.decode() and "SELECT" not in first.decode()


def test_build_refuses_when_audits_describe_another_lock(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    cfg.lock.write_text(cfg.lock.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ManifestError, match="A5"):
        build_manifest(cfg, PINNED, sample_size=12)


def test_build_refuses_when_a3_or_allowlist_drift_from_the_pinned_lists(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    (cfg.audit_dir / "date_dependent.json").write_text(json.dumps({"ids": ["1", "2", "7"]}), encoding="utf-8")
    with pytest.raises(ManifestError, match="date_dependent"):
        build_manifest(cfg, PINNED, sample_size=12)
    (cfg.audit_dir / "date_dependent.json").write_text(json.dumps({"ids": ["1", "2"]}), encoding="utf-8")
    cfg.allowlist.write_text(json.dumps({"entries": []}), encoding="utf-8")
    with pytest.raises(ManifestError, match="allowlisted"):
        build_manifest(cfg, PINNED, sample_size=12)


def test_manifest_config_requires_every_reason(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("seed: 1\nexclusions:\n  date_dependent: [7, '3']\n  gold_too_slow: []\n  allowlisted: []\n",
                    encoding="utf-8")
    assert load_manifest_config(path).exclusions["date_dependent"] == ("3", "7")
    path.write_text("seed: 1\nexclusions:\n  date_dependent: []\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest_config(path)
