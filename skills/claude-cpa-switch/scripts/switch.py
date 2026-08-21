#!/usr/bin/env python3
"""claude-cpa-switch: 在 CPA(ChatGPT) 与 DeepSeek(Ollama) 两套 Claude Code 路由间切换。

- CPA 模式:      ANTHROPIC_BASE_URL=http://127.0.0.1:11436 (cpa_anthropic_shim.py -> CPA -> gpt-5.6-sol)
- DeepSeek 模式: ANTHROPIC_BASE_URL=http://127.0.0.1:11435 (ollama_anthropic_shim.py -> Ollama Cloud -> deepseek-v4-flash)
- OpenCode 模式: ANTHROPIC_BASE_URL=http://127.0.0.1:11437 (opencode_anthropic_shim.py -> opencode Go -> glm-5.3 主 / deepseek-v4-flash 副)

只改 ~/.claude/settings.json 的 env 块，其余设置原样保留；切换前自动备份。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HOME = pathlib.Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
SHIM = HOME / ".claude" / "cpa_anthropic_shim.py"
OPENCODE_SHIM = HOME / ".claude" / "opencode_anthropic_shim.py"
BACKUP_DIR = HOME / ".claude" / "backups" / "claude-cpa-switch"

CPA_PORT = 11436
OLLAMA_PORT = 11435
OPENCODE_PORT = 11437

DEFAULT_CPA_MODEL = "gpt-5.6-sol"
DEEPSEEK_MODEL = "deepseek-v4-flash"
OPENCODE_MAIN_MODEL = "glm-5.3"
OPENCODE_SMALL_MODEL = "deepseek-v4-flash"

# env 键 -> 值，切换时整体替换
MODEL_KEYS = [
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
]

# opencode 模式下：主力槽（fable/opus/sonnet/主模型）= main；轻量槽（haiku/small_fast）= small
OPENCODE_MAIN_KEYS = [
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_MODEL",
]
OPENCODE_SMALL_KEYS = [
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_SMALL_FAST_MODEL",
]


def log(msg: str) -> None:
    print(msg)


def read_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def write_settings(data: dict) -> None:
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup() -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"settings-{ts}.json"
    shutil.copy2(SETTINGS, dest)
    return dest


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_cpa_shim() -> None:
    if port_listening(CPA_PORT):
        log(f"cpa shim already listening on 127.0.0.1:{CPA_PORT}")
        return
    if not SHIM.exists():
        log(f"ERROR: shim not found: {SHIM}")
        sys.exit(1)
    pythonw = None
    exe = sys.executable
    if exe:
        base = pathlib.Path(exe)
        candidate = base.with_name("pythonw.exe")
        if candidate.exists():
            pythonw = str(candidate)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [pythonw or exe, str(SHIM), "--port", str(CPA_PORT)],
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"ERROR: failed to start shim: {exc}")
        sys.exit(1)
    for _ in range(20):
        time.sleep(0.5)
        if port_listening(CPA_PORT):
            log(f"cpa shim started on 127.0.0.1:{CPA_PORT}")
            return
    log("WARNING: cpa shim did not come up within 10s; check ~/.claude/cpa_anthropic_shim.py")


def start_shim(shim_path: pathlib.Path, port: int, label: str) -> None:
    """通用后台拉起 shim（pythonw 无窗口），监听 port。"""
    if port_listening(port):
        log(f"{label} shim already listening on 127.0.0.1:{port}")
        return
    if not shim_path.exists():
        log(f"ERROR: shim not found: {shim_path}")
        sys.exit(1)
    pythonw = None
    exe = sys.executable
    if exe:
        candidate = pathlib.Path(exe).with_name("pythonw.exe")
        if candidate.exists():
            pythonw = str(candidate)
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [pythonw or exe, str(shim_path), "--port", str(port)],
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"ERROR: failed to start shim: {exc}")
        sys.exit(1)
    for _ in range(20):
        time.sleep(0.5)
        if port_listening(port):
            log(f"{label} shim started on 127.0.0.1:{port}")
            return
    log(f"WARNING: {label} shim did not come up within 10s; check {shim_path}")


def start_cpa_shim() -> None:
    if port_listening(CPA_PORT):
        log(f"cpa shim already listening on 127.0.0.1:{CPA_PORT}")
        return
    start_shim(SHIM, CPA_PORT, "cpa")


def start_opencode_shim() -> None:
    start_shim(OPENCODE_SHIM, OPENCODE_PORT, "opencode")


def current_mode() -> str:
    data = read_settings()
    base = (data.get("env", {}) or {}).get("ANTHROPIC_BASE_URL", "")
    if str(OPENCODE_PORT) in base:
        return "opencode"
    if str(CPA_PORT) in base:
        return "cpa"
    if str(OLLAMA_PORT) in base:
        return "deepseek"
    return "unknown"


def apply_mode(target: str, model: str, small_model: str = "") -> None:
    data = read_settings()
    env = dict(data.get("env", {}) or {})
    if target == "cpa":
        env["ANTHROPIC_AUTH_TOKEN"] = "cpa-local"
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{CPA_PORT}"
        for key in MODEL_KEYS:
            env[key] = model
    elif target == "opencode":
        env["ANTHROPIC_AUTH_TOKEN"] = "opencode-local"
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{OPENCODE_PORT}"
        main_model = model or OPENCODE_MAIN_MODEL
        small = small_model or OPENCODE_SMALL_MODEL
        for key in OPENCODE_MAIN_KEYS:
            env[key] = main_model
        for key in OPENCODE_SMALL_KEYS:
            env[key] = small
    else:
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama-local"
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{OLLAMA_PORT}"
        for key in MODEL_KEYS:
            env[key] = DEEPSEEK_MODEL
    data["env"] = env
    write_settings(data)


def cmd_status() -> None:
    mode = current_mode()
    data = read_settings()
    env = data.get("env", {}) or {}
    log(f"mode={mode}")
    log(f"base_url={env.get('ANTHROPIC_BASE_URL', '')}")
    log(f"model={env.get('ANTHROPIC_MODEL', '')}")
    log(f"cpa_shim_listening={port_listening(CPA_PORT)}")
    log(f"ollama_shim_listening={port_listening(OLLAMA_PORT)}")
    log(f"opencode_shim_listening={port_listening(OPENCODE_PORT)}")
    log(f"settings={SETTINGS}")


def cmd_switch(target: str, model: str, dry_run: bool, small_model: str = "") -> None:
    before = current_mode()
    if before == target:
        log(f"already on target: {target} (nothing to do)")
        if target == "cpa":
            start_cpa_shim()
        elif target == "opencode":
            start_opencode_shim()
        return
    if dry_run:
        extra = ""
        if target == "cpa":
            extra = f" (model={model})"
        elif target == "opencode":
            extra = f" (main={model or OPENCODE_MAIN_MODEL}, small={small_model or OPENCODE_SMALL_MODEL})"
        log(f"dry-run: would switch {before} -> {target}{extra}")
        return
    dest = backup()
    log(f"backup -> {dest}")
    apply_mode(target, model, small_model)
    if target == "cpa":
        start_cpa_shim()
    elif target == "opencode":
        start_opencode_shim()
    log(f"route -> {target} set. 请完全退出并重启 Claude Code，下次启动生效。")


def main() -> None:
    ap = argparse.ArgumentParser(description="claude-cpa-switch: CPA(ChatGPT) <-> DeepSeek(Ollama) 切换")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看当前路由").set_defaults(func=cmd_status)
    p_cpa = sub.add_parser("cpa", help="切到 CPA(ChatGPT) 路由")
    p_cpa.add_argument("--model", default=DEFAULT_CPA_MODEL, help=f"CPA 模型（默认 {DEFAULT_CPA_MODEL}）")
    p_cpa.add_argument("--dry-run", action="store_true", help="只预检，不写任何东西")
    p_cpa.set_defaults(func=cmd_switch, target="cpa")
    p_ds = sub.add_parser("deepseek", help="切到 DeepSeek(Ollama) 路由")
    p_ds.add_argument("--dry-run", action="store_true", help="只预检，不写任何东西")
    p_ds.set_defaults(func=cmd_switch, target="deepseek", model=DEEPSEEK_MODEL)
    p_oc = sub.add_parser("opencode", help="切到 OpenCode Go 路由 (glm-5.3 主 / deepseek-v4-flash 副)")
    p_oc.add_argument("--main", default=OPENCODE_MAIN_MODEL, help=f"主力模型（默认 {OPENCODE_MAIN_MODEL}）")
    p_oc.add_argument("--small", default=OPENCODE_SMALL_MODEL, help=f"轻量模型（默认 {OPENCODE_SMALL_MODEL}）")
    p_oc.add_argument("--dry-run", action="store_true", help="只预检，不写任何东西")
    p_oc.set_defaults(func=cmd_switch, target="opencode", model=None)
    args = ap.parse_args()
    if args.cmd == "status":
        args.func()
    elif args.cmd == "opencode":
        args.func(args.target, args.main, args.dry_run, args.small)
    else:
        args.func(args.target, args.model, args.dry_run)


if __name__ == "__main__":
    main()
