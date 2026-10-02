from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import IO

import pytest

from escalator.datasets import bootstrap, fetch
from escalator.datasets.config import DataConfig
from escalator.datasets.lock import (
    ExtractEntry,
    Lock,
    LockError,
    RepoFile,
    Source,
    load_lock,
    lock_to_json,
    parse_lock,
    validate_lock,
    write_lock,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
H = "0" * 64
RAW = "https://raw.githubusercontent.com/uiuc-kang-lab/text_to_sql_benchmarks"


def _bird() -> Source:
    return Source(
        "bird_minidev_zip", "https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip", None, "CC-BY-SA-4.0",
        "2026-01-01", "_archives/minidev.zip", H, 1,
        extract=(ExtractEntry("minidev/MINIDEV/mini_dev_sqlite.json", "bird/mini_dev_sqlite.json", H, 1),),
    )


def _plat_full(ref: str = SHA_A, url_ref: str | None = None) -> Source:
    return Source("arcwise_plat_full", f"{RAW}/{url_ref or ref}/data/arcwise_plat_full_with_diff.json", ref,
                  "CC-BY-SA-4.0", "2026-01-01", "arcwise/arcwise_plat_full_with_diff.json", H, 1)


def _schemas(ref: str = SHA_A, path: str = "arcwise/schemas/db/database_description/t.csv") -> Source:
    return Source("arcwise_schemas", f"{RAW}/{ref}/data/schemas/", ref, "CC-BY-SA-4.0", "2026-01-01",
                  files=(RepoFile("data/schemas/db/database_description/t.csv", path, H, 1),))


def _lock(*sources: Source) -> Lock:
    return Lock(2, "2026-01-01", sources or (_bird(), _plat_full(), _schemas()))


def test_valid_lock_has_no_errors() -> None:
    assert validate_lock(_lock()) == []


def test_lock_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sources.lock"
    write_lock(_lock(), path, force=False)
    assert load_lock(path) == Lock(2, "2026-01-01", tuple(sorted(_lock().sources, key=lambda s: s.name)))


# U7
def test_u7_lock_command_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    lock_path = tmp_path / "data" / "sources.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("{}", encoding="utf-8")
    cfg = DataConfig(tmp_path / "data" / "raw", lock_path, tmp_path / "data" / "env.lock", 60.0, 5.0)
    with pytest.raises(FileExistsError):
        bootstrap.run_lock(cfg, force=False, ref=None)
    assert lock_path.read_text(encoding="utf-8") == "{}"


def test_u7_write_lock_refuses_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / "sources.lock"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_lock(_lock(), path, force=False)
    write_lock(_lock(), path, force=True)
    assert load_lock(path).source("arcwise_plat_full") == _plat_full()


def test_u7_rejects_github_ref_that_is_not_a_sha() -> None:
    errors = validate_lock(_lock(_bird(), _plat_full(ref="main"), _schemas()))
    assert any("40-hex" in e for e in errors)


def test_u7_rejects_ref_not_matching_url() -> None:
    errors = validate_lock(_lock(_bird(), _plat_full(url_ref=SHA_B), _schemas()))
    assert any("does not match" in e for e in errors)


def test_u7_rejects_duplicate_paths() -> None:
    errors = validate_lock(_lock(_bird(), _plat_full(), _schemas(path="bird/mini_dev_sqlite.json")))
    assert any("duplicate path" in e for e in errors)


def test_requires_exactly_the_three_sources() -> None:
    assert any("exactly" in e for e in validate_lock(_lock(_bird(), _plat_full())))
    extra = replace(_plat_full(), name="arcwise_plat_sql", path="arcwise/other.json")
    assert any("exactly" in e for e in validate_lock(_lock(_bird(), _plat_full(), _schemas(), extra)))


def test_rejects_arcwise_sources_at_different_shas() -> None:
    errors = validate_lock(_lock(_bird(), _plat_full(), _schemas(ref=SHA_B)))
    assert any("share one commit SHA" in e for e in errors)


@pytest.mark.parametrize("path", ["/abs/x", "../x", "a/../../x", "C:/x", "a\\b"])
def test_rejects_unsafe_paths(path: str) -> None:
    errors = validate_lock(_lock(replace(_bird(), path=path), _plat_full(), _schemas()))
    assert any("must be relative" in e for e in errors)


def test_rejects_bad_hash_and_missing_fields(tmp_path: Path) -> None:
    assert any("64 lowercase hex" in e for e in validate_lock(_lock(replace(_bird(), sha256="ABC"), _plat_full(), _schemas())))
    path = tmp_path / "sources.lock"
    path.write_text(json.dumps({"lock_version": 2, "created_on": "2026-01-01", "sources": [{"name": "x"}]}),
                    encoding="utf-8")
    with pytest.raises(LockError):
        load_lock(path)


def test_schema_file_url_is_repo_root_at_ref_plus_upstream_path() -> None:
    src = _schemas()
    assert src.file_url(src.files[0]) == f"{RAW}/{SHA_A}/data/schemas/db/database_description/t.csv"


def test_earlier_layout_parses_for_reuse_but_does_not_validate() -> None:
    raw = lock_to_json(_lock(_bird(), _plat_full()))
    sources = raw["sources"]
    assert isinstance(sources, list)
    for s in sources:
        assert isinstance(s, dict)
        s["group"] = "core"
    old = parse_lock(raw)
    assert old.source("arcwise_plat_full") == _plat_full()
    assert validate_lock(old) != []


def test_force_relock_reuses_files_matching_previous_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"plat-full"
    url = f"{RAW}/{SHA_A}/data/arcwise_plat_full_with_diff.json"
    (tmp_path / "arcwise").mkdir()
    (tmp_path / "arcwise" / "arcwise_plat_full_with_diff.json").write_bytes(data)
    prev = bootstrap.Previous("arcwise/arcwise_plat_full_with_diff.json", hashlib.sha256(data).hexdigest(), len(data),
                              "2025-12-31")

    def refuse(url: str, accept: str | None = None) -> IO[bytes]:
        raise AssertionError("must not download a file that matches the previous lock")

    monkeypatch.setattr(fetch, "_urlopen", refuse)
    src = bootstrap.Builder(tmp_path, "2026-01-01", {url: prev}).plat_full(SHA_A, "x")
    assert (src.sha256, src.retrieved_on) == (prev.sha256, "2025-12-31")
