"""Offline verification gates and audits over `data/raw`.

Every gate returns a `GateResult` holding ids, counts and file or column names only, never
question, evidence, SQL or description text. The integration tests assert on the same functions.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import platform
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path

from escalator.datasets import descriptions, fetch
from escalator.datasets.config import (
    ARCWISE_SCHEMAS_DIR,
    EXPECTED_DB_IDS,
    MINIDEV_JSON,
    PLAT_FULL_JSON,
    DataConfig,
    arcwise_description_dir,
    db_path,
    description_dir,
)
from escalator.datasets.gold import GoldCheck, GoldOutcome, cache_key, check_gold, open_readonly, run_gold
from escalator.datasets.lock import EnvLock, Lock, current_env, iter_files, load_env_lock, load_lock, parse_lock, validate_lock

Record = dict[str, object]

MIN_SQLITE = (3, 32, 0)
MINIDEV_COUNT = 500
PLAT_FULL_COUNT = 498
MINIDEV_KEYS = frozenset({"question_id", "db_id", "question", "evidence", "SQL", "difficulty"})
PLAT_KEYS = frozenset({"question_id", "question", "evidence", "SQL", "db_id"})
PLAT_OPTIONAL = frozenset({"original_question", "original_evidence", "original_SQL", "difficulty"})
DIFFICULTIES = frozenset({"simple", "moderate", "challenging"})
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
DATE_DEPENDENT_RE = re.compile(r"(?i)\bnow\b|CURRENT_(DATE|TIME|TIMESTAMP)")
ID_RE = re.compile(r"0|[1-9][0-9]*")
ALLOWLIST_KINDS = frozenset({"error", "timeout", "empty", "nonterminating"})
# The gold outcome status each allowlist kind excuses.
ALLOWLIST_STATUS = {"error": "error", "timeout": "timeout", "empty": "empty", "nonterminating": "timeout"}


# --- normalization ---------------------------------------------------------


def normalize_id(value: object) -> str:
    """`str(int(x))` for plain digit strings or ints; anything else raises."""
    if isinstance(value, bool):
        raise ValueError(f"invalid id {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"invalid id {value!r}")
        return str(value)
    if isinstance(value, str) and ID_RE.fullmatch(value):
        return str(int(value))
    raise ValueError(f"invalid id {value!r}")


def sort_ids(ids: Iterable[str]) -> list[str]:
    return sorted(set(ids), key=lambda x: (0, int(x), "") if x.isdigit() else (1, 0, x))


# --- results ---------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    gate: str
    ok: bool
    message: str
    ids: tuple[str, ...] = ()
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        shown = ", ".join(self.ids[:30]) + (" ..." if len(self.ids) > 30 else "")
        return f"{self.gate}: {self.message}" + (f" [ids: {shown}]" if self.ids else "")


def _result(gate: str, bad: Iterable[str], ok_msg: str, bad_msg: str, **details: object) -> GateResult:
    ids = tuple(sort_ids(bad))
    return GateResult(gate, not ids, ok_msg if not ids else f"{bad_msg} ({len(ids)})", ids, dict(details))


def load_allowlist(path: Path) -> tuple[frozenset[tuple[str, str]], list[str]]:
    """Returns the allowed (question_id, gold status) pairs and validation errors."""
    if not path.is_file():
        return frozenset(), [f"{path} is missing"]
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return frozenset(), [f"{path}: expected {{'entries': [...]}}"]
    errors: list[str] = []
    keys: set[tuple[str, str]] = set()
    for i, item in enumerate(entries):
        if not isinstance(item, dict):
            errors.append(f"entries[{i}]: not an object")
            continue
        qid, kind, reason = item.get("question_id"), item.get("kind"), item.get("reason")
        try:
            nid = normalize_id(qid)
        except ValueError:
            errors.append(f"entries[{i}]: invalid question_id")
            continue
        if not isinstance(kind, str) or kind not in ALLOWLIST_KINDS:
            errors.append(f"entries[{i}]: kind must be one of {sorted(ALLOWLIST_KINDS)}")
        elif not isinstance(reason, str) or not reason.strip():
            errors.append(f"entries[{i}]: a written reason is required")
        else:
            keys.add((nid, ALLOWLIST_STATUS[kind]))
    return frozenset(keys), errors


# --- context ---------------------------------------------------------------


def _load_list(path: Path) -> list[Record]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path.name}: expected a JSON list")
    out: list[Record] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path.name}: every record must be an object")
        out.append({str(k): v for k, v in item.items()})
    return out


def _rid(rec: Record) -> str:
    return normalize_id(rec.get("question_id"))


def _safe_rid(rec: Record, index: int) -> str:
    try:
        return _rid(rec)
    except ValueError:
        return f"#{index}"


def _by_id(records: list[Record]) -> dict[str, Record]:
    """First record per normalized id (Mini-Dev duplicates are checked to be exact copies by M2)."""
    out: dict[str, Record] = {}
    for rec in records:
        out.setdefault(_rid(rec), rec)
    return out


class DataContext:
    """Lazily loaded view of the pinned data. Missing files raise; they never skip."""

    def __init__(self, cfg: DataConfig, use_cache: bool = True) -> None:
        self.cfg = cfg
        self.root = cfg.data_root
        self.use_cache = use_cache
        self.evaluated: dict[str, bool] = {}
        self._conns: dict[str, sqlite3.Connection] = {}
        self._gold: GoldCheck | None = None

    def record(self, result: GateResult) -> GateResult:
        self.evaluated[result.gate] = result.ok
        return result

    @cached_property
    def lock(self) -> Lock:
        if not self.cfg.lock.is_file():
            raise FileNotFoundError(f"{self.cfg.lock} is missing: run `make data-lock`")
        return load_lock(self.cfg.lock)

    @cached_property
    def env_lock(self) -> EnvLock:
        if not self.cfg.env_lock.is_file():
            raise FileNotFoundError(f"{self.cfg.env_lock} is missing: run `make env-lock`")
        return load_env_lock(self.cfg.env_lock)

    def _require(self, rel: str) -> Path:
        path = self.root / rel
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing: run `make data`")
        return path

    @cached_property
    def minidev(self) -> list[Record]:
        return _load_list(self._require(MINIDEV_JSON))

    @cached_property
    def platfull(self) -> list[Record]:
        return _load_list(self._require(PLAT_FULL_JSON))

    @cached_property
    def minidev_by_id(self) -> dict[str, Record]:
        return _by_id(self.minidev)

    @cached_property
    def platfull_by_id(self) -> dict[str, Record]:
        return _by_id(self.platfull)

    def connection(self, db_id: str) -> sqlite3.Connection:
        if db_id not in self._conns:
            self._conns[db_id] = open_readonly(self._require(db_path(db_id)))
        return self._conns[db_id]

    def tables(self, db_id: str) -> list[str]:
        rows = self.connection(db_id).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
        return sorted(str(r[0]) for r in rows)

    def inputs_verified(self) -> bool:
        """V2 and V5 have passed in this session (evaluated now if they have not run yet)."""
        for gate in (gate_v2, gate_v5):
            name = gate.__name__.removeprefix("gate_").upper()
            if name not in self.evaluated:
                self.record(gate(self))
        return self.evaluated.get("V2", False) and self.evaluated.get("V5", False)

    def _execute(self, qid: str) -> GoldOutcome:
        rec = self.platfull_by_id[qid]
        db_id, sql = rec.get("db_id"), rec.get("SQL")
        if not isinstance(db_id, str) or db_id not in EXPECTED_DB_IDS or not isinstance(sql, str):
            return GoldOutcome("error", 0, 0.0, "invalid record")
        return run_gold(self.connection(db_id), sql, self.cfg.gold_timeout_s)

    def gold(self) -> GoldCheck:
        """T5's gold outcomes: from the cache when the key matches and V2 and V5 passed, else executed."""
        if self._gold is None:
            key = cache_key(self.cfg.lock, self.cfg.env_lock, self.cfg.allowlist, self.cfg.gold_timeout_s)
            use = self.use_cache and self.inputs_verified()
            self._gold = check_gold(sort_ids(self.platfull_by_id), self._execute, self.cfg.gold_cache, key, use)
        return self._gold

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()


# --- V: versions -----------------------------------------------------------


def gate_v1(ctx: DataContext) -> GateResult:
    try:
        lock = parse_lock(json.loads(ctx.cfg.lock.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return GateResult("V1", False, f"lock unreadable: {exc}")
    errors = validate_lock(lock)
    return GateResult("V1", not errors, "lock is valid" if not errors else "; ".join(errors))


def gate_v2(ctx: DataContext) -> GateResult:
    bad = [f.path for f in iter_files(ctx.lock) if not fetch.file_matches(ctx.root / f.path, f.sha256, f.bytes)]
    return _result("V2", bad, "every locked file matches size and sha256", "missing or mismatched files")


def _files_under(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def gate_v3(ctx: DataContext) -> GateResult:
    locked = {f.path for f in iter_files(ctx.lock)}
    present = [p for p in _files_under(ctx.root) if p.startswith(("bird/", "arcwise/"))]
    return _result("V3", [p for p in present if p not in locked], "no unlocked files under bird/ or arcwise/",
                   "unlocked files present")


def snapshot(ctx: DataContext) -> dict[str, str]:
    return {f.path: fetch.sha256_file(ctx.root / f.path).sha256 for f in iter_files(ctx.lock)
            if (ctx.root / f.path).is_file()}


def gate_v4(ctx: DataContext, before: dict[str, str]) -> GateResult:
    after = snapshot(ctx)
    changed = [p for p in set(before) | set(after) if before.get(p) != after.get(p)]
    sidecars = [p for p in _files_under(ctx.root) if p.endswith(SIDECAR_SUFFIXES)]
    return _result("V4", changed + sidecars, "no locked file changed, no journal/wal/shm files",
                   "files changed during verification or sidecars present")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(".")[:3])


def gate_v5(ctx: DataContext) -> GateResult:
    """Hard gate: the running interpreter is exactly the one pinned in env.lock."""
    pinned, current = ctx.env_lock, current_env()
    problems: list[str] = []
    if current.python_version != pinned.python_version:
        problems.append(f"python {current.python_version} != env.lock {pinned.python_version}")
    if current.sqlite_version != pinned.sqlite_version:
        problems.append(f"sqlite {current.sqlite_version} != env.lock {pinned.sqlite_version}")
    if _version(current.sqlite_version) < MIN_SQLITE:
        problems.append(f"sqlite {current.sqlite_version} < 3.32.0")
    msg = "; ".join(problems) or f"python {current.python_version}, sqlite {current.sqlite_version} match env.lock"
    return GateResult("V5", not problems, msg)


# --- D: databases ----------------------------------------------------------


def gate_d1(ctx: DataContext) -> GateResult:
    bird = ctx.root / "bird"
    dirs = {p.name for p in bird.iterdir() if p.is_dir()} if bird.is_dir() else set()
    bad = list(dirs ^ set(EXPECTED_DB_IDS))
    for db_id in EXPECTED_DB_IDS:
        desc = ctx.root / description_dir(db_id)
        if not (ctx.root / db_path(db_id)).is_file() or not desc.is_dir() or not any(desc.glob("*.csv")):
            bad.append(db_id)
    return _result("D1", bad, "exactly the 11 expected databases, each complete", "unexpected or incomplete db dirs")


def gate_d2(ctx: DataContext) -> GateResult:
    bad: list[str] = []
    for db_id in EXPECTED_DB_IDS:
        try:
            rows = ctx.connection(db_id).execute("PRAGMA quick_check").fetchall()
        except (sqlite3.Error, FileNotFoundError):
            bad.append(db_id)
            continue
        if rows != [("ok",)]:
            bad.append(db_id)
    return _result("D2", bad, "every database opens read-only and passes quick_check", "databases failing quick_check")


def gate_d3(ctx: DataContext) -> GateResult:
    bad = [_safe_rid(r, i) for i, r in enumerate(ctx.platfull) if r.get("db_id") not in EXPECTED_DB_IDS]
    return _result("D3", bad, "every Plat-Full db_id is expected", "unknown db_id referenced")


# --- M: Mini-Dev labels ----------------------------------------------------


def gate_m1(ctx: DataContext) -> GateResult:
    records = ctx.minidev
    bad: list[str] = []
    for i, rec in enumerate(records):
        try:
            _rid(rec)
        except ValueError:
            bad.append(f"#{i}")
            continue
        if set(rec) != MINIDEV_KEYS or rec.get("difficulty") not in DIFFICULTIES:
            bad.append(_safe_rid(rec, i))
    ok = not bad and len(records) == MINIDEV_COUNT
    msg = f"{len(records)} records" + ("" if len(records) == MINIDEV_COUNT else f", expected {MINIDEV_COUNT}")
    return GateResult("M1", ok, msg + (f", {len(bad)} malformed" if bad else ""), tuple(sort_ids(bad)))


def minidev_duplicates(records: list[Record]) -> dict[str, list[Record]]:
    groups: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        groups[_rid(rec)].append(rec)
    return {k: v for k, v in groups.items() if len(v) > 1}


def gate_m2(ctx: DataContext) -> GateResult:
    dups = minidev_duplicates(ctx.minidev)
    differing = [qid for qid, recs in dups.items() if any(r != recs[0] for r in recs[1:])]
    return _result("M2", differing, f"duplicates are exact copies: {sort_ids(dups)}", "duplicate ids with differing records")


# --- T: Arcwise-Plat-Full --------------------------------------------------


def gate_t1(ctx: DataContext) -> GateResult:
    bad: list[str] = []
    for i, rec in enumerate(ctx.platfull):
        keys = set(rec)
        question, sql, evidence = rec.get("question"), rec.get("SQL"), rec.get("evidence")
        if (not PLAT_KEYS <= keys or keys - PLAT_KEYS - PLAT_OPTIONAL
                or not isinstance(question, str) or not question
                or not isinstance(sql, str) or not sql
                or not (evidence is None or isinstance(evidence, str))):
            bad.append(_safe_rid(rec, i))
    return _result("T1", bad, "every record has the required keys and field types", "malformed records")


def gate_t2(ctx: DataContext) -> GateResult:
    ids: list[str] = []
    bad: list[str] = []
    for i, rec in enumerate(ctx.platfull):
        try:
            ids.append(_rid(rec))
        except ValueError:
            bad.append(f"#{i}")
    bad += [qid for qid, n in Counter(ids).items() if n > 1]
    ok = not bad and len(ctx.platfull) == PLAT_FULL_COUNT
    msg = f"{len(ctx.platfull)} records, expected {PLAT_FULL_COUNT}" + (f"; {len(bad)} invalid/duplicate ids" if bad else "")
    return GateResult("T2", ok, msg, tuple(sort_ids(bad)))


def gate_t3(ctx: DataContext) -> GateResult:
    """The difficulty join: same id set as Mini-Dev's unique ids, and the same db_id per id."""
    full, mini = ctx.platfull_by_id, ctx.minidev_by_id
    bad = list(set(full) ^ set(mini))
    bad += [q for q in set(full) & set(mini) if full[q].get("db_id") != mini[q].get("db_id")]
    return _result("T3", bad, "Plat-Full ids equal Mini-Dev unique ids, db_id equal per id", "join mismatch")


def gate_t4(ctx: DataContext) -> GateResult:
    difficulties: dict[str, set[object]] = defaultdict(set)
    for rec in ctx.minidev:
        difficulties[_rid(rec)].add(rec.get("difficulty"))
    bad = [qid for qid in ctx.platfull_by_id if len(difficulties.get(qid, set())) != 1]
    return _result("T4", bad, "every id gets exactly one Mini-Dev difficulty", "ids without exactly one difficulty")


def gate_t5(ctx: DataContext) -> GateResult:
    allowed, errors = load_allowlist(ctx.cfg.allowlist)
    if errors:
        return GateResult("T5", False, "invalid gold_allowlist.json: " + "; ".join(errors))
    check = ctx.gold()
    failures = {qid: o.status for qid, o in check.outcomes.items() if o.status != "ok" and (qid, o.status) not in allowed}
    source = "cache" if check.from_cache else "executed"
    return _result("T5", failures, f"{len(check.outcomes)} gold queries return rows ({source}, {check.seconds:.1f}s)",
                   f"gold failures ({source})", status=failures)


# --- S: descriptions -------------------------------------------------------


def gate_s1(ctx: DataContext) -> GateResult:
    """Arcwise schemas/ is per-db_id description CSVs, each naming a table of that database."""
    schemas = ctx.root / ARCWISE_SCHEMAS_DIR
    bad: list[str] = []
    for rel in _files_under(schemas):
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] not in EXPECTED_DB_IDS or parts[1] != "database_description" or not rel.endswith(".csv"):
            bad.append(rel)
            continue
        tables = {t.casefold() for t in ctx.tables(parts[0])}
        if Path(parts[2]).stem.casefold() not in tables:
            bad.append(rel)
    return _result("S1", bad, "Arcwise description files map to tables of the 11 databases", "unexpected Arcwise files")


def gate_s2(ctx: DataContext) -> GateResult:
    bad: list[str] = []
    for db_id in EXPECTED_DB_IDS:
        effective = descriptions.effective_descriptions(db_id, ctx.root)
        bad += [f"{db_id}.{t}" for t in ctx.tables(db_id) if t.casefold() not in effective]
    return _result("S2", bad, "every table has an effective description file", "tables without descriptions")


FAST_GATES: tuple[Callable[[DataContext], GateResult], ...] = (
    gate_v1, gate_v2, gate_v3, gate_v5, gate_d1, gate_d2, gate_d3, gate_m1, gate_m2,
    gate_t1, gate_t2, gate_t3, gate_t4, gate_s1, gate_s2,
)
SLOW_GATES: tuple[Callable[[DataContext], GateResult], ...] = (gate_t5,)


# --- audits ----------------------------------------------------------------


def audit_gold_runtime(ctx: DataContext) -> dict[str, object]:
    """A2, from the run that produced the gold outcomes (cached or fresh)."""
    outcomes = ctx.gold().outcomes
    slow = sort_ids(q for q, o in outcomes.items() if o.wall_s > ctx.cfg.sandbox_timeout_s)
    return {
        "gold_timeout_s": ctx.cfg.gold_timeout_s,
        "sandbox_timeout_s": ctx.cfg.sandbox_timeout_s,
        "wall_ms": {qid: round(outcomes[qid].wall_s * 1000, 1) for qid in sort_ids(outcomes)},
        "exceeds_sandbox_timeout": slow,
        "exceeds_sandbox_timeout_count": len(slow),
        "status_counts": dict(sorted(Counter(o.status for o in outcomes.values()).items())),
    }


def audit_date_dependent(ctx: DataContext) -> dict[str, object]:
    ids = sort_ids(q for q, r in ctx.platfull_by_id.items() if DATE_DEPENDENT_RE.search(str(r.get("SQL"))))
    return {"ids": ids, "count": len(ids)}


def audit_coverage(ctx: DataContext) -> dict[str, object]:
    counts: Counter[tuple[str, str]] = Counter()
    disagree: list[str] = []
    for qid, rec in ctx.platfull_by_id.items():
        mini = ctx.minidev_by_id.get(qid, {})
        counts[(str(mini.get("difficulty")), str(rec.get("db_id")))] += 1
        if "difficulty" in rec and rec["difficulty"] != mini.get("difficulty"):
            disagree.append(qid)
    table: dict[str, dict[str, int]] = defaultdict(dict)
    for (difficulty, db_id), n in sorted(counts.items()):
        table[difficulty][db_id] = n
    return {
        "minidev_duplicate_ids": sort_ids(minidev_duplicates(ctx.minidev)),
        "platfull_count": len(ctx.platfull_by_id),
        "difficulty_by_db": dict(table),
        "difficulty_totals": dict(sorted(Counter(k[0] for k in counts.elements()).items())),
        "platfull_records_with_difficulty": sum("difficulty" in r for r in ctx.platfull),
        "difficulty_disagreements": sort_ids(disagree),
    }


def audit_environment(ctx: DataContext) -> dict[str, object]:
    env = current_env()
    return {
        "python": env.python_version,
        "sqlite": env.sqlite_version,
        "platform": platform.platform(),
        "lock_sha256": hashlib.sha256(ctx.cfg.lock.read_bytes()).hexdigest(),
        "env_lock_sha256": hashlib.sha256(ctx.cfg.env_lock.read_bytes()).hexdigest(),
    }


def _decodes_utf8(path: Path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def audit_descriptions_encoding(ctx: DataContext) -> dict[str, object]:
    files: dict[str, dict[str, bool]] = {}
    for db_id in EXPECTED_DB_IDS:
        for d in (description_dir(db_id), arcwise_description_dir(db_id)):
            for p in sorted((ctx.root / d).glob("*.csv")):
                files[p.relative_to(ctx.root).as_posix()] = {
                    "utf8": _decodes_utf8(p),
                    "utf8_bom": p.read_bytes().startswith(b"\xef\xbb\xbf"),
                }
    effective = {
        db_id: {k: e.layer for k, e in descriptions.effective_layers(db_id, ctx.root).items()} for db_id in EXPECTED_DB_IDS
    }
    return {
        "files": files,
        "not_utf8": sorted(p for p, f in files.items() if not f["utf8"]),
        "total": len(files),
        "effective_layer": effective,
    }


def _read_description_rows(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Rows keyed by case-folded original_column_name: (column name as written, all cells stripped)."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        cells = [c.strip() for c in row]
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            out[cells[0].casefold()] = (cells[0], tuple(cells))
    return out


def _changed_columns(bird: dict[str, tuple[str, tuple[str, ...]]], arc: dict[str, tuple[str, tuple[str, ...]]]) -> list[str]:
    """Names of columns added, removed, or with any description cell changed."""
    names: set[str] = set()
    for key in set(bird) | set(arc):
        b, a = bird.get(key), arc.get(key)
        if b is None or a is None or b[1] != a[1]:
            names.add((a or b or ("", ()))[0])
    return sorted(names, key=str.casefold)


def audit_schemas_diff(ctx: DataContext) -> dict[str, object]:
    """A7: per db_id and case-folded stem, how Arcwise's file relates to BIRD's (names and counts only)."""
    per_db: dict[str, dict[str, object]] = {}
    counts: Counter[str] = Counter()
    for db_id in EXPECTED_DB_IDS:
        bird = descriptions.layer_files(ctx.root / description_dir(db_id))
        arc = descriptions.layer_files(ctx.root / arcwise_description_dir(db_id))
        entries: dict[str, object] = {}
        for stem in sorted(set(bird) | set(arc)):
            if stem not in arc:
                cls, entry = "bird_only", {}
            elif stem not in bird:
                cls, entry = "arcwise_only", {}
            elif bird[stem].read_bytes() == arc[stem].read_bytes():
                cls, entry = "identical", {}
            else:
                b, a = _read_description_rows(bird[stem]), _read_description_rows(arc[stem])
                cls, entry = "differs", {"rows_bird": len(b), "rows_arcwise": len(a),
                                         "changed_columns": _changed_columns(b, a)}
            counts[cls] += 1
            entries[stem] = {"class": cls, **entry}
        per_db[db_id] = entries
    return {"counts": dict(sorted(counts.items())), "databases": per_db}


def write_audits(ctx: DataContext) -> list[str]:
    audits: dict[str, Callable[[DataContext], dict[str, object]]] = {
        "gold_runtime.json": audit_gold_runtime,
        "date_dependent.json": audit_date_dependent,
        "coverage.json": audit_coverage,
        "environment.json": audit_environment,
        "descriptions_encoding.json": audit_descriptions_encoding,
        "schemas_diff.json": audit_schemas_diff,
    }
    ctx.cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in audits.items():
        _write_json(ctx.cfg.audit_dir / name, fn(ctx))
    return list(audits)


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- driver ----------------------------------------------------------------


def _run_gate(ctx: DataContext, fn: Callable[[DataContext], GateResult]) -> GateResult:
    name = fn.__name__.removeprefix("gate_").upper()
    try:
        return ctx.record(fn(ctx))
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        return ctx.record(GateResult(name, False, f"could not evaluate: {type(exc).__name__}: {exc}"))


def run_verify(cfg: DataConfig, use_cache: bool) -> int:
    ctx = DataContext(cfg, use_cache)
    report = cfg.audit_dir / "verify_report.json"
    try:
        _ = ctx.lock
    except (OSError, ValueError) as exc:
        print(f"FAIL V1: {exc}")
        return 1
    before = snapshot(ctx)
    results: list[GateResult] = []
    for fn in FAST_GATES + SLOW_GATES:
        result = _run_gate(ctx, fn)
        results.append(result)
        print(("PASS " if result.ok else "FAIL ") + str(result), flush=True)
    audit_error: str | None = None
    try:
        written = write_audits(ctx)
        print(f"audits written to {cfg.audit_dir}: {', '.join(written)}")
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        audit_error = f"{type(exc).__name__}: {exc}"
        print(f"audit error: {audit_error}")
    finally:
        ctx.close()
    v4 = gate_v4(ctx, before)
    results.append(v4)
    print(("PASS " if v4.ok else "FAIL ") + str(v4))
    failing = [r for r in results if not r.ok]
    if failing or audit_error:
        cfg.audit_dir.mkdir(parents=True, exist_ok=True)
        _write_json(report, {
            "generated_on": date.today().isoformat(),
            "failing": [{"gate": r.gate, "message": r.message, "ids": list(r.ids)} for r in failing],
            "audit_error": audit_error,
        })
        print(f"{len(failing)} gate(s) failed; see {report}")
        return 1
    report.unlink(missing_ok=True)
    print("all gates passed")
    return 0


# --- gold diagnosis (A9) ---------------------------------------------------


def diagnose_gold(cfg: DataConfig, ids: list[str], cap_s: float) -> dict[str, object]:
    """One-off: run selected Plat-Full gold on fresh connections with a long cap, and record plans."""
    ctx = DataContext(cfg)
    out_ids: dict[str, object] = {}
    walls: list[float] = []
    capped = False
    for qid in [normalize_id(i) for i in ids]:
        rec = ctx.platfull_by_id[qid]
        db_id, sql = str(rec.get("db_id")), str(rec.get("SQL"))
        conn = open_readonly(cfg.data_root / db_path(db_id))
        try:
            plan = [list(row) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
            print(f"run {qid} on {db_id} (cap {cap_s:.0f}s)", flush=True)
            outcome = run_gold(conn, sql, cap_s)
        finally:
            conn.close()
        cap_reached = outcome.status == "timeout"
        capped = capped or cap_reached
        if not cap_reached:
            walls.append(outcome.wall_s)
        out_ids[qid] = {
            "db_id": db_id,
            "status": outcome.status,
            "cap_reached": cap_reached,
            "wall_s": None if cap_reached else round(outcome.wall_s, 3),
            "rows": None if cap_reached else outcome.n_rows,
            "query_plan": plan,
        }
        print(f"  -> {outcome.status} in {outcome.wall_s:.1f}s, rows={outcome.n_rows}", flush=True)
    slowest = max(walls) if walls else None
    rule = None if capped or slowest is None else max(cfg.gold_timeout_s, math.ceil(2 * slowest))
    env = current_env()
    result: dict[str, object] = {
        "cap_s": cap_s,
        "python": env.python_version,
        "sqlite": env.sqlite_version,
        "ids": out_ids,
        "all_completed_within_cap": not capped,
        "slowest_wall_s": None if slowest is None else round(slowest, 3),
        "rule_gold_timeout_s": rule,
    }
    cfg.audit_dir.mkdir(parents=True, exist_ok=True)
    _write_json(cfg.audit_dir / "gold_diagnosis.json", result)
    return result
