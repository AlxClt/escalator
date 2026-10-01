"""CLI: `python -m escalator.datasets {lock,fetch,verify,env,diagnose-gold}`."""

from __future__ import annotations

import argparse
import sys

from escalator.datasets import bootstrap, fetch, verify
from escalator.datasets.config import load_config
from escalator.datasets.lock import LockError, current_env, load_lock, write_env_lock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m escalator.datasets")
    sub = parser.add_subparsers(dest="command", required=True)
    p_lock = sub.add_parser("lock", help="resolve upstream, download, write data/sources.lock")
    p_lock.add_argument("--force", action="store_true", help="overwrite an existing lock, reusing matching files")
    p_lock.add_argument("--ref", help="Arcwise commit SHA to pin instead of resolving main")
    sub.add_parser("fetch", help="fetch missing or invalid locked files (never re-locks)")
    p_verify = sub.add_parser("verify", help="run the verification gates and write audits")
    p_verify.add_argument("--offline", action="store_true", help="make any network attempt raise")
    p_verify.add_argument("--no-cache", action="store_true", help="re-execute every gold query")
    sub.add_parser("env", help="write data/env.lock from the running interpreter")
    p_diag = sub.add_parser("diagnose-gold", help="one-off: run selected gold queries with a long cap")
    p_diag.add_argument("--ids", nargs="+", required=True)
    p_diag.add_argument("--cap-s", type=float, default=1800.0)
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.command == "env":
        env = current_env()
        write_env_lock(env, cfg.env_lock)
        print(f"wrote {cfg.env_lock}: python {env.python_version}, sqlite {env.sqlite_version}")
        return 0

    if args.command == "diagnose-gold":
        fetch.install_network_guard()
        result = verify.diagnose_gold(cfg, args.ids, args.cap_s)
        print(f"all completed within cap: {result['all_completed_within_cap']}; "
              f"slowest {result['slowest_wall_s']}s; rule gold_timeout_s = {result['rule_gold_timeout_s']}")
        return 0

    if args.command == "lock":
        try:
            bootstrap.run_lock(cfg, force=args.force, ref=args.ref)
        except FileExistsError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        except (bootstrap.DiscoveryError, fetch.UnsafeArchive, LockError) as exc:
            print(f"lock failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "fetch":
        if not cfg.lock.is_file():
            print(f"{cfg.lock} is missing: run `make data-lock`", file=sys.stderr)
            return 1
        try:
            downloads = fetch.fetch_all(load_lock(cfg.lock), cfg.data_root)
        except (fetch.HashMismatch, fetch.UnsafeArchive, LockError) as exc:
            print(f"fetch failed: {exc}", file=sys.stderr)
            return 1
        print(f"downloads: {downloads}")
        return 0

    if args.offline:
        fetch.install_network_guard()
    return verify.run_verify(cfg, use_cache=not args.no_cache)


if __name__ == "__main__":
    sys.exit(main())
