#!/usr/bin/env python3
"""Dependency-free model, chat, and image client for an OpenAI-compatible CPA."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


VERSION = "2.1.0"
DIRECT_OPENER = build_opener(ProxyHandler({}))
DEFAULT_LOCAL_CONFIG = Path.home() / ".codex" / "remote-cpa.local.json"
DEFAULT_IMAGE_RESPONSES_MODEL = "gpt-5.5"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
MAX_IMAGE_REFERENCES = 6
MAX_IMAGE_BYTES = 6 * 1024 * 1024

REMOTE_KEY_SCRIPT = r'''from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text()
in_keys = False
api_key = ""
for raw in text.splitlines():
    if raw.startswith("api-keys:"):
        in_keys = True
        continue
    if in_keys and raw and not raw[0].isspace():
        break
    if in_keys and raw.lstrip().startswith("-"):
        api_key = raw.split("-", 1)[1].strip().strip('"').strip("'")
        break
if not api_key:
    raise SystemExit("remote api-keys entry not found")
print(api_key)
'''


class CpaError(RuntimeError):
    pass


def local_config_path() -> Path:
    return Path(os.getenv("CPA_CONFIG", str(DEFAULT_LOCAL_CONFIG))).expanduser()


def reject_embedded_secrets(value: object, location: str = "config") -> None:
    forbidden = {
        "api_key",
        "cpa_api_key",
        "token",
        "oauth_token",
        "access_token",
        "refresh_token",
        "password",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in forbidden:
                raise CpaError(f"secret field is not allowed in local CPA config: {location}.{key}")
            reject_embedded_secrets(nested, f"{location}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            reject_embedded_secrets(nested, f"{location}[{index}]")


def local_config() -> dict:
    path = local_config_path()
    if not path.exists():
        raise CpaError(f"local CPA config was not found: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise CpaError(f"local CPA config permissions must be 0600: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CpaError(f"invalid local CPA config: {path}") from exc
    if not isinstance(value, dict):
        raise CpaError(f"local CPA config must be a JSON object: {path}")
    reject_embedded_secrets(value)
    return value


def config_text(config: dict, env_name: str, key: str, default: str = "") -> str:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value.strip()
    return str(config.get(key, default) or "").strip()


def config_bool(config: dict, env_name: str, key: str, default: bool = False) -> bool:
    env_value = os.getenv(env_name)
    value = env_value if env_value is not None else config.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def read_local_api_key(path: Path) -> str:
    """Read the api-keys entry directly from a local CPA config file.

    Host-machine mode: when this machine runs CPA itself, the OpenAI-compatible
    API key already lives in the local config.local.yaml, so no SSH round-trip
    is needed. The key is read into process memory only and never persisted,
    matching the security property of the SSH path. Reuses the same parsing as
    REMOTE_KEY_SCRIPT.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CpaError(f"local CPA config file could not be read: {path}") from exc
    in_keys = False
    api_key = ""
    for raw in text.splitlines():
        if raw.startswith("api-keys:"):
            in_keys = True
            continue
        if in_keys and raw and not raw[0].isspace():
            break
        if in_keys and raw.lstrip().startswith("-"):
            api_key = raw.split("-", 1)[1].strip().strip('"').strip("'")
            break
    if not api_key:
        raise CpaError(f"api-keys entry not found in local CPA config: {path}")
    return api_key


def run_ssh_capture(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def resolve_windows_ssh_key(
    command_prefix: list[str], remote_python: str, remote_config: str, timeout: float
) -> str:
    """Return the key through an SSH reverse tunnel, avoiding ssh.exe pipe hangs."""
    last_error = "no credential callback"
    for _ in range(3):
        nonce = secrets.token_hex(16)
        remote_port = 42000 + secrets.randbelow(20000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(timeout)
            local_port = listener.getsockname()[1]
            callback_script = REMOTE_KEY_SCRIPT.rsplit("print(api_key)", 1)[0] + f'''\
import socket
with socket.create_connection(("127.0.0.1", {remote_port}), timeout=10) as callback:
    callback.sendall(({nonce!r} + api_key).encode("utf-8"))
'''
            encoded_script = base64.b64encode(callback_script.encode("utf-8")).decode("ascii")
            bootstrap = f"import base64;exec(base64.b64decode({json.dumps(encoded_script)}))"
            remote_command = (
                shlex.join([remote_python, "-c", bootstrap, remote_config])
                + " >/dev/null 2>&1"
            )
            command = [
                *command_prefix[:-1],
                "-o",
                "ExitOnForwardFailure=yes",
                "-R",
                f"127.0.0.1:{remote_port}:127.0.0.1:{local_port}",
                command_prefix[-1],
                remote_command,
            ]
            process = subprocess.Popen(
                command,
                stdin=None,
                stdout=None,
                stderr=None,
            )
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(timeout)
                    chunks = []
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if sum(map(len, chunks)) > 16384:
                            raise CpaError("automatic SSH credential callback exceeded its size limit")
                process.wait(timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_error = str(exc)
                process.kill()
                process.wait()
                continue
            payload = b"".join(chunks).decode("utf-8", "replace")
            if process.returncode == 0 and payload.startswith(nonce):
                token = payload[len(nonce) :].strip()
                if token:
                    return token
            last_error = f"ssh exit {process.returncode} or invalid credential callback"
    raise CpaError(f"automatic SSH credential lookup failed: {last_error}")


def resolve_api_key() -> str:
    config = local_config()
    configured = os.getenv("CPA_API_KEY", "").strip()
    if configured:
        return configured
    if not config_bool(config, "CPA_SSH_AUTO", "ssh_auto", False):
        # Host-machine mode: read the api-key directly from the local CPA config
        # file (no SSH) when this machine runs CPA itself. Falls back to an error
        # when remote_config is unset or not present locally (i.e. a remote
        # machine, which must use ssh_auto=true).
        remote_config = config_text(config, "CPA_REMOTE_CONFIG", "remote_config")
        if not remote_config:
            raise CpaError("CPA_API_KEY is not set and ssh_auto is disabled; set remote_config to the local CPA config path or enable ssh_auto.")
        remote_path = Path(remote_config).expanduser()
        if not remote_path.is_file():
            raise CpaError(f"CPA_API_KEY is not set and local CPA config was not found: {remote_path}")
        return read_local_api_key(remote_path)

    ssh = shutil.which("ssh") or "ssh"
    host = config_text(config, "CPA_SSH_HOST", "ssh_host")
    user = config_text(config, "CPA_SSH_USER", "ssh_user")
    key_value = config_text(config, "CPA_SSH_KEY", "ssh_key")
    remote_config = config_text(config, "CPA_REMOTE_CONFIG", "remote_config")
    remote_python = config_text(config, "CPA_REMOTE_PYTHON", "remote_python", "python3")
    if not host or not user or not key_value or not remote_config or not remote_python:
        raise CpaError("SSH credential lookup is enabled but its host, user, key, or remote config is missing.")
    key_path = Path(key_value).expanduser()
    if not key_path.is_file():
        raise CpaError(f"CPA_API_KEY is not set and SSH key was not found: {key_path}")

    try:
        ssh_timeout = float(config_text(config, "CPA_SSH_TIMEOUT", "ssh_timeout", "20"))
    except ValueError as exc:
        raise CpaError("CPA_SSH_TIMEOUT must be numeric.") from exc
    if ssh_timeout <= 0:
        raise CpaError("CPA_SSH_TIMEOUT must be greater than zero.")

    encoded_script = base64.b64encode(REMOTE_KEY_SCRIPT.encode("utf-8")).decode("ascii")
    # JSON uses double quotes here, so shlex can wrap the complete Python snippet
    # in one simple pair of POSIX single quotes. This also survives Windows
    # OpenSSH's command-line forwarding without the nested '"'"' sequence.
    bootstrap = f"import base64;exec(base64.b64decode({json.dumps(encoded_script)}))"
    remote_command = shlex.join([remote_python, "-c", bootstrap, remote_config])

    command_prefix = [
        ssh,
        "-q",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        f"{user}@{host}",
    ]
    if os.name == "nt":
        return resolve_windows_ssh_key(
            command_prefix, remote_python, remote_config, ssh_timeout
        )
    command = [*command_prefix, remote_command]
    try:
        result = run_ssh_capture(command, ssh_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CpaError(f"automatic SSH credential lookup failed: {exc}") from exc
    if result.returncode != 0:
        raise CpaError("automatic SSH credential lookup failed; set CPA_API_KEY to override it")
    resolved = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(resolved) != 1:
        raise CpaError("remote CPA API key was not found; set CPA_API_KEY to override it")
    return resolved[0]


def configured_base_url(config: dict) -> str:
    base_url = config_text(config, "CPA_BASE_URL", "base_url").rstrip("/")
    parsed = urlparse(base_url)
    if not base_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CpaError("CPA base_url must be explicitly configured as an http(s) URL.")
    return base_url


def client_config() -> tuple[str, str, float]:
    config = local_config()
    base_url = configured_base_url(config)
    api_key = resolve_api_key()
    try:
        timeout = float(config_text(config, "CPA_TIMEOUT", "timeout", "60"))
    except ValueError as exc:
        raise CpaError("CPA_TIMEOUT must be numeric.") from exc
    return base_url, api_key, timeout


def command_doctor(required_model: str | None = None) -> None:
    path = local_config_path()
    config = local_config()
    base_url = configured_base_url(config)
    key_value = config_text(config, "CPA_SSH_KEY", "ssh_key")
    key_path = Path(key_value).expanduser() if key_value else None
    api_key = resolve_api_key()
    try:
        timeout = float(config_text(config, "CPA_TIMEOUT", "timeout", "60"))
    except ValueError as exc:
        raise CpaError("CPA_TIMEOUT must be numeric.") from exc
    result = request_json(base_url, api_key, timeout, "GET", "/models")
    models = result.get("data", []) if isinstance(result, dict) else []
    model_ids = {
        str(item["id"])
        for item in models
        if isinstance(item, dict) and item.get("id")
    }
    if required_model and required_model not in model_ids:
        raise CpaError(f"required model is not available: {required_model}")
    print(f"remote_cpa_version={VERSION}")
    print(f"config={path}")
    print("config_permissions=0600" if os.name != "nt" else "config_permissions=windows-acl")
    print(f"base_url={base_url}")
    print(f"ssh_auto={str(config_bool(config, 'CPA_SSH_AUTO', 'ssh_auto', False)).lower()}")
    print(f"ssh_key_present={str(bool(key_path and key_path.is_file())).lower()}")
    print("auth_token=resolved")
    print(f"model_count={len(model_ids)}")
    if required_model:
        print(f"required_model={required_model}")
        print("required_model_available=true")
    print("status=ok")


def request_raw(
    base_url: str,
    api_key: str,
    timeout: float,
    method: str,
    path: str,
    payload=None,
    accept: str = "application/json",
) -> tuple[str, bytes]:
    headers = {"Authorization": f"Bearer {api_key}", "Accept": accept}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base_url}/{path.lstrip('/')}", data=body, headers=headers, method=method)
    try:
        with DIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.headers.get("content-type", ""), response.read()
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = raw[:300]
        raise CpaError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise CpaError(f"connection failed: {exc.reason}") from exc


def request_json(base_url: str, api_key: str, timeout: float, method: str, path: str, payload=None):
    _, content = request_raw(base_url, api_key, timeout, method, path, payload)
    raw = content.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CpaError("CPA returned invalid JSON") from exc


def family(model_id: str) -> str:
    name = model_id.lower()
    if "grok" in name:
        return "Grok / xAI"
    if "claude" in name:
        return "Claude"
    if "gemini" in name:
        return "Gemini"
    if "gpt" in name or "codex" in name:
        return "OpenAI / Codex"
    if "kimi" in name:
        return "Kimi"
    return "Other"


def command_models(args) -> None:
    base_url, api_key, timeout = client_config()
    result = request_json(base_url, api_key, timeout, "GET", "/models")
    models = result.get("data", []) if isinstance(result, dict) else []
    models = [item for item in models if isinstance(item, dict) and item.get("id")]
    if args.grok_only:
        models = [item for item in models if family(item["id"]) == "Grok / xAI"]
    if args.contains:
        needle = args.contains.lower()
        models = [item for item in models if needle in item["id"].lower()]
    models.sort(key=lambda item: item["id"])
    if args.json:
        print(json.dumps(models, ensure_ascii=False, indent=2))
        return
    grouped = defaultdict(list)
    for item in models:
        grouped[family(item["id"])].append(item["id"])
    print(f"CPA: {base_url}")
    print(f"Models: {len(models)}")
    for group in sorted(grouped):
        print(f"\n[{group}] ({len(grouped[group])})")
        for model_id in grouped[group]:
            print(f"  {model_id}")


def command_chat(args) -> None:
    base_url, api_key, timeout = client_config()
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.message})
    result = request_json(
        base_url,
        api_key,
        timeout,
        "POST",
        "/chat/completions",
        {"model": args.model, "messages": messages, "max_tokens": args.max_tokens},
    )
    choices = result.get("choices", []) if isinstance(result, dict) else []
    if choices:
        print(choices[0].get("message", {}).get("content", ""))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def image_data_url(path: str) -> str:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise CpaError(f"reference image was not found: {source}")
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise CpaError(f"reference image exceeds 6 MB: {source}")
    mime_type = mimetypes.guess_type(source.name)[0]
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise CpaError(f"unsupported reference image type: {source}")
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def is_likely_base64(value: object) -> bool:
    normalized = re.sub(r"\s+", "", str(value or ""))
    return (
        len(normalized) > 100
        and len(normalized) % 4 == 0
        and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", normalized) is not None
    )


def append_image_url(found: list[str], value: str, limit: int = 1) -> None:
    if value and value not in found and len(found) < limit:
        found.append(value)


def find_image_data_urls(value: object, found: list[str] | None = None, limit: int = 1, depth: int = 0) -> list[str]:
    if found is None:
        found = []
    if value is None or depth > 12 or len(found) >= limit:
        return found
    if isinstance(value, str):
        if value.startswith("data:image/"):
            append_image_url(found, value, limit)
        return found
    if isinstance(value, list):
        for item in value:
            find_image_data_urls(item, found, limit, depth + 1)
        return found
    if not isinstance(value, dict):
        return found

    b64_json = value.get("b64_json")
    if isinstance(b64_json, str) and is_likely_base64(b64_json):
        normalized_b64 = re.sub(r"\s+", "", b64_json)
        append_image_url(found, f"data:image/png;base64,{normalized_b64}", limit)
    value_type = str(value.get("type") or "")
    if "image_generation_call" in value_type:
        result = value.get("result")
        if isinstance(result, str) and is_likely_base64(result):
            output_format = str(value.get("output_format") or "png").lower()
            mime_type = "image/jpeg" if output_format in {"jpg", "jpeg"} else "image/webp" if output_format == "webp" else "image/png"
            normalized_result = re.sub(r"\s+", "", result)
            append_image_url(found, f"data:{mime_type};base64,{normalized_result}", limit)
    url = value.get("url")
    if isinstance(url, str) and url.startswith("data:image/"):
        append_image_url(found, url, limit)
    for nested in value.values():
        find_image_data_urls(nested, found, limit, depth + 1)
    return found


def response_payloads(content_type: str, content: bytes) -> list[object]:
    text = content.decode("utf-8", "replace")
    if "application/json" in content_type:
        try:
            return [json.loads(text)]
        except json.JSONDecodeError as exc:
            raise CpaError("CPA returned invalid JSON for the image request") from exc

    payloads: list[object] = []
    event_lines: list[str] = []
    for raw_line in text.splitlines() + [""]:
        line = raw_line.rstrip("\r")
        if not line:
            if event_lines:
                event = "\n".join(event_lines).strip()
                event_lines = []
                if event and event != "[DONE]":
                    try:
                        payloads.append(json.loads(event))
                    except json.JSONDecodeError:
                        pass
            continue
        if line.startswith("data:"):
            event_lines.append(line[5:].lstrip())
    return payloads


def decode_image_data_url(data_url: str) -> tuple[str, bytes]:
    match = re.match(r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", data_url or "", re.S)
    if not match:
        raise CpaError("CPA returned an unsupported image payload")
    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[match.group(1)]
    try:
        return extension, base64.b64decode(match.group(2), validate=False)
    except ValueError as exc:
        raise CpaError("CPA returned invalid Base64 image data") from exc


def save_text(path: str, text: str) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return destination


def command_image(args) -> None:
    if len(args.image) > MAX_IMAGE_REFERENCES:
        raise CpaError(f"at most {MAX_IMAGE_REFERENCES} reference images are supported")
    references = [image_data_url(path) for path in args.image]
    action = args.action if args.action != "auto" else "edit" if references else "generate"
    payload = {
        "model": args.responses_model,
        "stream": True,
        "tool_choice": {"type": "image_generation"},
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": args.prompt},
                    *({"type": "input_image", "image_url": image} for image in references),
                ],
            }
        ],
        "tools": [
            {
                "type": "image_generation",
                "model": args.image_model,
                "action": action,
                "output_format": "png",
            }
        ],
    }
    request_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.request_output:
        print(f"request_output={save_text(args.request_output, request_text)}")
    if args.dry_run:
        print("dry_run=true")
        print(f"responses_model={args.responses_model}")
        print(f"image_model={args.image_model}")
        print(f"action={action}")
        print(f"reference_image_count={len(references)}")
        return

    base_url, api_key, _ = client_config()
    try:
        timeout = float(os.getenv("CPA_IMAGE_TIMEOUT", str(args.timeout)))
    except ValueError as exc:
        raise CpaError("CPA_IMAGE_TIMEOUT must be numeric") from exc
    content_type, content = request_raw(
        base_url,
        api_key,
        timeout,
        "POST",
        "/responses",
        payload,
        accept="application/json, text/event-stream",
    )
    if args.response_output:
        destination = Path(args.response_output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        print(f"response_output={destination}")
    images: list[str] = []
    for item in response_payloads(content_type, content):
        find_image_data_urls(item, images, limit=1)
        if images:
            break
    if not images:
        raise CpaError("CPA image response contained no final image")
    extension, image_bytes = decode_image_data_url(images[0])
    print(f"responses_model={args.responses_model}")
    print(f"image_model={args.image_model}")
    print(f"action={action}")
    print(f"reference_image_count={len(references)}")
    print(f"image_received=true")
    print(f"image_bytes={len(image_bytes)}")
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        if not destination.suffix:
            destination = destination.with_suffix(extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)
        print(f"output={destination}")


def command_auth_token() -> None:
    """Emit the CPA API key for Codex command-backed provider authentication."""
    print(resolve_api_key())


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the configured remote CLIProxyAPI")
    sub = parser.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models", help="list live models")
    models.add_argument("--grok-only", action="store_true")
    models.add_argument("--contains")
    models.add_argument("--json", action="store_true")
    chat = sub.add_parser("chat", help="send a chat completion")
    chat.add_argument("--model", default="grok-4.3")
    chat.add_argument("--message", required=True)
    chat.add_argument("--system")
    chat.add_argument("--max-tokens", type=int, default=256)
    image = sub.add_parser("image", help="send an image generation or edit request through CPA Responses")
    image.add_argument("--prompt", required=True)
    image.add_argument("--image", action="append", default=[], help="reference image path; repeat up to six times")
    image.add_argument("--responses-model", default=os.getenv("CPA_IMAGE_RESPONSES_MODEL", DEFAULT_IMAGE_RESPONSES_MODEL))
    image.add_argument("--image-model", default=os.getenv("CPA_IMAGE_MODEL", DEFAULT_IMAGE_MODEL))
    image.add_argument("--action", choices=["auto", "generate", "edit"], default="auto")
    image.add_argument("--timeout", type=float, default=600)
    image.add_argument("--output", help="save the returned final image")
    image.add_argument("--request-output", help="save the exact CPA request JSON")
    image.add_argument("--response-output", help="save the raw CPA JSON or SSE response")
    image.add_argument("--dry-run", action="store_true", help="build the CPA request without sending it")
    sub.add_parser("auth-token", help=argparse.SUPPRESS)
    doctor = sub.add_parser(
        "doctor", help="verify config, credential lookup, and the live models endpoint"
    )
    doctor.add_argument("--require-model", help="also require this exact live model ID")
    sub.add_parser("version", help="print the helper version")
    args = parser.parse_args()
    try:
        if args.command == "models":
            command_models(args)
        elif args.command == "image":
            command_image(args)
        elif args.command == "auth-token":
            command_auth_token()
        elif args.command == "doctor":
            command_doctor(args.require_model)
        elif args.command == "version":
            print(VERSION)
        else:
            command_chat(args)
        return 0
    except CpaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
