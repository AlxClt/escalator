"""Download, hash and extract locked sources.

Every write goes to a temp file in the destination directory, is hashed while it is
written, and is renamed into place only when the hash matches the lock.
"""

from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Protocol

from escalator.datasets.lock import ExtractEntry, Lock, Source

CHUNK = 1 << 20
USER_AGENT = "escalator-data/1 (+https://github.com/)"
HTTP_TIMEOUT_S = 60
IGNORED_PREFIXES = ("__MACOSX/",)


class HashMismatch(RuntimeError):
    pass


class UnsafeArchive(RuntimeError):
    pass


class NetworkDisabled(RuntimeError):
    pass


class _Readable(Protocol):
    def read(self, n: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class Digest:
    sha256: str
    bytes: int


# --- hashing ---------------------------------------------------------------


def iter_chunks(stream: _Readable, chunk: int = CHUNK) -> Iterator[bytes]:
    while True:
        block = stream.read(chunk)
        if not block:
            return
        yield block


def sha256_stream(stream: _Readable, chunk: int = CHUNK) -> Digest:
    h = hashlib.sha256()
    size = 0
    for block in iter_chunks(stream, chunk):
        h.update(block)
        size += len(block)
    return Digest(h.hexdigest(), size)


def sha256_file(path: Path, chunk: int = CHUNK) -> Digest:
    with path.open("rb") as f:
        return sha256_stream(f, chunk)


def file_matches(path: Path, sha256: str, size: int) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    return sha256_file(path).sha256 == sha256


# --- network ---------------------------------------------------------------


def _urlopen(url: str, accept: str | None = None) -> IO[bytes]:
    """The single network entry point. Tests monkeypatch this."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    response: IO[bytes] = urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S)
    return response


def http_get_bytes(url: str, accept: str | None = None) -> bytes:
    with _urlopen(url, accept) as response:
        return response.read()


def install_network_guard() -> None:
    """Make any socket connection or DNS lookup raise for the rest of the process."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise NetworkDisabled("network access is disabled (--offline)")

    socket.socket.connect = refuse  # type: ignore[method-assign]  # deliberate process-wide guard
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


# --- atomic writes ---------------------------------------------------------


def write_verified(dest: Path, chunks: Iterable[bytes], expected: Digest | None) -> Digest:
    """Stream `chunks` to a temp file next to `dest`, hashing as it goes.

    If `expected` is given and differs, the temp file is removed and `dest` is untouched.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        h = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out:
            for block in chunks:
                h.update(block)
                size += len(block)
                out.write(block)
        got = Digest(h.hexdigest(), size)
        if expected is not None and got != expected:
            raise HashMismatch(
                f"{dest.name}: expected sha256 {expected.sha256} ({expected.bytes} B), "
                f"got {got.sha256} ({got.bytes} B)"
            )
        os.replace(tmp, dest)
        return got
    finally:
        tmp.unlink(missing_ok=True)


def download(url: str, dest: Path, expected: Digest | None) -> Digest:
    with _urlopen(url) as response:
        return write_verified(dest, iter_chunks(response), expected)


# --- archives --------------------------------------------------------------


def is_ignored_member(name: str) -> bool:
    base = PurePosixPath(name).name
    return name.startswith(IGNORED_PREFIXES) or base.startswith("._") or base == ".DS_Store"


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def check_archive(zf: zipfile.ZipFile) -> None:
    """Reject the whole archive if any member is absolute, escapes, or is a symlink."""
    bad: list[str] = []
    for info in zf.infolist():
        name = info.filename
        parts = name.replace("\\", "/").split("/")
        if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
            bad.append(f"absolute path: {name!r}")
        elif ".." in parts:
            bad.append(f"parent traversal: {name!r}")
        elif _is_symlink(info):
            bad.append(f"symlink: {name!r}")
    if bad:
        raise UnsafeArchive("unsafe archive members:\n  " + "\n  ".join(bad))


def data_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Regular file members, without macOS metadata."""
    return [i for i in zf.infolist() if not i.is_dir() and not is_ignored_member(i.filename)]


def extract_member(zf: zipfile.ZipFile, member: str, dest: Path, expected: Digest | None) -> Digest:
    with zf.open(member) as stream:
        return write_verified(dest, iter_chunks(stream), expected)


def extract_entries(archive: Path, entries: tuple[ExtractEntry, ...], root: Path, log: bool = True) -> None:
    """Extract exactly the locked entries that are missing or invalid, recursing into nested archives."""
    with zipfile.ZipFile(archive) as zf:
        check_archive(zf)
        names = set(zf.namelist())
        for entry in entries:
            dest = root / entry.path
            if entry.member not in names:
                raise HashMismatch(f"{archive.name}: locked member {entry.member!r} is missing")
            if file_matches(dest, entry.sha256, entry.bytes):
                continue
            if log:
                print(f"  extract {entry.member} -> {entry.path}")
            extract_member(zf, entry.member, dest, Digest(entry.sha256, entry.bytes))
    for entry in entries:
        if entry.extract:
            extract_entries(root / entry.path, entry.extract, root, log)


def _entries_valid(entries: tuple[ExtractEntry, ...], root: Path) -> bool:
    return all(
        file_matches(root / e.path, e.sha256, e.bytes) and _entries_valid(e.extract, root) for e in entries
    )


# --- fetch -----------------------------------------------------------------


def _ensure_file(url: str, rel: str, sha256: str, size: int, root: Path) -> bool:
    """Skip a valid file with no network request; otherwise download and verify it. True if downloaded."""
    dest = root / rel
    if file_matches(dest, sha256, size):
        return False
    print(f"download {url}")
    download(url, dest, Digest(sha256, size))
    return True


def ensure_source(src: Source, root: Path) -> int:
    """Make `src`, its extracted members and its files valid on disk. Returns the number of downloads."""
    downloads = 0
    if src.path is not None and src.sha256 is not None and src.bytes is not None:
        downloads += _ensure_file(src.url, src.path, src.sha256, src.bytes, root)
        if src.extract and not _entries_valid(src.extract, root):
            extract_entries(root / src.path, src.extract, root)
    for f in src.files:
        downloads += _ensure_file(src.file_url(f), f.path, f.sha256, f.bytes, root)
    print(f"{'ok' if not downloads else 'fetched':8} {src.name} ({downloads} download(s))")
    return downloads


def fetch_all(lock: Lock, root: Path) -> int:
    """Fetch every locked source. Returns the number of downloads made."""
    return sum(ensure_source(src, root) for src in lock.sources)

