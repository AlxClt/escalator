"""`data/sources.lock` (what data) and `data/env.lock` (which engine): schema, load, validate, write."""

from __future__ import annotations

import json
import platform
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LOCK_VERSION = 2
SOURCE_NAMES = ("bird_minidev_zip", "arcwise_plat_full", "arcwise_schemas")
ARCWISE_SOURCES = ("arcwise_plat_full", "arcwise_schemas")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
GITHUB_RAW_PREFIX = "https://raw.githubusercontent.com/"


class LockError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid lock:\n  " + "\n  ".join(errors))


@dataclass(frozen=True)
class ExtractEntry:
    """A member extracted from an archive. A nested archive carries its own `extract`."""

    member: str
    path: str
    sha256: str
    bytes: int
    extract: tuple[ExtractEntry, ...] = ()


@dataclass(frozen=True)
class RepoFile:
    """One file of a directory source, at its upstream repository path."""

    upstream: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Source:
    """A single-file source (`path`/`sha256`/`bytes`) or a directory source (`files`)."""

    name: str
    url: str
    ref: str | None
    license: str
    retrieved_on: str
    path: str | None = None
    sha256: str | None = None
    bytes: int | None = None
    extract: tuple[ExtractEntry, ...] = ()
    files: tuple[RepoFile, ...] = ()

    def file_url(self, f: RepoFile) -> str:
        """Download URL of one file of a directory source: the repo root at `ref`, plus its upstream path."""
        if self.ref is None or f"/{self.ref}/" not in self.url:
            raise ValueError(f"{self.name}: directory sources need a ref that appears in the URL")
        return self.url.split(f"/{self.ref}/")[0] + f"/{self.ref}/{f.upstream}"


@dataclass(frozen=True)
class Lock:
    lock_version: int
    created_on: str
    sources: tuple[Source, ...]

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        raise KeyError(name)


@dataclass(frozen=True)
class LockedFile:
    """Any file the lock pins: a downloaded file, an extracted member, or one file of a directory."""

    path: str
    sha256: str
    bytes: int
    source: str


def iter_extract(entries: tuple[ExtractEntry, ...], source: str) -> Iterator[LockedFile]:
    for entry in entries:
        yield LockedFile(entry.path, entry.sha256, entry.bytes, source)
        yield from iter_extract(entry.extract, source)


def iter_files(lock: Lock) -> Iterator[LockedFile]:
    for src in lock.sources:
        if src.path is not None and src.sha256 is not None and src.bytes is not None:
            yield LockedFile(src.path, src.sha256, src.bytes, src.name)
        yield from iter_extract(src.extract, src.name)
        for f in src.files:
            yield LockedFile(f.path, f.sha256, f.bytes, src.name)


# --- parsing ---------------------------------------------------------------


def _str(obj: dict[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise LockError([f"{where}: '{key}' must be a string"])
    return value


def _int(obj: dict[str, object], key: str, where: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LockError([f"{where}: '{key}' must be an integer"])
    return value


def _opt_str(obj: dict[str, object], key: str, where: str) -> str | None:
    return None if obj.get(key) is None else _str(obj, key, where)


def _opt_int(obj: dict[str, object], key: str, where: str) -> int | None:
    return None if obj.get(key) is None else _int(obj, key, where)


def _dict(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LockError([f"{where}: expected an object"])
    return {str(k): v for k, v in value.items()}


def _list(value: object, where: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LockError([f"{where}: expected a list"])
    return list(value)


def _parse_extract(value: object, where: str) -> tuple[ExtractEntry, ...]:
    entries: list[ExtractEntry] = []
    for i, item in enumerate(_list(value, where)):
        w = f"{where}[{i}]"
        obj = _dict(item, w)
        entries.append(
            ExtractEntry(
                member=_str(obj, "member", w),
                path=_str(obj, "path", w),
                sha256=_str(obj, "sha256", w),
                bytes=_int(obj, "bytes", w),
                extract=_parse_extract(obj.get("extract"), f"{w}.extract"),
            )
        )
    return tuple(entries)


def _parse_files(value: object, where: str) -> tuple[RepoFile, ...]:
    files: list[RepoFile] = []
    for i, item in enumerate(_list(value, where)):
        w = f"{where}[{i}]"
        obj = _dict(item, w)
        files.append(RepoFile(_str(obj, "upstream", w), _str(obj, "path", w), _str(obj, "sha256", w), _int(obj, "bytes", w)))
    return tuple(files)


def parse_lock(raw: object) -> Lock:
    """Parse a lock. Keys this version does not use (such as earlier `group` fields) are ignored,
    so a forced re-lock can read a previous lock to reuse files; only `validate_lock` judges it."""
    top = _dict(raw, "lock")
    sources: list[Source] = []
    for i, item in enumerate(_list(top.get("sources"), "sources")):
        w = f"sources[{i}]"
        obj = _dict(item, w)
        sources.append(
            Source(
                name=_str(obj, "name", w),
                url=_str(obj, "url", w),
                ref=_opt_str(obj, "ref", w),
                license=_str(obj, "license", w),
                retrieved_on=_str(obj, "retrieved_on", w),
                path=_opt_str(obj, "path", w),
                sha256=_opt_str(obj, "sha256", w),
                bytes=_opt_int(obj, "bytes", w),
                extract=_parse_extract(obj.get("extract"), f"{w}.extract"),
                files=_parse_files(obj.get("files"), f"{w}.files"),
            )
        )
    return Lock(
        lock_version=_int(top, "lock_version", "lock"),
        created_on=_str(top, "created_on", "lock"),
        sources=tuple(sources),
    )


# --- validation ------------------------------------------------------------


def _check_rel_path(path: str, where: str, errors: list[str]) -> None:
    p = PurePosixPath(path)
    if not path or p.is_absolute() or "\\" in path or ":" in path or ".." in p.parts:
        errors.append(f"{where}: path {path!r} must be relative, POSIX, without '..'")


def _check_digest(sha256: str | None, size: int | None, where: str, errors: list[str]) -> None:
    if sha256 is None or not SHA256_RE.fullmatch(sha256):
        errors.append(f"{where}: sha256 must be 64 lowercase hex")
    if size is None or size < 0:
        errors.append(f"{where}: bytes must be an integer >= 0")


def _check_entry(entry: ExtractEntry, where: str, errors: list[str]) -> None:
    _check_rel_path(entry.path, where, errors)
    if not entry.member:
        errors.append(f"{where}: empty member")
    _check_digest(entry.sha256, entry.bytes, where, errors)
    for j, sub in enumerate(entry.extract):
        _check_entry(sub, f"{where}.extract[{j}]", errors)


def github_ref_in_url(url: str) -> str | None:
    """For raw.githubusercontent.com/<owner>/<repo>/<ref>/..., return <ref>."""
    if not url.startswith(GITHUB_RAW_PREFIX):
        return None
    parts = url[len(GITHUB_RAW_PREFIX) :].split("/")
    return parts[2] if len(parts) > 3 else ""


def validate_lock(lock: Lock) -> list[str]:
    errors: list[str] = []
    if lock.lock_version != LOCK_VERSION:
        errors.append(f"lock_version must be {LOCK_VERSION}")
    if not DATE_RE.fullmatch(lock.created_on):
        errors.append("created_on must be YYYY-MM-DD")
    names = sorted(s.name for s in lock.sources)
    if names != sorted(SOURCE_NAMES):
        errors.append(f"sources must be exactly {sorted(SOURCE_NAMES)}, got {names}")
    github_shas: set[str] = set()
    for i, src in enumerate(lock.sources):
        w = f"sources[{i}] ({src.name})"
        if not src.url.startswith("https://"):
            errors.append(f"{w}: url must be https")
        if not src.license:
            errors.append(f"{w}: empty license")
        if not DATE_RE.fullmatch(src.retrieved_on):
            errors.append(f"{w}: retrieved_on must be YYYY-MM-DD")
        if src.files:
            if src.path is not None or src.sha256 is not None or src.extract:
                errors.append(f"{w}: a directory source carries only per-file paths and hashes")
            for j, f in enumerate(src.files):
                _check_rel_path(f.path, f"{w}.files[{j}]", errors)
                _check_rel_path(f.upstream, f"{w}.files[{j}].upstream", errors)
                _check_digest(f.sha256, f.bytes, f"{w}.files[{j}]", errors)
        else:
            if src.path is None:
                errors.append(f"{w}: missing path")
            else:
                _check_rel_path(src.path, w, errors)
            _check_digest(src.sha256, src.bytes, w, errors)
        url_ref = github_ref_in_url(src.url)
        if url_ref is not None:
            if src.ref is None or not GIT_SHA_RE.fullmatch(src.ref):
                errors.append(f"{w}: GitHub source needs ref = 40-hex commit SHA, got {src.ref!r}")
            elif url_ref != src.ref:
                errors.append(f"{w}: ref {src.ref} does not match the URL ref {url_ref!r}")
            else:
                github_shas.add(src.ref)
        elif src.name in ARCWISE_SOURCES:
            errors.append(f"{w}: Arcwise sources must be raw.githubusercontent.com URLs")
        for j, entry in enumerate(src.extract):
            _check_entry(entry, f"{w}.extract[{j}]", errors)
    if len(github_shas) > 1:
        errors.append(f"Arcwise sources must share one commit SHA, found {sorted(github_shas)}")
    seen: set[str] = set()
    for f in iter_files(lock):
        if f.path in seen:
            errors.append(f"duplicate path {f.path!r}")
        seen.add(f.path)
    return errors


def load_lock(path: Path) -> Lock:
    lock = parse_lock(json.loads(path.read_text(encoding="utf-8")))
    errors = validate_lock(lock)
    if errors:
        raise LockError(errors)
    return lock


# --- writing ---------------------------------------------------------------


def _entry_json(entry: ExtractEntry) -> dict[str, object]:
    out: dict[str, object] = {"member": entry.member, "path": entry.path, "sha256": entry.sha256, "bytes": entry.bytes}
    if entry.extract:
        out["extract"] = [_entry_json(e) for e in entry.extract]
    return out


def lock_to_json(lock: Lock) -> dict[str, object]:
    sources: list[dict[str, object]] = []
    for src in sorted(lock.sources, key=lambda s: s.name):
        obj: dict[str, object] = {"name": src.name, "url": src.url, "ref": src.ref}
        if not src.files:
            obj |= {"path": src.path, "sha256": src.sha256, "bytes": src.bytes}
        obj |= {"license": src.license, "retrieved_on": src.retrieved_on}
        if src.extract:
            obj["extract"] = [_entry_json(e) for e in src.extract]
        if src.files:
            obj["files"] = [{"upstream": f.upstream, "path": f.path, "sha256": f.sha256, "bytes": f.bytes} for f in src.files]
        sources.append(obj)
    return {"lock_version": lock.lock_version, "created_on": lock.created_on, "sources": sources}


def write_lock(lock: Lock, path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; set FORCE=1 to overwrite it")
    errors = validate_lock(lock)
    if errors:
        raise LockError(errors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock_to_json(lock), indent=2) + "\n", encoding="utf-8")


# --- env.lock --------------------------------------------------------------


@dataclass(frozen=True)
class EnvLock:
    """`data/env.lock`: the engine the data is verified on."""

    python_version: str
    sqlite_version: str


def current_env() -> EnvLock:
    return EnvLock(platform.python_version(), sqlite3.sqlite_version)


def load_env_lock(path: Path) -> EnvLock:
    obj = _dict(json.loads(path.read_text(encoding="utf-8")), "env.lock")
    return EnvLock(_str(obj, "python_version", "env.lock"), _str(obj, "sqlite_version", "env.lock"))


def write_env_lock(env: EnvLock, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"python_version": env.python_version, "sqlite_version": env.sqlite_version}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
