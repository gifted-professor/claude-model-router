#!/usr/bin/env python3
"""Claude Code -> CPA (CLIProxyAPI) Anthropic-compatible shim.

Translates Anthropic /v1/messages requests to CPA's OpenAI /v1/chat/completions
endpoint, and translates responses (including streaming SSE) back to the
Anthropic format Claude Code expects.

CPA = ChatGPT 路由（默认 gpt-5.6-sol）。与 ollama_anthropic_shim.py（DeepSeek 路由）
互斥使用，由 claude-cpa-switch skill 切换。监听 127.0.0.1:11436。
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Reuse CPA connection/key resolution from the remote-cpa skill.
CPA_SCRIPTS = os.path.expanduser("~/.codex/skills/remote-cpa/scripts")
if CPA_SCRIPTS not in sys.path:
    sys.path.insert(0, CPA_SCRIPTS)
import cpa_request  # noqa: E402

DEFAULT_MODEL = os.environ.get("CPA_SHIM_MODEL", "gpt-5.6-sol")
KEY_TTL = 600  # seconds; CPA_API_KEY env var makes resolution instant anyway

_KEY_LOCK = threading.RLock()
_KEY_CACHE = {"value": None, "at": 0.0}
_BASE_URL_CACHE = {"value": None}

# OpenAI finish_reason -> Anthropic stop_reason
_STOP_REASON = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


def _log(message):
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(str(message) + "\n")
        stream.flush()
    except Exception:
        pass


def _json_bytes(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _cpa_config():
    """Return (base_url, api_key), cached with a TTL."""
    with _KEY_LOCK:
        now = time.time()
        if _KEY_CACHE["value"] and now - _KEY_CACHE["at"] < KEY_TTL:
            return _BASE_URL_CACHE["value"], _KEY_CACHE["value"]
    base_url, api_key, _timeout = cpa_request.client_config()
    with _KEY_LOCK:
        _BASE_URL_CACHE["value"] = base_url
        _KEY_CACHE.update({"value": api_key, "at": time.time()})
    return base_url, api_key


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


# ── Anthropic -> OpenAI request translation ────────────────────────────────

def _block_text(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") == "text":
        return block.get("text", "")
    return ""


def translate_request(payload):
    """Anthropic /v1/messages payload -> OpenAI /v1/chat/completions payload."""
    messages = []

    system = payload.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            texts = [_block_text(b) for b in system]
            messages.append({"role": "system", "content": "\n".join(t for t in texts if t)})

    for msg in payload.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                text_parts = []
                image_parts = []
                tool_results = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype in ("image", "input_image"):
                        source = block.get("source", {})
                        media_type = source.get("media_type", "")
                        data = source.get("data", "")
                        if data:
                            image_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"},
                            })
                    elif btype == "tool_result":
                        tool_results.append(block)
                if text_parts or image_parts:
                    content_parts = []
                    if text_parts:
                        content_parts.append({"type": "text", "text": "\n".join(text_parts)})
                    content_parts.extend(image_parts)
                    messages.append({"role": "user", "content": content_parts})
                for tr in tool_results:
                    tr_content = tr.get("content")
                    if isinstance(tr_content, list):
                        tr_text = "\n".join(_block_text(b) for b in tr_content)
                    else:
                        tr_text = tr_content or ""
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": tr_text,
                    })

        elif role == "assistant":
            if isinstance(content, str):
                messages.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                            },
                        })
                    # thinking / redacted_thinking blocks are skipped
                assistant_msg = {"role": "assistant", "content": "\n".join(text_parts)}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

    out = {
        "model": payload.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }
    for key in ("max_tokens", "temperature", "top_p"):
        if key in payload and payload[key] is not None:
            out[key] = payload[key]

    tools = payload.get("tools")
    if tools:
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
            if isinstance(t, dict) and t.get("name")
        ]

    tc = payload.get("tool_choice")
    if tc:
        if isinstance(tc, dict):
            ttype = tc.get("type")
            if ttype == "auto":
                out["tool_choice"] = "auto"
            elif ttype == "any":
                out["tool_choice"] = "required"
            elif ttype == "none":
                out["tool_choice"] = "none"
            elif ttype == "tool":
                out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        elif isinstance(tc, str):
            out["tool_choice"] = tc

    return out


# ── OpenAI -> Anthropic response translation (non-streaming) ───────────────

def translate_response(openai_payload, model):
    choices = openai_payload.get("choices", [{}])
    choice = choices[0] if choices else {}
    message = choice.get("message", {}) or {}
    content = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except Exception:
            arguments = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": arguments,
        })
    finish = choice.get("finish_reason")
    usage = openai_payload.get("usage", {}) or {}
    return {
        "id": "msg_" + str(openai_payload.get("id", "unknown"))[:20],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _STOP_REASON.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ── Streaming SSE translation ──────────────────────────────────────────────

def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def translate_stream(lines, model, request_id, input_tokens):
    """Yield Anthropic SSE strings from OpenAI chat.completion.chunk lines."""
    sent_start = False
    text_block_index = None
    tool_blocks = {}  # openai tc index -> {"block_index", "id", "name", "started"}
    next_block_index = 0

    for raw in lines:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta", {}) or {}
        finish = choice.get("finish_reason")

        if not sent_start:
            yield _sse("message_start", {
                "type": "message_start",
                "message": {
                    "id": "msg_" + str(request_id)[:20],
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0},
                },
            })
            sent_start = True

        content = delta.get("content")
        if content:
            if text_block_index is None:
                text_block_index = next_block_index
                next_block_index += 1
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": content},
            })

        for tc in delta.get("tool_calls") or []:
            tc_index = tc.get("index", 0)
            fn = tc.get("function", {}) or {}
            block = tool_blocks.get(tc_index)
            if block is None:
                block = {
                    "block_index": next_block_index,
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "started": False,
                }
                tool_blocks[tc_index] = block
                next_block_index += 1
            if not block["started"]:
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": block["block_index"],
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                })
                block["started"] = True
            args = fn.get("arguments")
            if args:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block["block_index"],
                    "delta": {"type": "input_json_delta", "partial_json": args},
                })

        if finish:
            open_blocks = []
            if text_block_index is not None:
                open_blocks.append(text_block_index)
            for block in sorted(tool_blocks.values(), key=lambda b: b["block_index"]):
                if block["started"]:
                    open_blocks.append(block["block_index"])
            for idx in open_blocks:
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            yield _sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": _STOP_REASON.get(finish, "end_turn"), "stop_sequence": None},
                "usage": {"output_tokens": 0},
            })
            yield _sse("message_stop", {"type": "message_stop"})
            return


# ── HTTP Handler ────────────────────────────────────────────────────────────

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

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b"{}"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            try:
                base_url, _ = _cpa_config()
                ok = True
            except Exception as exc:
                base_url, ok = str(exc), False
            self._send(200, _json_bytes({"ok": ok, "base_url": base_url, "model": DEFAULT_MODEL}))
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
                payload = json.loads(self._read_body().decode("utf-8"))
                self._send(200, _json_bytes({"input_tokens": _estimate_tokens(payload)}))
            except Exception as exc:
                self._send(400, _json_bytes({"error": {"type": "bad_request", "message": str(exc)}}))
            return
        if path == "/v1/messages":
            self._proxy_messages()
            return
        self._send(404, _json_bytes({"error": {"type": "not_found", "message": "not found"}}))

    def _proxy_messages(self):
        try:
            body = self._read_body()
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            self._send(400, _json_bytes({"error": {"type": "bad_request", "message": str(exc)}}))
            return

        model = payload.get("model") or DEFAULT_MODEL
        input_tokens = _estimate_tokens(payload)
        stream = bool(payload.get("stream", False))

        try:
            base_url, api_key = _cpa_config()
        except Exception as exc:
            _log(f"cpa_config_error: {exc}")
            self._send(502, _json_bytes({"error": {"type": "upstream_error", "message": f"CPA config failed: {exc}"}}))
            return

        openai_payload = translate_request(payload)
        target = base_url + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "Accept": "text/event-stream" if stream else "application/json",
        }
        req = Request(target, data=_json_bytes(openai_payload), headers=headers, method="POST")

        try:
            with urlopen(req, timeout=600) as resp:
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    request_id = str(resp.headers.get("x-request-id", "")) or str(time.time())
                    for event in translate_stream(resp, model, request_id, input_tokens):
                        self.wfile.write(event.encode("utf-8"))
                        self.wfile.flush()
                    return
                resp_body = resp.read()
                try:
                    openai_resp = json.loads(resp_body.decode("utf-8"))
                    anthropic_resp = translate_response(openai_resp, model)
                    self._send(200, _json_bytes(anthropic_resp))
                except Exception as exc:
                    _log(f"response_translate_error: {exc} body={resp_body[:200]}")
                    self._send(502, _json_bytes({"error": {"type": "upstream_error", "message": str(exc)}}))
                return
        except HTTPError as exc:
            err_body = exc.read()
            _log(f"upstream_http_error status={exc.code} body={err_body[:200]}")
            self._send(exc.code, err_body, exc.headers.get("content-type", "application/json"))
        except URLError as exc:
            _log(f"upstream_urlerror: {exc}")
            self._send(502, _json_bytes({"error": {"type": "upstream_error", "message": str(exc)}}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11436)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"cpa anthropic shim listening on http://{args.host}:{args.port} (model={DEFAULT_MODEL})")
    server.serve_forever()


if __name__ == "__main__":
    main()
