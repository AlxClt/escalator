"""`make manifest`: exclusions, stratified sampling and pinning of the task subset in data/manifest.json.

The manifest holds ids, labels and hashes only, never question, evidence or SQL text.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import yaml

from escalator.datasets.config import REPO_ROOT, DataConfig
from escalator.datasets.gold import GoldError, GoldTimeout, execute_with_deadline
from escalator.datasets.lock import current_env, load_env_lock, parse_lock
from escalator.datasets.verify import DataContext, Record, normalize_id, sort_ids

DEFAULT_MANIFEST_CONFIG = REPO_ROOT / "configs" / "manifest.yaml"
SAMPLE_SIZE = 200
STRATA = ("simple", "moderate", "challenging")
REASONS = ("date_dependent", "gold_too_slow", "allowlisted")
TASK_FIELDS = ("question_id", "db_id", "question", "evidence", "SQL")
METHOD = (
    "frame = Plat-Full ids minus the exclusions pinned in configs/manifest.yaml "
    "(date_dependent, gold_too_slow, allowlisted); "
    "strata = Mini-Dev difficulty; allocation proportional to frame stratum sizes, largest-remainder "
    "rounding to the sample size (remainder ties: larger stratum, then simple < moderate < challenging); "
    "selection = per stratum, ids ranked by sha256(f'{seed}:{question_id}') hex ascending, first n_h"
)


class ManifestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManifestConfig:
    seed: int
    exclusions: dict[str, tuple[str, ...]]  # reason -> pinned ids


def load_manifest_config(path: Path = DEFAULT_MANIFEST_CONFIG) -> ManifestConfig:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: expected a mapping")
    seed, pinned = raw.get("seed"), raw.get("exclusions")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ManifestError(f"{path}: seed must be an integer")
    if not isinstance(pinned, dict) or sorted(str(k) for k in pinned) != sorted(REASONS):
        raise ManifestError(f"{path}: exclusions must list exactly {list(REASONS)}")
    exclusions: dict[str, tuple[str, ...]] = {}
    for reason in REASONS:
        ids = pinned[reason]
        if not isinstance(ids, list):
            raise ManifestError(f"{path}: exclusions.{reason} must be a list of ids")
        exclusions[reason] = tuple(sort_ids(normalize_id(i) for i in ids))
    return ManifestConfig(seed, exclusions)


# --- hashing ---------------------------------------------------------------


def canonical_json(obj: object) -> bytes:
    """Sorted keys, UTF-8, no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_hash(rec: Record) -> str:
    """sha256 of the canonical JSON of the task's inputs and gold; null evidence stays null."""
    obj = {k: rec.get(k) for k in TASK_FIELDS}
    obj["question_id"] = normalize_id(rec.get("question_id"))
    return sha256_hex(canonical_json(obj))


def encode_value(value: object) -> list[object]:
    """A SQLite value as [type, value]; floats as float.hex() so no precision is lost."""
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        raise ManifestError("SQLite does not return booleans")
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    raise ManifestError(f"unexpected SQLite value type {type(value).__name__}")


def result_hash(rows: list[tuple[object, ...]]) -> str:
    """sha256 of the result as a multiset of rows: rows encoded, then sorted by their canonical JSON."""
    encoded = sorted(([encode_value(v) for v in row] for row in rows), key=canonical_json)
    return sha256_hex(canonical_json(encoded))


# --- frame and sampling ----------------------------------------------------


def allocate(frame_sizes: Mapping[str, int], n: int) -> dict[str, int]:
    """Proportional allocation with largest-remainder rounding, summing to exactly n."""
    total = sum(frame_sizes.values())
    if n > total:
        raise ManifestError(f"cannot sample {n} from a frame of {total}")
    quotas = {s: Fraction(n * size, total) for s, size in frame_sizes.items()}
    alloc = {s: int(q) for s, q in quotas.items()}  # floor: quotas are non-negative
    order = sorted(frame_sizes, key=lambda s: (-(quotas[s] - alloc[s]), -frame_sizes[s], STRATA.index(s)))
    for s in order[: n - sum(alloc.values())]:
        alloc[s] += 1
    return {s: alloc[s] for s in STRATA if s in alloc}


def rank_key(seed: int, question_id: str) -> str:
    return sha256_hex(f"{seed}:{question_id}".encode("utf-8"))


def select(frame: Mapping[str, list[str]], alloc: Mapping[str, int], seed: int) -> dict[str, list[str]]:
    return {s: sorted(ids, key=lambda q: rank_key(seed, q))[: alloc[s]] for s, ids in frame.items()}


def _read_json(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: expected an object")
    return {str(k): v for k, v in raw.items()}


def _id_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ManifestError(f"{where}: expected a list of ids")
    return [normalize_id(v) for v in value]


def exclusions(mcfg: ManifestConfig) -> dict[str, list[str]]:
    """Every pinned excluded id with all of its reasons."""
    reasons: dict[str, list[str]] = {}
    for reason in REASONS:
        for qid in mcfg.exclusions[reason]:
            reasons.setdefault(qid, []).append(reason)
    return {q: reasons[q] for q in sort_ids(reasons)}


def check_pinned_exclusions(cfg: DataConfig, mcfg: ManifestConfig) -> None:
    """The deterministic sources must still agree with the pinned lists: A3 (date_dependent) and the
    gold allowlist (allowlisted). gold_too_slow is timing-based and deliberately not re-derived."""
    a3 = sort_ids(_id_list(_read_json(cfg.audit_dir / "date_dependent.json").get("ids"), "A3 ids"))
    entries = _read_json(cfg.allowlist).get("entries")
    if not isinstance(entries, list):
        raise ManifestError("gold_allowlist.json: entries missing")
    allow = sort_ids(normalize_id(e.get("question_id")) for e in entries if isinstance(e, dict))
    for reason, current in (("date_dependent", a3), ("allowlisted", allow)):
        if current != list(mcfg.exclusions[reason]):
            raise ManifestError(f"pinned exclusions.{reason} no longer matches its source: {current}; "
                                "review and update configs/manifest.yaml by hand")


def check_inputs(cfg: DataConfig) -> None:
    """Refuse unless the audits were written for this sources.lock and the interpreter matches env.lock."""
    lock_sha = sha256_hex(cfg.lock.read_bytes())
    if _read_json(cfg.audit_dir / "environment.json").get("lock_sha256") != lock_sha:
        raise ManifestError("environment.json (A5) records another sources.lock: run make data-verify first")
    if current_env() != load_env_lock(cfg.env_lock):
        raise ManifestError(f"interpreter {current_env()} does not match env.lock")


# --- build -----------------------------------------------------------------


def _gold_rows(ctx: DataContext, rec: Record) -> list[tuple[object, ...]]:
    try:
        return execute_with_deadline(ctx.connection(str(rec.get("db_id"))), str(rec.get("SQL")), ctx.cfg.gold_timeout_s)
    except (GoldTimeout, GoldError) as exc:
        raise ManifestError(f"gold for {normalize_id(rec.get('question_id'))} failed: {type(exc).__name__}") from exc


def build_manifest(cfg: DataConfig, mcfg: ManifestConfig, sample_size: int = SAMPLE_SIZE) -> bytes:
    check_inputs(cfg)
    check_pinned_exclusions(cfg, mcfg)
    excluded = exclusions(mcfg)
    ctx = DataContext(cfg)
    try:
        minidev = ctx.minidev_by_id
        frame: dict[str, list[str]] = {s: [] for s in STRATA}
        for qid in sort_ids(ctx.platfull_by_id):
            if qid not in excluded:
                frame[str(minidev[qid].get("difficulty"))].append(qid)
        alloc = allocate({s: len(ids) for s, ids in frame.items()}, sample_size)
        chosen = select(frame, alloc, mcfg.seed)
        tasks: list[dict[str, object]] = []
        for qid in sort_ids(q for ids in chosen.values() for q in ids):
            rec = ctx.platfull_by_id[qid]
            rows = _gold_rows(ctx, rec)
            tasks.append({
                "question_id": qid,
                "db_id": rec.get("db_id"),
                "difficulty": minidev[qid].get("difficulty"),
                "task_hash": task_hash(rec),
                "gold_result_hash": result_hash(rows),
                "gold_row_count": len(rows),
            })
    finally:
        ctx.close()
    ref = parse_lock(json.loads(cfg.lock.read_text(encoding="utf-8"))).source("arcwise_plat_full").ref
    manifest = {
        "seed": mcfg.seed,
        "method": METHOD,
        "sample_size": sample_size,
        "sources_lock_sha256": sha256_hex(cfg.lock.read_bytes()),
        "env_lock_sha256": sha256_hex(cfg.env_lock.read_bytes()),
        "arcwise_ref": ref,
        "frame_size": sum(len(ids) for ids in frame.values()),
        "strata": {s: {"frame": len(frame[s]), "allocation": alloc[s]} for s in STRATA},
        "exclusions": [{"question_id": q, "reasons": r} for q, r in excluded.items()],
        "tasks": tasks,
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_manifest(cfg: DataConfig, mcfg: ManifestConfig, path: Path) -> bytes:
    data = build_manifest(cfg, mcfg)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data
