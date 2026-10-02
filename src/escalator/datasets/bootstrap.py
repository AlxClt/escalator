"""`lock`: resolve upstream versions, download, discover archive members, write the lock."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from escalator.datasets import fetch
from escalator.datasets.config import (
    ARCWISE_SCHEMAS_DIR,
    EXPECTED_DB_IDS,
    MINIDEV_JSON,
    PLAT_FULL_JSON,
    DataConfig,
    db_path,
    description_dir,
)
from escalator.datasets.lock import (
    GIT_SHA_RE,
    LOCK_VERSION,
    ExtractEntry,
    Lock,
    RepoFile,
    Source,
    parse_lock,
    write_lock,
)

BIRD_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip"
BIRD_ARCHIVE = "_archives/minidev.zip"
BIRD_REPO = "bird-bench/mini_dev"
# GitHub detects no LICENSE file in bird-bench/mini_dev; the README badge states CC BY-SA 4.0.
BIRD_LICENSE_FALLBACK = "CC-BY-SA-4.0 (stated in the bird-bench/mini_dev README)"
ARCWISE_REPO = "uiuc-kang-lab/text_to_sql_benchmarks"
ARCWISE_RAW = f"https://raw.githubusercontent.com/{ARCWISE_REPO}"
GITHUB_API = "https://api.github.com/repos"
UNKNOWN_LICENSE = "UNKNOWN - see upstream"
SCHEMAS_UPSTREAM = "data/schemas/"


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Plan:
    """A member to extract during bootstrap."""

    member: str
    path: str


# --- upstream metadata -----------------------------------------------------


def resolve_github_sha(repo: str, ref: str) -> str:
    sha = fetch.http_get_bytes(f"{GITHUB_API}/{repo}/commits/{ref}", "application/vnd.github.sha")
    text = sha.decode("ascii").strip()
    if len(text) != 40:
        raise RuntimeError(f"unexpected SHA for {repo}@{ref}: {text!r}")
    return text


def list_github_tree(repo: str, sha: str, prefix: str) -> list[str]:
    raw: object = json.loads(fetch.http_get_bytes(f"{GITHUB_API}/{repo}/git/trees/{sha}?recursive=1"))
    if not isinstance(raw, dict) or raw.get("truncated") is not False or not isinstance(raw.get("tree"), list):
        raise RuntimeError(f"cannot list {repo}@{sha} (truncated or malformed tree)")
    tree: list[object] = raw["tree"]
    paths: list[str] = []
    for item in tree:
        if isinstance(item, dict) and item.get("type") == "blob":
            path = item.get("path")
            if isinstance(path, str) and path.startswith(prefix):
                paths.append(path)
    return sorted(paths)


def github_license(repo: str) -> str | None:
    try:
        raw: object = json.loads(fetch.http_get_bytes(f"{GITHUB_API}/{repo}/license"))
    except OSError:
        return None
    if isinstance(raw, dict):
        lic = raw.get("license")
        if isinstance(lic, dict):
            spdx = lic.get("spdx_id")
            if isinstance(spdx, str) and spdx not in ("", "NOASSERTION"):
                return f"{spdx} (GitHub license file of {repo})"
    return None


# --- Mini-Dev discovery ----------------------------------------------------


def discover_minidev(zf: zipfile.ZipFile) -> list[Plan]:
    """Find mini_dev_sqlite.json and, per expected db, its .sqlite and description CSVs."""
    members = [i.filename for i in fetch.data_members(zf)]
    problems: list[str] = []
    plans: list[Plan] = []

    tasks = [m for m in members if PurePosixPath(m).name == "mini_dev_sqlite.json"]
    if len(tasks) != 1:
        problems.append(f"expected one mini_dev_sqlite.json, found {tasks}")
    else:
        plans.append(Plan(tasks[0], MINIDEV_JSON))

    for db_id in EXPECTED_DB_IDS:
        dbs = [m for m in members if m.endswith(f"/{db_id}/{db_id}.sqlite") or m == f"{db_id}/{db_id}.sqlite"]
        if len(dbs) != 1:
            problems.append(f"{db_id}: expected one {db_id}.sqlite, found {dbs}")
            continue
        plans.append(Plan(dbs[0], db_path(db_id)))
        desc_prefix = str(PurePosixPath(dbs[0]).parent / "database_description") + "/"
        csvs = sorted(
            m for m in members if m.startswith(desc_prefix) and "/" not in m[len(desc_prefix) :] and m.endswith(".csv")
        )
        if not csvs:
            problems.append(f"{db_id}: no database_description/*.csv next to {dbs[0]}")
        for m in csvs:
            plans.append(Plan(m, f"{description_dir(db_id)}/{PurePosixPath(m).name}"))

    if problems:
        nested = [m for m in members if m.lower().endswith(".zip")]
        hint = f"\nnested archives present: {nested}" if nested else "\nno nested archives present"
        raise DiscoveryError("Mini-Dev discovery failed:\n  " + "\n  ".join(problems) + hint)
    return plans


# --- building the lock -----------------------------------------------------


@dataclass(frozen=True)
class Previous:
    """A file pinned by the previous lock, by URL: reused under FORCE=1 when it still matches."""

    path: str
    sha256: str
    bytes: int
    retrieved_on: str
    extract: tuple[ExtractEntry, ...] = ()


def previous_files(lock_path: Path) -> dict[str, Previous]:
    """Files of the existing lock (any earlier layout) keyed by download URL."""
    if not lock_path.is_file():
        return {}
    try:
        old = parse_lock(json.loads(lock_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    out: dict[str, Previous] = {}
    for src in old.sources:
        if src.path is not None and src.sha256 is not None and src.bytes is not None:
            out[src.url] = Previous(src.path, src.sha256, src.bytes, src.retrieved_on, src.extract)
        for f in src.files:
            out[src.file_url(f)] = Previous(f.path, f.sha256, f.bytes, src.retrieved_on)
    return out


@dataclass
class Builder:
    """Downloads sources for a new lock, reusing files that match the previous lock (FORCE=1)."""

    root: Path
    today: str
    previous: dict[str, Previous]

    def obtain(self, url: str, local: str) -> tuple[fetch.Digest, str, Previous | None]:
        """Returns the file's digest, its retrieval date, and the previous entry if reused."""
        prev = self.previous.get(url)
        dest = self.root / local
        if prev is not None and prev.path == local and fetch.file_matches(dest, prev.sha256, prev.bytes):
            print(f"reuse    {local} (matches previous lock)")
            return fetch.Digest(prev.sha256, prev.bytes), prev.retrieved_on, prev
        print(f"download {url}")
        return fetch.download(url, dest, None), self.today, None

    def bird(self) -> Source:
        digest, retrieved_on, prev = self.obtain(BIRD_URL, BIRD_ARCHIVE)
        old = {(e.member, e.path): e for e in prev.extract} if prev else {}
        entries: list[ExtractEntry] = []
        with zipfile.ZipFile(self.root / BIRD_ARCHIVE) as zf:
            fetch.check_archive(zf)
            for plan in discover_minidev(zf):
                kept = old.get((plan.member, plan.path))
                if kept and not kept.extract and fetch.file_matches(self.root / plan.path, kept.sha256, kept.bytes):
                    entries.append(kept)
                    continue
                got = fetch.extract_member(zf, plan.member, self.root / plan.path, None)
                entries.append(ExtractEntry(plan.member, plan.path, got.sha256, got.bytes))
        return Source(
            name="bird_minidev_zip",
            url=BIRD_URL,
            ref=None,
            license=github_license(BIRD_REPO) or BIRD_LICENSE_FALLBACK,
            retrieved_on=retrieved_on,
            path=BIRD_ARCHIVE,
            sha256=digest.sha256,
            bytes=digest.bytes,
            extract=tuple(entries),
        )

    def plat_full(self, sha: str, lic: str) -> Source:
        url = f"{ARCWISE_RAW}/{sha}/data/arcwise_plat_full_with_diff.json"
        digest, retrieved_on, _ = self.obtain(url, PLAT_FULL_JSON)
        return Source("arcwise_plat_full", url, sha, lic, retrieved_on, PLAT_FULL_JSON, digest.sha256, digest.bytes)

    def schemas(self, sha: str, lic: str) -> Source:
        """Every file under data/schemas/ at `sha`, kept in upstream layout under arcwise/schemas/."""
        files: list[RepoFile] = []
        dates: set[str] = set()
        for upstream in list_github_tree(ARCWISE_REPO, sha, SCHEMAS_UPSTREAM):
            local = f"{ARCWISE_SCHEMAS_DIR}/{upstream[len(SCHEMAS_UPSTREAM):]}"
            digest, retrieved_on, _ = self.obtain(f"{ARCWISE_RAW}/{sha}/{upstream}", local)
            dates.add(retrieved_on)
            files.append(RepoFile(upstream, local, digest.sha256, digest.bytes))
        if not files:
            raise DiscoveryError(f"no files under {SCHEMAS_UPSTREAM} at {sha}")
        return Source("arcwise_schemas", f"{ARCWISE_RAW}/{sha}/{SCHEMAS_UPSTREAM}", sha, lic,
                      max(dates), files=tuple(files))


def build_lock(root: Path, ref: str | None, previous: dict[str, Previous]) -> Lock:
    builder = Builder(root, date.today().isoformat(), previous)
    bird = builder.bird()
    if ref is None:
        sha = resolve_github_sha(ARCWISE_REPO, "main")
        print(f"arcwise main -> {sha}")
    else:
        if not GIT_SHA_RE.fullmatch(ref):
            raise DiscoveryError(f"REF must be a 40-hex commit SHA, got {ref!r}")
        sha = ref
        print(f"arcwise ref {sha} (REF)")
    lic = github_license(ARCWISE_REPO) or UNKNOWN_LICENSE
    return Lock(LOCK_VERSION, builder.today, (bird, builder.plat_full(sha, lic), builder.schemas(sha, lic)))


# --- NOTICE and summary ----------------------------------------------------


def render_notice(lock: Lock) -> str:
    lines = [
        "Third-party data used by this repository",
        "=========================================",
        "",
        "None of these files are committed. `make data` downloads them into data/raw/",
        "at the versions pinned in data/sources.lock.",
        "",
        "Arcwise-Plat-Full (the benchmark)",
        "  Jin et al., 2026, arXiv:2601.08778.",
        f"  https://github.com/{ARCWISE_REPO}",
        "",
        "BIRD Mini-Dev (SQLite databases, base description files, difficulty labels)",
        "  Li et al., \"Can LLM Already Serve as A Database Interface? A BIg Bench for",
        "  Large-Scale Database Grounded Text-to-SQLs\", NeurIPS 2023, arXiv:2305.03111.",
        "  https://github.com/bird-bench/mini_dev",
        "",
        "Pinned sources and licenses as stated upstream:",
    ]
    for src in sorted(lock.sources, key=lambda s: s.name):
        lines += [f"  - {src.name}: {src.license}", f"    {src.url}"]
    lines += ["", "These datasets are redistributed under their own licenses, not this repository's."]
    return "\n".join(lines) + "\n"


def _record_count(path: Path) -> str:
    if path.suffix != ".json":
        return ""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "  (unparseable JSON)"
    return f"  records={len(raw)}" if isinstance(raw, list) else "  (not a list)"


def summarize(lock: Lock, root: Path) -> str:
    lines = [f"lock created_on={lock.created_on}", f"github refs: {sorted({s.ref for s in lock.sources if s.ref})}"]
    for src in sorted(lock.sources, key=lambda s: s.name):
        if src.files:
            lines.append(f"{src.name} {len(src.files)} files, {sum(f.bytes for f in src.files):,} B")
        else:
            lines.append(f"{src.name} {src.bytes or 0:,} B sha256={src.sha256}{_record_count(root / str(src.path))}")
        lines += [f"  url: {src.url}", f"  license: {src.license}"]
        for e in src.extract:
            lines.append(f"  {e.member} -> {e.path} ({e.bytes:,} B){_record_count(root / e.path)}")
    return "\n".join(lines)


def run_lock(cfg: DataConfig, force: bool, ref: str | None) -> None:
    if cfg.lock.exists() and not force:
        raise FileExistsError(f"{cfg.lock} already exists; set FORCE=1 to overwrite it")
    lock = build_lock(cfg.data_root, ref, previous_files(cfg.lock))
    write_lock(lock, cfg.lock, force=force)
    (cfg.data_dir / "NOTICE").write_text(render_notice(lock), encoding="utf-8")
    print()
    print(summarize(lock, cfg.data_root))
    print()
    print(f"wrote {cfg.lock} and {cfg.data_dir / 'NOTICE'}; review them, then commit them yourself.")
