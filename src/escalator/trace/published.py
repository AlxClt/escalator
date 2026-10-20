"""Published traces
"""

from __future__ import annotations

import argparse
import gzip
import io
import shutil
import sys
from pathlib import Path

from escalator.datasets.config import REPO_ROOT
from escalator.trace.schema import META_FILE, STEPS_FILE, RunMeta, StepRecord, parse_steps, read_meta, read_steps

DEFAULT_TRACES = REPO_ROOT / "traces"
PUBLISHED = "published"
STEPS_GZ = STEPS_FILE + ".gz"


class RunNotFound(FileNotFoundError):
    pass


def run_dir(run_id: str, traces_root: Path = DEFAULT_TRACES) -> Path:
    """The directory holding run_id: published first, then the raw run directory."""
    for d in (traces_root / PUBLISHED / run_id, traces_root / run_id):
        if (d / META_FILE).is_file() and ((d / STEPS_GZ).is_file() or (d / STEPS_FILE).is_file()):
            return d
    raise RunNotFound(f"run {run_id!r} not found under {traces_root / PUBLISHED} or {traces_root}")


def load_meta(run_id: str, traces_root: Path = DEFAULT_TRACES) -> RunMeta:
    return read_meta(run_dir(run_id, traces_root) / META_FILE)


def load_steps(run_id: str, traces_root: Path = DEFAULT_TRACES) -> list[StepRecord]:
    d = run_dir(run_id, traces_root)
    gz = d / STEPS_GZ
    if gz.is_file():
        return parse_steps(gzip.decompress(gz.read_bytes()).decode("utf-8"), gz)
    return read_steps(d / STEPS_FILE)


def gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as f:
        f.write(data)
    return buf.getvalue()


def publish(run_id: str, traces_root: Path = DEFAULT_TRACES) -> Path:
    """Copy traces/<run_id>/ into traces/published/<run_id>/; the trace is validated first."""
    src = traces_root / run_id
    if not (src / META_FILE).is_file() or not (src / STEPS_FILE).is_file():
        raise RunNotFound(f"{src} holds no complete run")
    read_meta(src / META_FILE)
    read_steps(src / STEPS_FILE)  # strict: a truncated or malformed trace is never published
    dst = traces_root / PUBLISHED / run_id
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / META_FILE, dst / META_FILE)
    (dst / STEPS_GZ).write_bytes(gzip_bytes((src / STEPS_FILE).read_bytes()))
    return dst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.trace.published")
    p.add_argument("run_ids", nargs="+", help="run ids under traces/")
    args = p.parse_args(argv)
    for run_id in args.run_ids:
        try:
            dst = publish(run_id)
        except RunNotFound as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(f"published {run_id} -> {dst.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
