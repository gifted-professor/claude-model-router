#!/usr/bin/env python3
"""Safely switch Codex routing between CPA and normal OAuth.

Routing changes are fast and are the default. Sidebar-history migration is an
explicit ``--history`` operation and is rejected while Codex has its state
database open. This prevents a live transcript from being replaced underneath
an active Codex writer.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import codex_provider as provider


CPA_MODEL = "gpt-5.6-sol"
CPA_PROVIDER = "cliproxyapi"
OAUTH_PROVIDER = "openai"


def capture_file(path: Path) -> tuple[bool, bytes, int | None]:
    if not path.exists():
        return False, b"", None
    stat_result = path.stat()
    return True, path.read_bytes(), stat_result.st_mode


def restore_file(path: Path, snapshot: tuple[bool, bytes, int | None]) -> None:
    existed, content, mode = snapshot
    if existed:
        provider.write_bytes_atomically(path, content)
        if mode is not None and os.name != "nt":
            path.chmod(mode)
        return
    if path.exists():
        path.unlink()
        provider.fsync_directory(path.parent)


def _status() -> int:
    provider.status()
    print()
    provider.history_status()
    print()
    return 0


def _switch_locked(target: str, with_history: bool, dry_run: bool) -> int:
    before = provider.current_provider()
    if target == "cpa":
        detect = CPA_PROVIDER
        sync_target = CPA_PROVIDER
    else:
        detect = "oauth"
        sync_target = OAUTH_PROVIDER

    already = before == detect
    tag = "  (already on target)" if already else ""
    print(f"current provider: {before}  ->  target: {target}{tag}", flush=True)
    if with_history and not dry_run:
        provider.ensure_history_mutation_safe()

    config_snapshot = capture_file(provider.config_path())
    previous_snapshot = capture_file(provider.previous_config_path())
    route_attempted = not already and not dry_run
    try:
        if target == "cpa":
            if already:
                provider.doctor()
            else:
                provider.enable(CPA_MODEL, dry_run=dry_run)
        elif not already:
            provider.disable(None, dry_run=dry_run)
        elif dry_run:
            print("provider routing already on target; routing step skipped (dry-run).")

        if not dry_run and not already:
            if target == "cpa":
                provider.doctor()
            elif provider.current_provider() != "oauth":
                raise RuntimeError("OAuth routing verification failed after disable")

        blockers: list[str] = []
        if with_history:
            provider.sync_history(
                sync_target,
                source=None,
                include_archived=True,
                dry_run=dry_run,
            )
            if dry_run:
                blockers = provider.history_mutation_blockers()
        else:
            print("history_sync=not_requested")
            print(
                "Sidebar history metadata was not changed. To migrate it, close Codex Desktop "
                f"and run this command again with: {target} --history"
            )
    except BaseException:
        if route_attempted:
            restore_file(provider.config_path(), config_snapshot)
            restore_file(provider.previous_config_path(), previous_snapshot)
            print("provider routing restored after the failed operation.")
        raise

    if dry_run:
        print("dry_run=true")
        return 2 if blockers else 0
    if already and not with_history:
        print("nothing_to_change=true")
        return 0
    print("Restart Codex Desktop when convenient so routing and sidebar state reload.")
    return 0


def switch(target: str, with_history: bool, dry_run: bool) -> int:
    if dry_run:
        return _switch_locked(target, with_history, dry_run=True)
    with provider.operation_lock():
        return _switch_locked(target, with_history, dry_run=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Toggle Codex between CPA and OAuth login.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("cpa", "oauth"):
        command = sub.add_parser(name, help=f"switch routing to {name}")
        history = command.add_mutually_exclusive_group()
        history.add_argument(
            "--history",
            action="store_true",
            help="also migrate sidebar history; Codex Desktop must be fully closed",
        )
        history.add_argument(
            "--no-history",
            action="store_true",
            help=argparse.SUPPRESS,
        )
        command.add_argument("--dry-run", action="store_true", help="validate without writing")
    sub.add_parser("status", help="show current provider and history distribution")
    args = parser.parse_args()

    if args.cmd == "status":
        return _status()
    return switch(args.cmd, with_history=args.history, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
