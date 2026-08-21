#!/usr/bin/env python3
"""Claude CLI -> Ollama Cloud Anthropic-compatible shim.

Forwards /v1/messages to Ollama Cloud with usage-weighted key rotation,
automatic retry on auth/quota failures, and ban mechanism for exhausted keys.

Also normalizes model names (strips [1m] etc.) and auto-switches to a vision
model when image content is detected in the request.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse


OLLAMA_BASE_URL = os.environ.get("OLLAMA_ANTHROPIC_BASE_URL", "https://ollama.com").rstrip("/")
OLLAMA_KEYS_FILE = os.environ.get("OLLAMA_ANTHROPIC_KEYS_FILE", os.path.expanduser("~/.claude/ollama_keys.json"))
OLLAMA_USAGE_FILE = os.environ.get("OLLAMA_USAGE_FILE", os.path.expanduser("~/.claude/ollama_usage.json"))
DEFAULT_VISION_MODEL = os.environ.get("OLLAMA_CLOUD_VISION_MODEL", "gemma4:31b")


def _log(message):
    """Write diagnostics when a console is available (including pythonw)."""
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(str(message) + "\n")
        stream.flush()
    except Exception:
        pass
VISION_MODEL_PREFIXES = ("gemma4", "gemma3")

_KEY_LOCK = threading.RLock()
_KEY_STATE = {"mtime": None, "keys": [], "index": 0}

# Tracks temporarily disabled keys after hard failures.
_BANNED_UNTIL = {}


def _json_bytes(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _load_key_pool():
    try:
        stat = os.stat(OLLAMA_KEYS_FILE)
    except FileNotFoundError:
        with _KEY_LOCK:
            _KEY_STATE.update({"mtime": None, "keys": [], "index": 0})
            return []

    with _KEY_LOCK:
        if _KEY_STATE["mtime"] == stat.st_mtime:
            return list(_KEY_STATE["keys"])

        with open(OLLAMA_KEYS_FILE, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()

        if raw.startswith("{"):
            payload = json.loads(raw)
            keys = payload.get("keys", [])
        elif raw.startswith("["):
            keys = json.loads(raw)
        else:
            keys = [line.strip() for line in raw.splitlines()]

        clean_keys = []
        seen = set()
        for key in keys:
            if isinstance(key, str):
                value = key.strip()
                if value and value not in seen:
                    clean_keys.append(value)
                    seen.add(value)

        _KEY_STATE.update({"mtime": stat.st_mtime, "keys": clean_keys, "index": 0})
        with _KEY_LOCK:
            _BANNED_UNTIL.clear()
        return list(clean_keys)


def _load_usage():
    """Load usage stats from ollama_usage.json. Missing data -> high usage."""
    try:
        with open(OLLAMA_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("keys", [])
    except Exception as exc:
        _log(f"usage_load_error: {exc}")
        return []


def _usage_stats(slot, usage):
    """Return (session_pct, weekly_pct, balance) for a slot, or None."""
    if slot < 0 or slot >= len(usage):
        return None
    stats = usage[slot]
    if stats is None:
        return None
    return (
        float(stats.get("session_pct", 100.0)),
        float(stats.get("weekly_pct", 100.0)),
        float(stats.get("balance", 0.0)),
    )


def _slot_weight(slot, usage):
    """
    Compute routing weight for a key slot.

    - Uses the larger of session_pct and weekly_pct as the 'consumed' fraction.
    - If session/weekly >= 100% and balance == 0, weight is 0 (dead key).
    - If balance > 0, give a fallback weight even if session/weekly are full.
    - Non-linear: remaining ** 2 so healthy keys are heavily preferred.
    """
    stats = _usage_stats(slot, usage)
    if stats is None:
        # No usage data: assume healthy to avoid total outage while updater runs.
        return 1.0

    session_pct, weekly_pct, balance = stats
    consumed = max(session_pct, weekly_pct) / 100.0

    if consumed >= 1.0 and balance <= 0:
        return 0.0

    if balance > 0 and consumed >= 1.0:
        return 0.3

    remaining = max(0.0, 1.0 - consumed)
    return remaining ** 2


def _is_banned(slot):
    with _KEY_LOCK:
        until = _BANNED_UNTIL.get(slot, 0)
        if time.time() < until:
            return True
        if until:
            del _BANNED_UNTIL[slot]
        return False


def _ban_slot(slot, seconds=60):
    with _KEY_LOCK:
        _BANNED_UNTIL[slot] = time.time() + seconds


def _ban_seconds_for_error(exc_code, usage, slot):
    """Determine ban duration based on error code and usage state."""
    stats = _usage_stats(slot, usage)
    if stats is not None:
        session_pct, weekly_pct, _balance = stats
        if max(session_pct, weekly_pct) >= 100.0:
            return 600  # 10 minutes for known-exhausted keys
    if exc_code == 429:
        return 120  # 2 minutes for rate-limit without known usage
    return 60  # default


def _next_key(exclude_slots=None):
    _load_key_pool()
    usage = _load_usage()
    exclude_slots = set(exclude_slots or [])

    with _KEY_LOCK:
        keys = _KEY_STATE["keys"]
        if not keys:
            return None, 0, 0

        candidates = []
        weights = []
        for slot, key in enumerate(keys):
            if slot in exclude_slots or _is_banned(slot):
                continue
            candidates.append((slot, key))
            weights.append(_slot_weight(slot, usage))

        if not candidates:
            # All keys excluded/banned; clear bans and retry with all keys.
            _BANNED_UNTIL.clear()
            candidates = list(enumerate(keys))
            weights = [_slot_weight(slot, usage) for slot, _ in candidates]
            if sum(weights) == 0:
                weights = [1.0] * len(candidates)

        total = sum(weights)
        if total <= 0:
            return None, 0, len(keys)

        pick = random.uniform(0, total)
        cumulative = 0.0
        chosen_slot, chosen_key = candidates[0]
        for (slot, key), weight in zip(candidates, weights):
            cumulative += weight
            if pick <= cumulative:
                chosen_slot, chosen_key = slot, key
                break

        return chosen_key, chosen_slot + 1, len(keys)


def _key_count():
    return len(_load_key_pool())


def _estimate_tokens(payload):
    text_parts = []

    def collect(value):
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for key in ("system", "content", "text", "name", "description"):
                if key in value:
                    collect(value[key])

    collect(payload.get("system"))
    collect(payload.get("messages", []))
    collect(payload.get("tools", []))

    text = "\n".join(text_parts)
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


# ── Model name normalization (local enhancement) ──────────────────────────

def normalize_cloud_model_name(model):
    """Strip [1m]/[1000k]/[1024k] suffixes and :cloud suffix from model names."""
    if not isinstance(model, str):
        return model
    normalized = model.strip()
    for suffix in ("[1m]", "[1000k]", "[1024k]"):
        if normalized.lower().endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    if normalized.endswith(":cloud"):
        normalized = normalized[: -len(":cloud")]
    return normalized


def payload_has_image(value):
    """Check whether the request payload contains image content."""
    if isinstance(value, dict):
        content_type = str(value.get("type") or "").lower()
        if content_type in ("image", "input_image", "image_url"):
            return True
        if "image_url" in value:
            return True
        source = value.get("source")
        if isinstance(source, dict) and source.get("media_type", "").startswith("image/"):
            return True
        return any(payload_has_image(child) for child in value.values())
    if isinstance(value, list):
        return any(payload_has_image(item) for item in value)
    return False


def _is_image_content_block(value):
    if not isinstance(value, dict):
        return False
    content_type = str(value.get("type") or "").lower()
    if content_type in ("image", "input_image", "image_url") or "image_url" in value:
        return True
    source = value.get("source")
    return isinstance(source, dict) and str(source.get("media_type") or "").lower().startswith("image/")


def _normalize_tool_result_images(payload):
    """Flatten image blocks nested in tool_result for Ollama compatibility."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False

    changed = False
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue

        normalized_content = []
        message_changed = False
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                normalized_content.append(block)
                continue

            block_content = block.get("content")
            if not isinstance(block_content, list) or not any(
                _is_image_content_block(item) for item in block_content
            ):
                normalized_content.append(block)
                continue

            message_changed = True
            text_parts = []
            image_parts = []
            for item in block_content:
                if _is_image_content_block(item):
                    image_parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                elif isinstance(item, str) and item:
                    text_parts.append(item)
                elif item is not None:
                    text_parts.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

            if text_parts:
                normalized_content.append({"type": "text", "text": "\n".join(text_parts)})
            normalized_content.extend(image_parts)

        if message_changed:
            message["content"] = normalized_content
            changed = True

    return changed


def is_vision_model(model):
    """Check whether the model name indicates a vision-capable model."""
    normalized = normalize_cloud_model_name(model)
    return isinstance(normalized, str) and normalized.startswith(VISION_MODEL_PREFIXES)


def normalize_request_body(raw_body):
    """Normalize model name in request body; auto-switch to vision model if needed."""
    if not raw_body:
        return raw_body
    try:
        payload = json.loads(raw_body)
    except Exception:
        return raw_body
    if not isinstance(payload, dict) or "model" not in payload:
        return raw_body
    changed = _normalize_tool_result_images(payload)
    normalized = normalize_cloud_model_name(payload.get("model"))
    if payload_has_image(payload) and not is_vision_model(normalized):
        normalized = DEFAULT_VISION_MODEL
    if normalized != payload.get("model"):
        payload["model"] = normalized
        changed = True
    if not changed:
        return raw_body
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── HTTP Handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _log("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def _send(self, status, body, content_type="application/json", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            usage = _load_usage()
            weights = [_slot_weight(i, usage) for i in range(_key_count())]
            info = {
                "ok": True,
                "key_count": _key_count(),
                "weights": weights,
                "banned_slots": list(_BANNED_UNTIL.keys()),
                "usage_stale": len(usage) == 0,
            }
            self._send(200, _json_bytes(info))
            return
        self._send(404, _json_bytes({"error": {"type": "not_found", "message": "not found"}}))

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/messages/count_tokens":
            try:
                payload = self._read_json()
                self._send(200, _json_bytes({"input_tokens": _estimate_tokens(payload)}))
            except Exception as exc:
                self._send(400, _json_bytes({"error": {"type": "bad_request", "message": str(exc)}}))
            return

        if path == "/v1/messages":
            self._proxy_messages()
            return

        self._send(404, _json_bytes({"error": {"type": "not_found", "message": "not found"}}))

    def _proxy_messages(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"

        # Normalize model name and handle vision detection
        body = normalize_request_body(body)

        max_attempts = max(1, _key_count())
        tried_slots = set()
        last_status = None
        last_body = b""
        last_content_type = "application/json"
        usage = _load_usage()

        for attempt in range(max_attempts):
            key, key_slot, key_count = _next_key(exclude_slots=tried_slots)
            if not key:
                # No keys available.
                self._send(400, _json_bytes({"error": {"type": "configuration_error", "message": "no keys available"}}))
                return

            slot_index = key_slot - 1
            tried_slots.add(slot_index)
            _log(f"upstream_key_slot={key_slot}/{key_count} attempt={attempt+1}/{max_attempts}")

            headers = {
                "Content-Type": self.headers.get("Content-Type", "application/json"),
                "Accept": self.headers.get("Accept", "application/json"),
            }
            for name in ("anthropic-version", "anthropic-beta"):
                value = self.headers.get(name)
                if value:
                    headers[name] = value

            headers["Authorization"] = "Bearer " + key
            headers["x-api-key"] = key

            target = OLLAMA_BASE_URL + "/v1/messages"
            req = Request(target, data=body, headers=headers, method="POST")

            try:
                with urlopen(req, timeout=600) as resp:
                    resp_body = resp.read()
                    response_headers = {}
                    for hdr in ("content-type", "request-id"):
                        value = resp.headers.get(hdr)
                        if value:
                            response_headers[hdr] = value
                    self._send(resp.status, resp_body, resp.headers.get("content-type", "application/json"), response_headers)
                    return
            except HTTPError as exc:
                last_status = exc.code
                last_body = exc.read()
                last_content_type = exc.headers.get("content-type", "application/json")
                _log(f"upstream_error slot={key_slot} status={exc.code} body={last_body[:200]}")

                if exc.code in (401, 402, 403, 429):
                    ban_seconds = _ban_seconds_for_error(exc.code, usage, slot_index)
                    _ban_slot(slot_index, ban_seconds)
                    continue
                if 500 <= exc.code < 600:
                    continue
                self._send(exc.code, last_body, last_content_type)
                return
            except URLError as exc:
                _log(f"upstream_urlerror slot={key_slot}: {exc}")
                _ban_slot(slot_index, 60)
                continue

        self._send(
            last_status or 502,
            last_body or _json_bytes({"error": {"type": "upstream_error", "message": "all keys failed"}}),
            last_content_type,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11435)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"ollama anthropic shim listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
