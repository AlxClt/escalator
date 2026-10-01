from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

from escalator.datasets import fetch
from escalator.datasets.lock import ExtractEntry, RepoFile, Source


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source(path: str, data: bytes, extract: tuple[ExtractEntry, ...] = ()) -> Source:
    return Source(
        name="s", url="https://example.invalid/s", ref=None, path=path,
        sha256=_sha(data), bytes=len(data), license="test", retrieved_on="2026-01-01", extract=extract,
    )


def _serve(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> list[str]:
    calls: list[str] = []

    def opener(url: str, accept: str | None = None) -> IO[bytes]:
        calls.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr(fetch, "_urlopen", opener)
    return calls


def _refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    def opener(url: str, accept: str | None = None) -> IO[bytes]:
        raise AssertionError(f"unexpected network call to {url}")

    monkeypatch.setattr(fetch, "_urlopen", opener)


# U1
@pytest.mark.parametrize("data", [b"", b"abc", bytes(range(256)) * 20_000], ids=["empty", "small", "multi-chunk"])
def test_u1_streaming_sha256_matches_hashlib(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "f"
    path.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert fetch.sha256_file(path, chunk=4096) == fetch.Digest(expected, len(data))
    assert fetch.sha256_stream(io.BytesIO(data), chunk=7).sha256 == expected


# U2
def test_u2_present_file_with_locked_hash_makes_no_network_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"pinned content"
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.json").write_bytes(data)
    _refuse(monkeypatch)
    assert fetch.ensure_source(_source("a/f.json", data), tmp_path) == 0


def test_u2_missing_file_is_downloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"pinned content"
    calls = _serve(monkeypatch, data)
    assert fetch.ensure_source(_source("a/f.json", data), tmp_path) == 1
    assert calls == ["https://example.invalid/s"]
    assert (tmp_path / "a" / "f.json").read_bytes() == data


# U3
def test_u3_hash_mismatch_raises_cleans_temp_and_keeps_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "f.json"
    dest.write_bytes(b"old, valid for some other lock")
    _serve(monkeypatch, b"tampered upstream")
    with pytest.raises(fetch.HashMismatch):
        fetch.download("https://example.invalid/s", dest, fetch.Digest(_sha(b"expected"), 8))
    assert dest.read_bytes() == b"old, valid for some other lock"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.json"]


# U4
def test_u4_exception_mid_stream_leaves_no_partial_file(tmp_path: Path) -> None:
    dest = tmp_path / "f.bin"

    def chunks() -> Iterator[bytes]:
        yield b"first block"
        raise ConnectionResetError("dropped")

    with pytest.raises(ConnectionResetError):
        fetch.write_verified(dest, chunks(), None)
    assert list(tmp_path.iterdir()) == []


def _zip(path: Path, members: dict[str, bytes], symlinks: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return path


# U5
@pytest.mark.parametrize(
    ("members", "symlinks"),
    [
        ({"ok/a.txt": b"a", "../x": b"evil"}, None),
        ({"ok/a.txt": b"a", "/abs/x": b"evil"}, None),
        ({"ok/a.txt": b"a", "ok/..\\..\\x": b"evil"}, None),
        ({"ok/a.txt": b"a"}, {"ok/link": "/etc/passwd"}),
    ],
)
def test_u5_unsafe_archives_rejected_before_writing(
    tmp_path: Path, members: dict[str, bytes], symlinks: dict[str, str] | None
) -> None:
    archive = _zip(tmp_path / "a.zip", members, symlinks)
    out = tmp_path / "out"
    entry = ExtractEntry("ok/a.txt", "x/a.txt", _sha(b"a"), 1)
    with pytest.raises(fetch.UnsafeArchive):
        fetch.extract_entries(archive, (entry,), out)
    assert not out.exists()


# U6
def test_u6_extraction_writes_exactly_the_allowlisted_members(tmp_path: Path) -> None:
    archive = _zip(
        tmp_path / "a.zip",
        {
            "root/keep.json": b"keep",
            "root/db/keep.sqlite": b"db",
            "root/other.json": b"not listed",
            "__MACOSX/root/._keep.json": b"meta",
        },
    )
    out = tmp_path / "out"
    entries = (
        ExtractEntry("root/keep.json", "bird/keep.json", _sha(b"keep"), 4),
        ExtractEntry("root/db/keep.sqlite", "bird/db/db.sqlite", _sha(b"db"), 2),
    )
    fetch.extract_entries(archive, entries, out, log=False)
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert written == ["bird/db/db.sqlite", "bird/keep.json"]


def test_u6_member_with_wrong_hash_is_not_written(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "a.zip", {"root/keep.json": b"keep"})
    out = tmp_path / "out"
    with pytest.raises(fetch.HashMismatch):
        fetch.extract_entries(archive, (ExtractEntry("root/keep.json", "k.json", _sha(b"other"), 4),), out, log=False)
    assert not (out / "k.json").exists()


def test_u6_extracted_members_are_restored_offline_from_kept_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_bytes_path = _zip(tmp_path / "src.zip", {"root/keep.json": b"keep"})
    data = archive_bytes_path.read_bytes()
    root = tmp_path / "raw"
    (root / "_archives").mkdir(parents=True)
    (root / "_archives" / "a.zip").write_bytes(data)
    _refuse(monkeypatch)
    src = _source("_archives/a.zip", data, (ExtractEntry("root/keep.json", "bird/keep.json", _sha(b"keep"), 4),))
    assert fetch.ensure_source(src, root) == 0
    assert (root / "bird" / "keep.json").read_bytes() == b"keep"


def test_u2_directory_source_files_skip_or_download_individually(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "a" * 40
    present, missing = b"present", b"missing"
    (tmp_path / "arcwise" / "schemas" / "db").mkdir(parents=True)
    (tmp_path / "arcwise" / "schemas" / "db" / "a.csv").write_bytes(present)
    src = Source(
        name="arcwise_schemas",
        url=f"https://raw.githubusercontent.com/o/r/{ref}/data/schemas/",
        ref=ref,
        license="test",
        retrieved_on="2026-01-01",
        files=(
            RepoFile("data/schemas/db/a.csv", "arcwise/schemas/db/a.csv", _sha(present), len(present)),
            RepoFile("data/schemas/db/b.csv", "arcwise/schemas/db/b.csv", _sha(missing), len(missing)),
        ),
    )
    calls = _serve(monkeypatch, missing)
    assert fetch.ensure_source(src, tmp_path) == 1
    assert calls == [f"https://raw.githubusercontent.com/o/r/{ref}/data/schemas/db/b.csv"]
