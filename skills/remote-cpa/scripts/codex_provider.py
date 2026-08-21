#!/usr/bin/env python3
"""Enable or inspect the local Codex custom provider for remote CPA."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 on older macOS installations.
    import tomli as tomllib


VERSION = "2.1.0"
DEFAULT_MODEL = "gpt-5.6-sol"
PROVIDER_ID = "cliproxyapi"
OPENAI_PROVIDER_ID = "openai"
ACTIVE_RECENCY_MS = 15 * 60 * 1000
LOCK_NAME = "provider-operation.lock"
HISTORY_FREE_RESERVE_BYTES = 256 * 1024 * 1024
DEFAULT_LOCAL_CONFIG = Path.home() / ".codex" / "remote-cpa.local.json"
PREVIOUS_CONFIG_NAME = "remote-cpa.previous.json"


def codex_home() -> Path:
    return Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser()


def config_path() -> Path:
    return codex_home() / "config.toml"


def state_path() -> Path:
    return codex_home() / "state_5.sqlite"


def previous_config_path() -> Path:
    return codex_home() / PREVIOUS_CONFIG_NAME


def local_cpa_config() -> dict:
    path = Path(os.getenv("CPA_CONFIG", str(DEFAULT_LOCAL_CONFIG))).expanduser()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid local CPA config: {path}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Local CPA config must be a JSON object: {path}")
    return value


def configured_base_url() -> str:
    config = local_cpa_config()
    base_url = os.getenv("CPA_BASE_URL", str(config.get("base_url") or "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit("CPA base_url must be explicitly configured as an http(s) URL.")
    return base_url


def configured_provider_name() -> str:
    config = local_cpa_config()
    return os.getenv("CPA_PROVIDER_NAME", str(config.get("provider_name") or "CLIProxyAPI"))


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def provider_block() -> str:
    script = Path(__file__).resolve().with_name("cpa_request.py")
    python = Path(sys.executable).resolve()
    return "\n".join(
        [
            f"[model_providers.{PROVIDER_ID}]",
            f"name = {toml_string(configured_provider_name())}",
            f"base_url = {toml_string(configured_base_url())}",
            'wire_api = "responses"',
            "supports_websockets = true",
            "",
            f"[model_providers.{PROVIDER_ID}.auth]",
            f"command = {toml_string(str(python))}",
            f"args = [{toml_string(str(script))}, \"auth-token\"]",
            "timeout_ms = 30000",
            "refresh_interval_ms = 300000",
            "",
        ]
    )


def remove_provider_blocks(text: str) -> str:
    block = re.compile(
        rf"(?ms)^\[model_providers\.{re.escape(PROVIDER_ID)}\]\n.*?"
        rf"(?=^\[(?!model_providers\.{re.escape(PROVIDER_ID)}(?:\.|]))|\Z)"
    )
    text = block.sub("", text)
    auth_only = re.compile(
        rf"(?ms)^\[model_providers\.{re.escape(PROVIDER_ID)}\.auth\]\n.*?"
        rf"(?=^\[|\Z)"
    )
    return auth_only.sub("", text)


def split_top_level_preamble(text: str) -> tuple[str, str]:
    match = re.search(r"(?m)^\s*\[", text)
    return (text, "") if match is None else (text[: match.start()], text[match.start() :])


def set_preamble_value(preamble: str, key: str, value: str | None) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=\s*.*(?:\n|$)")
    preamble = pattern.sub("", preamble)
    if value is not None:
        preamble = f"{key} = {toml_string(value)}\n" + preamble
    return preamble


def set_top_level_routing(text: str, model: str | None, provider: str | None) -> str:
    preamble, tables = split_top_level_preamble(text)
    if model is not None:
        preamble = set_preamble_value(preamble, "model", model)
    preamble = set_preamble_value(preamble, "model_provider", provider)
    return preamble + tables


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_bytes_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_atomically(path: Path, text: str) -> None:
    write_bytes_atomically(path, text.encode("utf-8"))


def validate_toml(text: str) -> None:
    try:
        tomllib.loads(text)
    except Exception as exc:
        raise SystemExit(f"Refusing to write invalid Codex TOML: {exc}") from exc


def write_private_json(path: Path, value: dict) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    write_atomically(path, text)
    if os.name != "nt":
        path.chmod(0o600)


def load_previous_routing(path: Path) -> dict:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid previous routing snapshot: {path}") from exc
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise SystemExit(f"Unsupported previous routing snapshot: {path}")
    for key in ("model_present", "model_provider_present"):
        if not isinstance(snapshot.get(key), bool):
            raise SystemExit(f"Invalid previous routing snapshot field {key}: {path}")
    for present_key, value_key in (
        ("model_present", "model"),
        ("model_provider_present", "model_provider"),
    ):
        value = snapshot.get(value_key)
        if snapshot[present_key] and value is not None and not isinstance(value, str):
            raise SystemExit(f"Invalid previous routing snapshot field {value_key}: {path}")
    return snapshot


def routing_matches_snapshot(data: dict, snapshot: dict) -> bool:
    return (
        ("model" in data) == snapshot["model_present"]
        and data.get("model") == snapshot.get("model")
        and ("model_provider" in data) == snapshot["model_provider_present"]
        and data.get("model_provider") == snapshot.get("model_provider")
    )


def secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


@contextmanager
def operation_lock():
    """Serialize provider routing and history metadata mutations."""
    home = codex_home()
    secure_directory(home)
    path = home / LOCK_NAME
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SystemExit(f"Another Codex provider operation is already running: {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SystemExit(f"Another Codex provider operation is already running: {path}") from exc
        locked = True
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": datetime.now().astimezone().isoformat()},
            ensure_ascii=False,
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def save_previous_routing(original: str) -> None:
    path = previous_config_path()
    data = tomllib.loads(original) if original.strip() else {}
    if path.exists():
        snapshot = load_previous_routing(path)
        if data.get("model_provider") != PROVIDER_ID and not routing_matches_snapshot(data, snapshot):
            raise SystemExit(
                "Existing previous routing snapshot does not match the current routing; "
                f"refusing to reuse it: {path}"
            )
        return
    if data.get("model_provider") == PROVIDER_ID:
        return
    snapshot = {
        "version": 1,
        "model_present": "model" in data,
        "model": data.get("model"),
        "model_provider_present": "model_provider" in data,
        "model_provider": data.get("model_provider"),
    }
    write_private_json(path, snapshot)


def run_cpa_doctor(required_model: str | None = None) -> None:
    helper = Path(__file__).resolve().with_name("cpa_request.py")
    command = [sys.executable, str(helper), "doctor"]
    if required_model:
        command.extend(["--require-model", required_model])
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown failure"
        raise SystemExit(f"CPA preflight failed; Codex config was not changed: {detail}")
    print(result.stdout.rstrip())


def enable(model: str | None, dry_run: bool = False) -> None:
    path = config_path()
    config_existed = path.exists()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    current = tomllib.loads(original) if original.strip() else {}
    selected_model = model if model is not None else current.get("model")
    run_cpa_doctor(str(selected_model) if selected_model else None)
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    text = set_top_level_routing(text, model, PROVIDER_ID)
    text = remove_provider_blocks(text).rstrip() + "\n\n" + provider_block()
    validate_toml(text)
    if newline != "\n":
        text = text.replace("\n", newline)
    if dry_run:
        print(f"Codex CPA provider enable validated: {path}")
        print("dry_run=true")
        return
    snapshot_path = previous_config_path()
    snapshot_existed = snapshot_path.exists()
    save_previous_routing(original)
    try:
        if text != original:
            write_atomically(path, text)
    except Exception:
        if config_existed:
            write_atomically(path, original)
        elif path.exists():
            path.unlink()
            fsync_directory(path.parent)
        if not snapshot_existed and snapshot_path.exists():
            snapshot_path.unlink()
            fsync_directory(snapshot_path.parent)
        raise
    print(f"Codex CPA provider enabled: {path}")


def disable(model: str | None, dry_run: bool = False) -> None:
    path = config_path()
    if not path.exists():
        print(f"Codex config not found: {path}")
        return
    original = path.read_text(encoding="utf-8")
    current_data = tomllib.loads(original) if original.strip() else {}
    if current_data.get("model_provider") != PROVIDER_ID:
        raise SystemExit(
            f"Codex CPA provider is not active in {path}; refusing to disable unknown routing."
        )
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    snapshot_path = previous_config_path()
    if snapshot_path.exists():
        snapshot = load_previous_routing(snapshot_path)
        restored_model = snapshot.get("model") if snapshot.get("model_present") else None
        restored_provider = (
            snapshot.get("model_provider") if snapshot.get("model_provider_present") else None
        )
        text = set_top_level_routing(remove_provider_blocks(text), None, None)
        preamble, tables = split_top_level_preamble(text)
        preamble = set_preamble_value(preamble, "model", restored_model)
        preamble = set_preamble_value(preamble, "model_provider", restored_provider)
        text = preamble + tables
    else:
        if not model:
            raise SystemExit(
                "No previous routing snapshot exists. Re-run disable with --model <built-in-model>."
            )
        text = set_top_level_routing(remove_provider_blocks(text), model, None)
    text = text.rstrip() + "\n"
    validate_toml(text)
    if newline != "\n":
        text = text.replace("\n", newline)
    if dry_run:
        print(f"Codex CPA provider disable validated: {path}")
        print("dry_run=true")
        return
    changed = text != original
    if changed:
        write_atomically(path, text)
    if snapshot_path.exists():
        try:
            snapshot_path.unlink()
            fsync_directory(snapshot_path.parent)
        except Exception:
            if changed:
                write_atomically(path, original)
            raise
    print(f"Codex CPA provider disabled: {path}")


def provider_counts() -> list[tuple[str, int, int]]:
    path = state_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            """
            SELECT model_provider, archived, COUNT(*)
            FROM threads
            GROUP BY model_provider, archived
            ORDER BY model_provider, archived
            """
        ).fetchall()


def current_provider() -> str:
    path = config_path()
    if not path.exists():
        return "oauth"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Cannot determine the current provider from {path}: {exc}") from exc
    value = data.get("model_provider")
    if value is None or value == "":
        return "oauth"
    if not isinstance(value, str):
        raise SystemExit(f"Invalid top-level model_provider in {path}: {value!r}")
    return value


def history_status() -> None:
    path = state_path()
    if not path.exists():
        print(f"Codex state database not found: {path}")
        return
    print(f"state={path}")
    print("model_provider\tarchived\tthreads")
    for provider, archived, count in provider_counts():
        print(f"{provider}\t{archived}\t{count}")
    active, switching = active_thread_ids()
    print(f"active_threads={len(active)}")
    print(f"switching_threads={len(switching)}")
    users = state_database_users()
    print("state_db_users=" + (",".join(map(str, users)) if users else "none"))
    print(f"history_apply_blocked={str(bool(active or users)).lower()}")


def backup_root() -> Path:
    return codex_home() / "provider-history-backups"


def process_manager_path() -> Path:
    return codex_home() / "process_manager" / "chat_processes.json"


def process_exists(pid: object) -> bool:
    try:
        value = int(pid)
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def process_command(pid: object) -> str:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(int(pid)), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except (OSError, TypeError, ValueError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def command_matches(pid: object, expected: str) -> bool:
    actual = process_command(pid)
    if not actual:
        return False
    try:
        expected_parts = shlex.split(expected)
    except ValueError:
        expected_parts = expected.split()
    try:
        actual_parts = shlex.split(actual)
    except ValueError:
        actual_parts = actual.split()
    if not expected_parts or not actual_parts:
        return False
    expected_executable = Path(expected_parts[0]).name
    actual_executables = {Path(part).name for part in actual_parts[:3]}
    return expected_executable in actual_executables


def active_thread_ids() -> tuple[set[str], set[str]]:
    path = process_manager_path()
    if not path.exists():
        return set(), set()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set(), set()
    if not isinstance(entries, list):
        return set(), set()

    now_ms = int(time.time() * 1000)
    active: set[str] = set()
    switching: set[str] = set()
    script_name = Path(__file__).name
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        thread_id = entry.get("conversationId")
        command = str(entry.get("command") or "")
        if not thread_id:
            continue
        updated_at = entry.get("updatedAtMs")
        recent = isinstance(updated_at, (int, float)) and now_ms - int(updated_at) <= ACTIVE_RECENCY_MS
        os_pid = entry.get("osPid")
        process_id = entry.get("processId")
        alive = command_matches(os_pid, command) or (recent and process_exists(process_id))
        if not alive:
            continue
        active.add(thread_id)
        if script_name in command:
            switching.add(thread_id)
    return active, switching


def state_database_users() -> list[int] | None:
    """Return processes with the Codex state DB open, or None if unavailable."""
    if os.name == "nt":
        return None
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    result = subprocess.run(
        [lsof, "-t", str(state_path())],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        return None
    users = []
    for raw in result.stdout.splitlines():
        try:
            pid = int(raw.strip())
        except ValueError:
            continue
        if pid != os.getpid():
            users.append(pid)
    return sorted(set(users))


def history_mutation_blockers() -> list[str]:
    active, _ = active_thread_ids()
    users = state_database_users()
    blockers = []
    if users:
        blockers.append("Codex state database is open by process(es): " + ", ".join(map(str, users)))
    if active:
        blockers.append(f"{len(active)} Codex task(s) appear active")
    return blockers


def ensure_history_mutation_safe() -> None:
    blockers = history_mutation_blockers()
    if blockers:
        raise SystemExit(
            "History metadata mutation requires Codex Desktop to be fully closed. "
            + "; ".join(blockers)
        )


def select_history_rows(
    connection: sqlite3.Connection,
    target: str,
    source: str | None,
    include_archived: bool,
) -> list[tuple[str, str | None, str]]:
    where = ["(model_provider IS NULL OR model_provider <> ?)"]
    params: list[object] = [target]
    if source:
        where.append("model_provider = ?")
        params.append(source)
    if not include_archived:
        where.append("archived = 0")
    query = (
        "SELECT id, model_provider, rollout_path FROM threads WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at, id"
    )
    return connection.execute(query, params).fetchall()


def parse_session_metadata(raw: str) -> tuple[dict, str] | None:
    if '"session_meta"' not in raw or '"model_provider"' not in raw:
        return None
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict) or item.get("type") != "session_meta":
        return None
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id") or payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    return item, session_id


def collect_session_metadata(
    rows: list[tuple[str, str | None, str]], *, validate_provider: bool
) -> dict[str, str]:
    expected_by_id = {thread_id: provider for thread_id, provider, _ in rows}
    ids_by_path: dict[Path, set[str]] = {}
    for thread_id, _, rollout in rows:
        ids_by_path.setdefault(Path(rollout), set()).add(thread_id)
    found: dict[str, str] = {}
    errors: list[str] = []
    for path, expected_ids in sorted(ids_by_path.items(), key=lambda item: str(item[0])):
        if not path.exists():
            errors.append(f"missing transcript: {path}")
            continue
        remaining = set(expected_ids)
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line in handle:
                parsed = parse_session_metadata(line.rstrip("\r\n"))
                if parsed is None:
                    continue
                item, session_id = parsed
                if session_id not in remaining:
                    continue
                payload = item["payload"]
                if validate_provider and payload.get("model_provider") != expected_by_id[session_id]:
                    errors.append(
                        "transcript/database provider mismatch before mutation: "
                        f"{session_id} transcript={payload.get('model_provider')!r} "
                        f"database={expected_by_id[session_id]!r}"
                    )
                found[session_id] = line
                remaining.remove(session_id)
                if not remaining:
                    break
        for session_id in sorted(remaining):
            errors.append(f"session metadata not found: {session_id} in {path}")
    if errors:
        raise RuntimeError("history preflight failed:\n" + "\n".join(errors[:20]))
    return found


def rewrite_session_metadata(
    path: Path, provider_by_id: dict[str, str | None]
) -> set[str]:
    changed = False
    seen: set[str] = set()
    expected_ids = set(provider_by_id)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with path.open("r", encoding="utf-8", newline="") as source, os.fdopen(
            fd, "w", encoding="utf-8", newline=""
        ) as destination:
            for line in source:
                raw = line.rstrip("\r\n")
                ending = line[len(raw) :]
                parsed = parse_session_metadata(raw)
                if parsed is not None:
                    item, session_id = parsed
                    if session_id in provider_by_id:
                        seen.add(session_id)
                        payload = item["payload"]
                        target = provider_by_id[session_id]
                        if payload.get("model_provider") != target:
                            payload["model_provider"] = target
                            raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                            changed = True
                destination.write(raw + ending)
                if seen == expected_ids:
                    shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
                    break
            destination.flush()
            os.fsync(destination.fileno())
        missing = expected_ids - seen
        if missing:
            raise RuntimeError(
                f"session metadata not found while rewriting {path}: {', '.join(sorted(missing))}"
            )
        if changed:
            shutil.copystat(path, temp_name)
            os.replace(temp_name, path)
            fsync_directory(path.parent)
        else:
            os.unlink(temp_name)
        return seen
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def restore_session_metadata_lines(path: Path, line_by_id: dict[str, str]) -> set[str]:
    changed = False
    seen: set[str] = set()
    expected_ids = set(line_by_id)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with path.open("r", encoding="utf-8", newline="") as source, os.fdopen(
            fd, "w", encoding="utf-8", newline=""
        ) as destination:
            for line in source:
                parsed = parse_session_metadata(line.rstrip("\r\n"))
                if parsed is None or parsed[1] not in line_by_id:
                    destination.write(line)
                    continue
                session_id = parsed[1]
                replacement = line_by_id[session_id]
                destination.write(replacement)
                seen.add(session_id)
                changed = changed or replacement != line
                if seen == expected_ids:
                    shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
                    break
            destination.flush()
            os.fsync(destination.fileno())
        missing = expected_ids - seen
        if missing:
            raise RuntimeError(
                f"session metadata not found while restoring {path}: {', '.join(sorted(missing))}"
            )
        if changed:
            shutil.copystat(path, temp_name)
            os.replace(temp_name, path)
            fsync_directory(path.parent)
        else:
            os.unlink(temp_name)
        return seen
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def create_history_backup(
    connection: sqlite3.Connection,
    rows: list[tuple[str, str | None, str]],
    target: str,
    metadata_lines: dict[str, str],
) -> tuple[Path, dict[str, object]]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = backup_root() / f"{stamp}-to-{target}"
    suffix = 1
    while directory.exists():
        directory = backup_root() / f"{stamp}-to-{target}-{suffix}"
        suffix += 1
    secure_directory(backup_root())
    secure_directory(directory)

    backup_db = directory / "state_5.sqlite"
    with closing(sqlite3.connect(backup_db)) as backup_connection:
        connection.backup(backup_connection)
        backup_connection.commit()
    if os.name != "nt":
        backup_db.chmod(0o600)

    entries = []
    for thread_id, provider, rollout in rows:
        source_path = Path(rollout)
        entries.append(
            {
                "id": thread_id,
                "provider": provider,
                "rollout_path": str(source_path),
                "session_meta_line": metadata_lines[thread_id],
            }
        )
    manifest: dict[str, object] = {
        "version": 3,
        "created_at": datetime.now().astimezone().isoformat(),
        "target_provider": target,
        "backup_counts": {"metadata": len(entries)},
        "entries": entries,
    }
    write_private_json(directory / "manifest.json", manifest)
    return directory, manifest


def database_provider_map(
    connection: sqlite3.Connection, ids: set[str]
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        result.update(
            connection.execute(
                f"SELECT id, model_provider FROM threads WHERE id IN ({placeholders})", chunk
            ).fetchall()
        )
    return result


def validate_database_rows(
    connection: sqlite3.Connection, rows: list[tuple[str, str | None, str]]
) -> list[str]:
    expected = {thread_id: provider for thread_id, provider, _ in rows}
    actual = database_provider_map(connection, set(expected))
    errors = []
    for thread_id in sorted(expected):
        if thread_id not in actual:
            errors.append(f"database thread missing: {thread_id}")
        elif actual[thread_id] != expected[thread_id]:
            errors.append(f"database provider mismatch: {thread_id}={actual[thread_id]!r}")
    return errors


def validate_history_rows(
    connection: sqlite3.Connection,
    rows: list[tuple[str, str | None, str]],
    target: str | None,
) -> list[str]:
    expected_rows = [(thread_id, target, rollout) for thread_id, _, rollout in rows]
    errors = validate_database_rows(connection, expected_rows)
    ids = {thread_id for thread_id, _, _ in rows}
    if not ids:
        return errors

    seen: set[str] = set()
    for rollout_path in sorted({Path(rollout) for _, _, rollout in rows}):
        if not rollout_path.exists():
            errors.append(f"missing transcript: {rollout_path}")
            continue
        with rollout_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if '"session_meta"' not in line or '"model_provider"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "session_meta" or not isinstance(item.get("payload"), dict):
                    continue
                payload = item["payload"]
                thread_id = payload.get("id") or payload.get("session_id")
                if thread_id not in ids:
                    continue
                seen.add(thread_id)
                if payload.get("model_provider") != target:
                    errors.append(
                        f"transcript provider mismatch: {thread_id}={payload.get('model_provider')!r}"
                    )
    for thread_id in sorted(ids - seen):
        errors.append(f"session metadata not found: {thread_id}")
    return errors


def history_size_estimate(rows: list[tuple[str, str | None, str]]) -> dict[str, int]:
    sizes = []
    for path in {Path(rollout) for _, _, rollout in rows}:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            continue
    available = shutil.disk_usage(state_path().parent).free
    return {
        "logical_bytes": sum(sizes),
        "largest_transcript_bytes": max(sizes, default=0),
        "available_bytes": available,
    }


def print_sync_plan(
    target: str,
    rows: list[tuple[str, str | None, str]],
    blockers: list[str],
) -> dict[str, int]:
    sources: dict[str, int] = {}
    for _, provider, _ in rows:
        label = provider if provider is not None else "<null>"
        sources[label] = sources.get(label, 0) + 1
    source_summary = ", ".join(f"{provider}={count}" for provider, count in sorted(sources.items()))
    estimate = history_size_estimate(rows)
    print(f"target_provider={target}")
    print(f"threads_to_sync={len(rows)}")
    print(f"source_providers={source_summary or 'none'}")
    print(f"transcript_logical_bytes={estimate['logical_bytes']}")
    print(f"largest_transcript_bytes={estimate['largest_transcript_bytes']}")
    print(f"available_bytes={estimate['available_bytes']}")
    print("backup_mode=metadata-only")
    print(f"apply_blocked={str(bool(blockers)).lower()}")
    for blocker in blockers:
        print(f"blocker={blocker}")
    return estimate


def sync_history(
    target: str,
    source: str | None,
    include_archived: bool,
    include_active: bool = False,
    dry_run: bool = False,
) -> None:
    if not target or not re.fullmatch(r"[A-Za-z0-9._-]+", target):
        raise SystemExit(f"Invalid target provider ID: {target!r}")
    if include_active:
        raise SystemExit(
            "--include-active is no longer supported; close Codex Desktop before history mutation."
        )
    path = state_path()
    if not path.exists():
        raise SystemExit(f"Codex state database not found: {path}")
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        rows = select_history_rows(connection, target, source, include_archived)
        if not rows:
            print(f"No history needs syncing to {target}.")
            return
        blockers = history_mutation_blockers()
        estimate = print_sync_plan(target, rows, blockers)
        if dry_run:
            print("dry_run=true")
            return
        if blockers:
            ensure_history_mutation_safe()
        metadata_lines = collect_session_metadata(rows, validate_provider=True)
        required_free = (
            estimate["largest_transcript_bytes"]
            + path.stat().st_size * 2
            + HISTORY_FREE_RESERVE_BYTES
        )
        if estimate["available_bytes"] < required_free:
            raise SystemExit(
                "Insufficient free space for safe history mutation: "
                f"required={required_free} available={estimate['available_bytes']}"
            )
        backup_dir, manifest = create_history_backup(connection, rows, target, metadata_lines)
        provider_by_path: dict[Path, dict[str, str | None]] = {}
        original_lines_by_path: dict[Path, dict[str, str]] = {}
        for thread_id, _, rollout in rows:
            rollout_path = Path(rollout)
            provider_by_path.setdefault(rollout_path, {})[thread_id] = target
            original_lines_by_path.setdefault(rollout_path, {})[thread_id] = metadata_lines[thread_id]
        changed_paths: list[Path] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            for rollout_path in sorted(provider_by_path, key=str):
                rewrite_session_metadata(rollout_path, provider_by_path[rollout_path])
                changed_paths.append(rollout_path)
            connection.executemany(
                "UPDATE threads SET model_provider = ? WHERE id = ?",
                [(target, thread_id) for thread_id, _, _ in rows],
            )
            expected_rows = [(thread_id, target, rollout) for thread_id, _, rollout in rows]
            errors = validate_database_rows(connection, expected_rows)
            if errors:
                raise RuntimeError("history validation failed:\n" + "\n".join(errors[:20]))
            connection.commit()
        except Exception as exc:
            connection.rollback()
            rollback_errors = []
            for rollout_path in reversed(changed_paths):
                try:
                    restore_session_metadata_lines(
                        rollout_path, original_lines_by_path[rollout_path]
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(f"{rollout_path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    f"history mutation failed: {exc}; transcript rollback also failed:\n"
                    + "\n".join(rollback_errors[:20])
                    + f"\nrecovery_backup={backup_dir}"
                ) from exc
            raise
    backup_counts = manifest.get("backup_counts", {})
    print(f"Synced {len(rows)} Codex threads to provider {target}.")
    print(f"validation=passed")
    print(f"backup_methods={json.dumps(backup_counts, sort_keys=True)}")
    print(f"backup={backup_dir}")
    print("Restart Codex Desktop to refresh the sidebar history.")


def latest_backup() -> Path:
    candidates = sorted(backup_root().glob("*/manifest.json"))
    if not candidates:
        raise SystemExit(f"No provider history backups found in {backup_root()}")
    return candidates[-1].parent


def load_restore_manifest(backup: str) -> tuple[Path, list[dict]]:
    root = backup_root().resolve()
    directory = latest_backup().resolve() if backup == "latest" else Path(backup).expanduser().resolve()
    if directory.parent != root:
        raise SystemExit(f"Backup must be a direct child of {root}: {directory}")
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Backup manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid backup manifest: {manifest_path}") from exc
    if manifest.get("version") not in (2, 3):
        raise SystemExit(f"Unsupported backup manifest version: {manifest.get('version')!r}")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"Backup manifest has no entries: {manifest_path}")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"Invalid backup entry in {manifest_path}")
        thread_id = entry.get("id")
        provider = entry.get("provider")
        rollout_path = entry.get("rollout_path")
        if not isinstance(thread_id, str) or not thread_id or thread_id in seen:
            raise SystemExit(f"Invalid or duplicate thread ID in {manifest_path}: {thread_id!r}")
        if provider is not None and not isinstance(provider, str):
            raise SystemExit(f"Invalid provider for {thread_id} in {manifest_path}")
        if not isinstance(rollout_path, str) or not Path(rollout_path).is_absolute():
            raise SystemExit(f"Invalid rollout path for {thread_id} in {manifest_path}")
        seen.add(thread_id)
    return directory, entries


def current_rows_for_restore(
    connection: sqlite3.Connection, entries: list[dict]
) -> list[tuple[str, str | None, str]]:
    ids = [entry["id"] for entry in entries]
    found: dict[str, tuple[str | None, str]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for thread_id, provider, rollout in connection.execute(
            f"SELECT id, model_provider, rollout_path FROM threads WHERE id IN ({placeholders})",
            chunk,
        ):
            found[thread_id] = (provider, rollout)
    errors = []
    rows = []
    for entry in entries:
        thread_id = entry["id"]
        if thread_id not in found:
            errors.append(f"database thread missing: {thread_id}")
            continue
        provider, rollout = found[thread_id]
        if Path(rollout).resolve() != Path(entry["rollout_path"]).resolve():
            errors.append(f"rollout path changed for {thread_id}")
            continue
        rows.append((thread_id, provider, rollout))
    if errors:
        raise RuntimeError("restore preflight failed:\n" + "\n".join(errors[:20]))
    return rows


def restore_history(backup: str) -> None:
    ensure_history_mutation_safe()
    directory, entries = load_restore_manifest(backup)
    with closing(sqlite3.connect(state_path(), timeout=30)) as connection:
        current_rows = current_rows_for_restore(connection, entries)
        current_lines = collect_session_metadata(current_rows, validate_provider=True)
        safety_backup, _ = create_history_backup(
            connection, current_rows, "restore-safety", current_lines
        )
        target_by_path: dict[Path, dict[str, str | None]] = {}
        current_lines_by_path: dict[Path, dict[str, str]] = {}
        rollout_by_id = {thread_id: rollout for thread_id, _, rollout in current_rows}
        for entry in entries:
            rollout_path = Path(rollout_by_id[entry["id"]])
            target_by_path.setdefault(rollout_path, {})[entry["id"]] = entry["provider"]
            current_lines_by_path.setdefault(rollout_path, {})[entry["id"]] = current_lines[
                entry["id"]
            ]
        changed_paths: list[Path] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            for rollout_path in sorted(target_by_path, key=str):
                rewrite_session_metadata(rollout_path, target_by_path[rollout_path])
                changed_paths.append(rollout_path)
            connection.executemany(
                "UPDATE threads SET model_provider = ? WHERE id = ?",
                [(entry["provider"], entry["id"]) for entry in entries],
            )
            expected_rows = [
                (entry["id"], entry["provider"], rollout_by_id[entry["id"]]) for entry in entries
            ]
            errors = validate_database_rows(connection, expected_rows)
            if errors:
                raise RuntimeError("restore validation failed:\n" + "\n".join(errors[:20]))
            connection.commit()
        except Exception as exc:
            connection.rollback()
            rollback_errors = []
            for rollout_path in reversed(changed_paths):
                try:
                    restore_session_metadata_lines(
                        rollout_path, current_lines_by_path[rollout_path]
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(f"{rollout_path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    f"restore failed: {exc}; transcript rollback also failed:\n"
                    + "\n".join(rollback_errors[:20])
                    + f"\nsafety_backup={safety_backup}"
                ) from exc
            raise
    print(f"Restored provider metadata for {len(entries)} Codex threads.")
    print("validation=passed")
    print(f"restored_from={directory}")
    print(f"safety_backup={safety_backup}")
    print("Restart Codex Desktop to refresh the sidebar history.")


def verify_history(target: str, include_archived: bool) -> bool:
    path = state_path()
    if not path.exists():
        print(f"Codex state database not found: {path}")
        return False
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        archived_clause = "" if include_archived else " AND archived = 0"
        rows = connection.execute(
            "SELECT id, model_provider, rollout_path FROM threads "
            f"WHERE model_provider = ?{archived_clause} ORDER BY created_at, id",
            (target,),
        ).fetchall()
        other_count = connection.execute(
            "SELECT COUNT(*) FROM threads "
            f"WHERE (model_provider IS NULL OR model_provider <> ?){archived_clause}",
            (target,),
        ).fetchone()[0]
        errors = validate_history_rows(connection, rows, target)
    print(f"target_provider={target}")
    print(f"verified_threads={len(rows)}")
    print(f"other_provider_threads={other_count}")
    if errors:
        print("validation=failed")
        for error in errors[:20]:
            print(f"error={error}")
        return False
    print("validation=passed")
    if other_count:
        print("provider_split=true")
    else:
        print("provider_split=false")
    return True


def status() -> None:
    path = config_path()
    if not path.exists():
        print(f"Codex config not found: {path}")
        return
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    provider = data.get("model_providers", {}).get(PROVIDER_ID, {})
    auth = provider.get("auth", {}) if isinstance(provider, dict) else {}
    print(f"config={path}")
    print(f"model={data.get('model', '')}")
    print(f"model_provider={data.get('model_provider', '')}")
    print(f"base_url={provider.get('base_url', '') if isinstance(provider, dict) else ''}")
    print(f"auth_mode={'command' if auth.get('command') else 'not-configured'}")


def doctor() -> None:
    path = config_path()
    if not path.exists():
        raise SystemExit(f"Codex config not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Invalid Codex config: {path}: {exc}") from exc
    selected_model = data.get("model")
    run_cpa_doctor(str(selected_model) if selected_model else None)
    provider = data.get("model_providers", {}).get(PROVIDER_ID, {})
    auth = provider.get("auth", {}) if isinstance(provider, dict) else {}
    expected_script = str(Path(__file__).resolve().with_name("cpa_request.py"))
    errors = []
    if data.get("model_provider") != PROVIDER_ID:
        errors.append(f"top-level model_provider is not {PROVIDER_ID}")
    if not isinstance(provider, dict) or provider.get("base_url") != configured_base_url():
        errors.append("provider base_url does not match the local CPA config")
    if not isinstance(auth, dict) or not auth.get("command"):
        errors.append("command-backed authentication is not configured")
    elif auth.get("args") != [expected_script, "auth-token"]:
        errors.append("authentication helper path does not match this skill installation")
    if errors:
        for error in errors:
            print(f"provider_error={error}")
        raise SystemExit("Codex CPA provider doctor failed")
    print(f"codex_config={path}")
    print(f"model={data.get('model', '')}")
    print(f"model_provider={data.get('model_provider', '')}")
    print("provider_auth=command")
    print("provider_status=ok")


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "enable":
        enable(args.model, dry_run=args.dry_run)
    elif args.command == "disable":
        disable(args.model, dry_run=args.dry_run)
    elif args.command == "doctor":
        doctor()
    elif args.command == "version":
        print(VERSION)
    elif args.command == "history-status":
        history_status()
    elif args.command == "sync-history":
        sync_history(
            args.to,
            args.from_provider,
            not args.exclude_archived,
            include_active=args.include_active,
            dry_run=args.dry_run,
        )
    elif args.command == "restore-history":
        restore_history(args.backup)
    elif args.command == "verify-history":
        return 0 if verify_history(args.provider, not args.exclude_archived) else 1
    else:
        status()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Codex to use the remote CPA provider")
    sub = parser.add_subparsers(dest="command", required=True)
    enable_parser = sub.add_parser("enable")
    enable_parser.add_argument("--model", help="Set the default model; preserve the current model when omitted")
    enable_parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    disable_parser = sub.add_parser("disable")
    disable_parser.add_argument(
        "--model",
        help="Built-in model to restore when no previous routing snapshot exists",
    )
    disable_parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("version")
    sub.add_parser("history-status")
    sync_parser = sub.add_parser("sync-history")
    sync_parser.add_argument("--to", required=True, help="Target provider ID")
    sync_parser.add_argument("--from-provider", help="Only sync this source provider")
    sync_parser.add_argument(
        "--exclude-archived", action="store_true", help="Leave archived threads unchanged"
    )
    sync_parser.add_argument(
        "--include-active",
        action="store_true",
        help="Deprecated and rejected; close Codex Desktop instead",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="Show the migration plan without changing files"
    )
    restore_parser = sub.add_parser("restore-history")
    restore_parser.add_argument(
        "--backup", default="latest", help="Backup directory or 'latest'"
    )
    verify_parser = sub.add_parser("verify-history")
    verify_parser.add_argument("--provider", required=True, help="Provider ID to verify")
    verify_parser.add_argument(
        "--exclude-archived", action="store_true", help="Ignore archived threads"
    )
    args = parser.parse_args()
    mutating = (
        args.command in {"enable", "disable"} and not args.dry_run
    ) or (
        args.command == "sync-history" and not args.dry_run
    ) or args.command == "restore-history"
    if mutating:
        with operation_lock():
            return dispatch(args)
    return dispatch(args)


if __name__ == "__main__":
    raise SystemExit(main())
