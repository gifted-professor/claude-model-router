#!/usr/bin/env python3
"""Run one bounded, parallel external review batch through remote-cpa."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any


ROLE_CANDIDATES = {
    "coverage": ["kimi-k3-256k", "kimi-k3", "kimi-k2.7-code", "glm-5.2"],
    "adversarial": ["grok-4.6", "grok-4.5", "grok-4.3", "glm-5.2", "deepseek-v4-flash"],
    "implementation": ["glm-5.2", "deepseek-v4-flash", "kimi-k2.7-code", "kimi-k3"],
    "safety": ["grok-4.6", "glm-5.2", "deepseek-v4-flash", "kimi-k3-256k", "kimi-k3"],
}

SENSITIVE_NAMES = {
    ".env",
    "auth.json",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "remote-cpa.local.json",
}

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_PACKET_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_TOKENS = 8192
DEFAULT_RECOVERY_MAX_TOKENS = 12288
THINKING_OUTPUT_FLOOR = 8192
DEFAULT_REVIEW_TIMEOUT = 180.0
TRUNCATED_FINISH_REASONS = {"length", "max_tokens"}
NO_RETRY_FAILURES = {"authentication_error", "content_filtered", "context_length_exceeded"}
FALLBACK_FAILURES = {"rate_limited", "upstream_error", "timeout_error", "model_unavailable"}
UNSUPPORTED_REQUEST_MARKERS = (
    "unknown parameter",
    "unexpected field",
    "unrecognized request",
    "invalid parameter",
    "does not support",
    "unsupported",
    "response_format",
    "reasoning_effort",
)
REASONING_EFFORT_PREFIXES = ("kimi-k3",)


class ReportValidationError(ValueError):
    """A locally classified final-answer or report-schema failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def load_remote_cpa() -> Any:
    path = Path.home() / ".codex" / "skills" / "remote-cpa" / "scripts" / "cpa_request.py"
    if not path.is_file():
        raise RuntimeError(f"remote-cpa helper not found: {path}")
    spec = importlib.util.spec_from_file_location("remote_cpa_request", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load remote-cpa helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_read(path_text: str) -> tuple[str, str]:
    path = Path(path_text).expanduser().resolve()
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_NAMES or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}:
        raise ValueError(f"refusing sensitive input path: {path.name}")
    if not path.is_file():
        raise ValueError(f"input file not found: {path}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"input exceeds {MAX_FILE_BYTES} bytes: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"input is not UTF-8 text: {path}") from exc
    return str(path), content


def build_packet(artifact: str, contexts: list[str]) -> tuple[str, str]:
    items = [safe_read(artifact)] + [safe_read(item) for item in contexts]
    packet = "\n\n".join(f"===== {path} =====\n{content}" for path, content in items)
    encoded = packet.encode("utf-8")
    if len(encoded) > MAX_PACKET_BYTES:
        raise ValueError(f"review packet exceeds {MAX_PACKET_BYTES} bytes")
    return packet, hashlib.sha256(encoded).hexdigest()


def system_prompt(mode: str, role: str, *, recovery: bool = False) -> str:
    focus = {
        "coverage": "Find requirement gaps, cross-file inconsistencies, missing dependencies, and unverifiable assumptions.",
        "adversarial": "Challenge key assumptions with concrete counterexamples, failure paths, and simpler viable alternatives.",
        "implementation": "Check implementation feasibility, code-path correctness, plan compliance, tests, and operational failure modes.",
        "safety": "Check authorization, data integrity, rollback, recovery, abuse paths, and irreversible effects.",
    }[role]
    subject = "implementation" if mode == "execution" else "plan"
    if recovery:
        return (
            f"Review the same frozen {subject} packet as the {role} critic. {focus} "
            "Return one compact JSON object only; no markdown and no visible reasoning. Include at most five "
            "evidence-backed findings. Required keys: verdict, findings, missing_evidence, confidence. Each finding "
            "must contain id, severity, claim, evidence, impact, minimal_change. Use verdict sound, needs_changes, "
            "or blocked; severity P0, P1, P2, or P3; confidence high, medium, or low."
        )
    return (
        f"You are the {role} critic for a frozen {subject} review packet. {focus} "
        "Be evidence-first. Return at most five high-confidence findings. P0 means catastrophic or irreversible; "
        "P1 means delivery-blocking correctness or safety; P2/P3 are non-blocking. Do not propose another review. "
        "Return one JSON object only with keys verdict, findings, missing_evidence, confidence; do not put reasoning "
        "in the final answer. Each finding must contain id, severity, claim, evidence (array), impact, minimal_change. "
        "Use verdict sound, needs_changes, or blocked; severity P0, P1, P2, or P3; confidence high, medium, or low."
    )


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def normalize_verdict(value: Any, findings: list[Any], missing_evidence: list[Any]) -> str:
    key = _norm_key(value)
    if key in {"sound", "pass", "ok", "lgtm", "approve", "approved"}:
        return "sound"
    if key in {"blocked", "block", "cannot_review"} or key.startswith("blocked_") or "insufficient" in key:
        return "blocked"
    if any(token in key for token in ("change", "revise", "request", "conditional", "needs")):
        return "needs_changes"
    if findings:
        return "needs_changes"
    if missing_evidence:
        return "blocked"
    raise ReportValidationError("schema_invalid", "verdict must be sound, needs_changes, or blocked")


def normalize_confidence(value: Any) -> str:
    if value is None:
        return "medium"
    if isinstance(value, bool):
        raise ReportValidationError("schema_invalid", "confidence must be high, medium, or low")
    if isinstance(value, (int, float)):
        score = float(value)
        if score > 1.0:
            score = score / 100.0 if score <= 100.0 else 1.0
        if score >= 0.8:
            return "high"
        if score >= 0.5:
            return "medium"
        return "low"
    key = str(value).strip().lower()
    if key in {"high", "medium", "low"}:
        return key
    try:
        return normalize_confidence(float(key))
    except ValueError as exc:
        raise ReportValidationError("schema_invalid", "confidence must be high, medium, or low") from exc


def normalize_severity(value: Any, index: int) -> str:
    key = _norm_key(value).upper()
    if key in {"P0", "P1", "P2", "P3"}:
        return key
    raise ReportValidationError("schema_invalid", f"finding {index} has an invalid severity")


def normalize_string_list(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        raise ReportValidationError("schema_invalid", f"{field} must be an array")
    items: list[str] = []
    for item in value:
        if isinstance(item, str):
            items.append(item)
        elif isinstance(item, (int, float, bool)):
            items.append(str(item))
        else:
            raise ReportValidationError("schema_invalid", f"{field} entries must be strings")
    return items


def extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if not candidate:
        raise ReportValidationError("empty_final_content", "response final content is empty")
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1]
        if candidate.endswith("```"):
            candidate = candidate[:-3].rstrip()
    decoder = json.JSONDecoder()
    start = candidate.find("{")
    while start >= 0:
        try:
            value, _end = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            start = candidate.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = candidate.find("{", start + 1)
    raise ReportValidationError("invalid_json", "response does not contain a JSON object")


def parse_report(text: str) -> dict[str, Any]:
    value = extract_json_object(text)
    findings = value.get("findings", [])
    if isinstance(findings, dict):
        findings = [findings]
    if not isinstance(findings, list):
        raise ReportValidationError("schema_invalid", "findings must be an array")
    missing_evidence = normalize_string_list(value.get("missing_evidence"), field="missing_evidence")
    if "verdict" not in value:
        raise ReportValidationError("schema_invalid", "response is missing required keys: verdict")
    verdict = normalize_verdict(value["verdict"], findings, missing_evidence)
    confidence = normalize_confidence(value.get("confidence"))

    finding_keys = ("id", "severity", "claim", "evidence", "impact", "minimal_change")
    sanitized_findings: list[dict[str, Any]] = []
    for index, finding in enumerate(findings[:5]):
        if not isinstance(finding, dict):
            raise ReportValidationError("schema_invalid", f"finding {index} must be an object")
        missing_finding = sorted(set(finding_keys).difference(finding))
        if missing_finding:
            raise ReportValidationError(
                "schema_invalid",
                f"finding {index} is missing required keys: {', '.join(missing_finding)}",
            )
        evidence = normalize_string_list(finding["evidence"], field=f"finding {index} evidence")
        record: dict[str, Any] = {"severity": normalize_severity(finding["severity"], index), "evidence": evidence}
        for field in ("id", "claim", "impact", "minimal_change"):
            item = finding[field]
            if isinstance(item, (int, float, bool)):
                item = str(item)
            if not isinstance(item, str):
                raise ReportValidationError("schema_invalid", f"finding {index} {field} must be a string")
            record[field] = item
        sanitized_findings.append({field: record[field] for field in finding_keys})
    return {
        "verdict": verdict,
        "findings": sanitized_findings,
        "missing_evidence": missing_evidence,
        "confidence": confidence,
    }


def extract_text_content(value: Any) -> str:
    """Normalize OpenAI-compatible text content without using reasoning content."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {None, "text", "output_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif "text" not in item and any(key in item for key in ("verdict", "findings")):
                    parts.append(json.dumps(item, ensure_ascii=False))
        return "".join(parts)
    return ""


def report_source_text(message: dict[str, Any], content: str) -> str:
    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(parsed, str) and parsed.strip():
        return parsed
    return content


def reasoning_blob(message: dict[str, Any]) -> Any:
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if value:
            return value
    return None


def safe_usage(value: Any) -> dict[str, Any]:
    """Keep token counters and scalar metadata, never generated text."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (int, float, bool)) or item is None:
            result[str(key)] = item
        elif isinstance(item, dict):
            nested = safe_usage(item)
            if nested:
                result[str(key)] = nested
    return result


def response_diagnostics(response: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    response_keys = sorted(str(key) for key in response) if isinstance(response, dict) else []
    choices = response.get("choices", []) if isinstance(response, dict) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    content = extract_text_content(message.get("content"))
    source = report_source_text(message, content)
    reasoning = reasoning_blob(message)
    if isinstance(reasoning, str):
        reasoning_length = len(reasoning)
    elif reasoning is None:
        reasoning_length = 0
    else:
        reasoning_length = len(json.dumps(reasoning, ensure_ascii=False, default=str))
    diagnostics = {
        "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        "usage": safe_usage(response.get("usage")) if isinstance(response, dict) else {},
        "response_keys": response_keys,
        "choices_count": len(choices) if isinstance(choices, list) else 0,
        "message_keys": sorted(str(key) for key in message),
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else None,
        "reasoning_present": reasoning_length > 0,
        "reasoning_content_length": reasoning_length,
        "parsed_present": isinstance(message.get("parsed"), (dict, str)),
    }
    return source, diagnostics, message


def classify_request_error(exc: Exception) -> tuple[str, int | None]:
    message = str(exc).lower()
    status_match = re.search(r"http\s+(\d{3})\b", message)
    status = int(status_match.group(1)) if status_match else None
    if status in {401, 403}:
        return "authentication_error", status
    if "content_filter" in message or "content filter" in message:
        return "content_filtered", status
    if status == 429:
        return "rate_limited", status
    if status == 404:
        return "model_unavailable", status
    if status is not None and 500 <= status <= 599:
        return "upstream_error", status
    if status == 400 and any(marker in message for marker in UNSUPPORTED_REQUEST_MARKERS):
        return "unsupported_request", status
    if any(marker in message for marker in ("context_length", "maximum context", "context window")):
        return "context_length_exceeded", status
    if any(marker in message for marker in ("model unavailable", "model not found", "no available channel")):
        return "model_unavailable", status
    if isinstance(exc, TimeoutError) or any(marker in message for marker in ("timed out", "timeout")):
        return "timeout_error", status
    if isinstance(exc, ConnectionError) or "connection failed" in message:
        return "transport_error", status
    return "request_error", status


def model_family(model: str) -> str:
    lowered = model.lower()
    for family in ("kimi", "grok", "glm", "deepseek", "qwen", "minimax", "mimo"):
        if family in lowered:
            return family
    return lowered.split("-", 1)[0]


def is_thinking_model(model: str) -> bool:
    lowered = model.lower()
    if "non-reasoning" in lowered or "non_reasoning" in lowered:
        return False
    if "thinking" in lowered or "kimi-k2.7-code" in lowered or "deepseek-v4" in lowered:
        return True
    if "kimi-k3" in lowered:
        return True
    return "grok-4" in lowered


def uses_reasoning_effort(model: str) -> bool:
    lowered = model.lower()
    if "non-reasoning" in lowered or "non_reasoning" in lowered:
        return False
    return any(prefix in lowered for prefix in REASONING_EFFORT_PREFIXES)


def effective_max_tokens(model: str, requested: int) -> int:
    if is_thinking_model(model):
        return max(requested, THINKING_OUTPUT_FLOOR)
    return requested


def request_extras(model: str) -> dict[str, Any]:
    extras: dict[str, Any] = {"response_format": {"type": "json_object"}}
    if uses_reasoning_effort(model):
        extras["reasoning_effort"] = "low"
    return extras


def build_chat_payload(
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    *,
    extras: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": effective_max_tokens(model, max_tokens),
    }
    if extras:
        payload.update(request_extras(model))
    return payload


def resolve_models(live_ids: set[str], roles: list[str], overrides: dict[str, str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    used: set[str] = set()
    for role in roles:
        candidates = [overrides[role]] if role in overrides else ROLE_CANDIDATES[role]
        model = next((item for item in candidates if item in live_ids and item not in used), None)
        if model is None:
            model = next((item for item in candidates if item in live_ids), None)
        if model is None:
            raise RuntimeError(f"no live model available for role {role}: {', '.join(candidates)}")
        selected[role] = model
        used.add(model)
    return selected


def resolve_fallback_models(
    live_ids: set[str],
    roles: list[str],
    selected: dict[str, str],
    allow_fallback: bool,
) -> dict[str, str | None]:
    if not allow_fallback:
        return {role: None for role in roles}
    primary_models = set(selected.values())
    result: dict[str, str | None] = {}
    for role in roles:
        candidates = [
            candidate
            for candidate in ROLE_CANDIDATES[role]
            if candidate in live_ids and candidate not in primary_models
        ]
        primary_family = model_family(selected[role])
        result[role] = next(
            (candidate for candidate in candidates if model_family(candidate) != primary_family),
            candidates[0] if candidates else None,
        )
    return result


def recovery_output_tokens(
    failure_kind: str,
    attempt_record: dict[str, Any],
    model: str,
    max_tokens: int,
    retry_max_tokens: int,
) -> int:
    tokens = max(max_tokens, retry_max_tokens)
    if failure_kind in {"output_truncated", "empty_final_content"}:
        previous = int(attempt_record.get("max_tokens") or max_tokens)
        tokens = max(tokens, previous * 2)
    return effective_max_tokens(model, tokens)


def recovery_plan(
    failure_kind: str,
    attempt_record: dict[str, Any],
    primary_model: str,
    fallback_model: str | None,
    max_tokens: int,
    retry_max_tokens: int,
) -> tuple[str, int, str, bool] | None:
    if failure_kind in NO_RETRY_FAILURES:
        return None
    if failure_kind == "unsupported_request":
        return (
            primary_model,
            effective_max_tokens(primary_model, max_tokens),
            "same_model_strip_request_extras",
            False,
        )
    if failure_kind in {"invalid_json", "schema_invalid"}:
        if fallback_model:
            return (
                fallback_model,
                recovery_output_tokens(failure_kind, attempt_record, fallback_model, max_tokens, retry_max_tokens),
                "fallback_after_format_failure",
                True,
            )
        return primary_model, max_tokens, "same_model_format_repair", True
    if failure_kind == "output_truncated" or (
        failure_kind == "empty_final_content" and attempt_record.get("reasoning_present")
    ):
        return (
            primary_model,
            recovery_output_tokens(failure_kind, attempt_record, primary_model, max_tokens, retry_max_tokens),
            "same_model_output_recovery",
            True,
        )
    if failure_kind == "empty_final_content" and fallback_model:
        return (
            fallback_model,
            recovery_output_tokens(failure_kind, attempt_record, fallback_model, max_tokens, retry_max_tokens),
            "fallback_after_empty_final",
            True,
        )
    if failure_kind in FALLBACK_FAILURES and fallback_model:
        return fallback_model, max_tokens, "fallback_after_upstream_failure", True
    return primary_model, max_tokens, "same_model_request_retry", True


def parse_overrides(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"model override must be ROLE=MODEL: {value}")
        role, model = (part.strip() for part in value.split("=", 1))
        if role not in ROLE_CANDIDATES or not model:
            raise ValueError(f"invalid model override: {value}")
        result[role] = model
    return result


def run_one(
    module: Any,
    config: tuple[str, str, float],
    mode: str,
    role: str,
    model: str,
    fallback_model: str | None,
    packet: str,
    max_tokens: int,
    retry_max_tokens: int,
    retry_limit: int,
) -> dict[str, Any]:
    base_url, api_key, timeout = config
    error = ""
    failure_kind = "request_error"
    attempt_log: list[dict[str, Any]] = []
    attempted_models: list[str] = []
    next_model = model
    next_tokens = max_tokens
    recovery_action = "primary"
    use_extras = True
    for attempt in range(1, retry_limit + 2):
        recovery = attempt > 1
        attempt_model = next_model
        attempt_tokens = next_tokens
        attempted_models.append(attempt_model)
        user_message = packet
        if recovery:
            user_message += "\n\nRECOVERY: Return the required compact JSON object only. Do not include analysis or markdown."
        payload = build_chat_payload(
            attempt_model,
            [
                {"role": "system", "content": system_prompt(mode, role, recovery=recovery)},
                {"role": "user", "content": user_message},
            ],
            attempt_tokens,
            extras=use_extras,
        )
        record: dict[str, Any] = {
            "attempt": attempt,
            "model": attempt_model,
            "max_tokens": payload["max_tokens"],
            "recovery": recovery,
            "action": recovery_action,
            "request_option_keys": sorted(str(key) for key in payload if key not in {"model", "messages", "max_tokens"}),
            "json_mode": isinstance(payload.get("response_format"), dict)
            and payload["response_format"].get("type") == "json_object",
            "reasoning_effort": payload.get("reasoning_effort"),
        }
        started = time.monotonic()
        try:
            response = module.request_json(
                base_url,
                api_key,
                timeout,
                "POST",
                "/chat/completions",
                payload,
            )
            raw, diagnostics, _message = response_diagnostics(response)
            record.update(diagnostics)
            try:
                parsed = parse_report(raw)
            except ReportValidationError as exc:
                if diagnostics["finish_reason"] in TRUNCATED_FINISH_REASONS:
                    raise ReportValidationError(
                        "output_truncated",
                        f"response stopped with finish_reason={diagnostics['finish_reason']}",
                    ) from exc
                if diagnostics["finish_reason"] == "content_filter":
                    raise ReportValidationError("content_filtered", "response was stopped by content filtering") from exc
                raise
            record.update(
                {
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "ok": True,
                    "failure_kind": None,
                    "error_code": None,
                }
            )
            attempt_log.append(record)
            return {
                "role": role,
                "primary_model": model,
                "model": attempt_model,
                "ok": True,
                "attempts": attempt,
                "models_attempted": attempted_models,
                "fallback_used": attempt_model != model,
                "recovered": attempt > 1,
                "report": parsed,
                "attempt_log": attempt_log,
                "error": None,
            }
        except ReportValidationError as exc:
            failure_kind = exc.kind
            error = failure_kind
        except Exception as exc:  # The envelope records transport, upstream, and local failures.
            failure_kind, http_status = classify_request_error(exc)
            if http_status is not None:
                record["http_status"] = http_status
            error = failure_kind
        record.update(
            {
                "latency_ms": round((time.monotonic() - started) * 1000),
                "ok": False,
                "failure_kind": failure_kind,
                "error_code": error,
            }
        )
        recovery = recovery_plan(
            failure_kind,
            record,
            model,
            fallback_model,
            max_tokens,
            retry_max_tokens,
        )
        if attempt > retry_limit:
            record["retry_action"] = "stop_budget_exhausted"
            attempt_log.append(record)
            break
        if recovery is None:
            record["retry_action"] = "stop_non_retryable"
            attempt_log.append(record)
            break
        next_model, next_tokens, recovery_action, keep_extras = recovery
        use_extras = keep_extras and use_extras
        if recovery_action == "same_model_strip_request_extras":
            use_extras = False
        record["retry_action"] = recovery_action
        attempt_log.append(record)
    return {
        "role": role,
        "primary_model": model,
        "model": attempted_models[-1] if attempted_models else model,
        "ok": False,
        "attempts": len(attempt_log),
        "models_attempted": attempted_models,
        "fallback_used": any(item != model for item in attempted_models),
        "recovered": False,
        "report": None,
        "failure_kind": failure_kind,
        "attempt_log": attempt_log,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "execution"), required=True)
    parser.add_argument("--artifact", required=True, help="explicit UTF-8 plan or review artifact")
    parser.add_argument("--context", action="append", default=[], help="additional explicit UTF-8 file; repeat as needed")
    parser.add_argument("--role", action="append", choices=tuple(ROLE_CANDIDATES), required=True)
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="optional ROLE=exact-live primary model; bounded recovery may use a same-role fallback",
    )
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--retry-max-tokens",
        type=int,
        default=DEFAULT_RECOVERY_MAX_TOKENS,
        help="output allowance for the sole format/output recovery call",
    )
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--retry-limit", type=int, choices=(0, 1), default=1)
    parser.add_argument("--no-model-fallback", action="store_true", help="retry the primary model only")
    parser.add_argument("--output-dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    roles = list(dict.fromkeys(args.role))
    if len(roles) > 2:
        parser.error("one review wave allows at most two roles")
    if args.max_tokens < 256:
        parser.error("--max-tokens must be at least 256")
    if args.retry_max_tokens < 256:
        parser.error("--retry-max-tokens must be at least 256")
    try:
        overrides = parse_overrides(args.model)
        packet, fingerprint = build_packet(args.artifact, args.context)
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run:
        envelope = {
            "schema": "multi-model-review-batch.v2",
            "dry_run": True,
            "mode": args.mode,
            "snapshot_sha256": fingerprint,
            "roles": roles,
            "candidate_models": {role: [overrides[role]] if role in overrides else ROLE_CANDIDATES[role] for role in roles},
            "max_parallel": min(2, len(roles)),
            "retry_limit": args.retry_limit,
            "max_calls_per_role": args.retry_limit + 1,
            "retry_max_tokens": max(args.max_tokens, args.retry_max_tokens),
            "thinking_output_floor": THINKING_OUTPUT_FLOOR,
            "json_mode": True,
            "model_fallback": not args.no_model_fallback,
        }
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else Path(tempfile.gettempdir()) / "multi-model-review-gate" / fingerprint[:16]
    output_path = output_dir / f"review-batch-{args.mode}.json"
    if output_path.is_file():
        try:
            previous = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if (
            isinstance(previous, dict)
            and previous.get("snapshot_sha256") == fingerprint
            and previous.get("mode") == args.mode
        ):
            print(
                json.dumps(
                    {
                        "schema": "multi-model-review-batch.v2",
                        "status": "ALREADY_REVIEWED_STOP",
                        "mode": args.mode,
                        "snapshot_sha256": fingerprint,
                        "output": str(output_path),
                        "instruction": "Do not start another full review wave; adjudicate the saved batch or fall back to GPT once.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

    module = load_remote_cpa()
    base_url, api_key, configured_timeout = module.client_config()
    timeout = args.timeout if args.timeout is not None else max(configured_timeout, DEFAULT_REVIEW_TIMEOUT)
    models_response = module.request_json(base_url, api_key, timeout, "GET", "/models")
    model_items = models_response.get("data", []) if isinstance(models_response, dict) else []
    live_ids = {str(item.get("id")) for item in model_items if isinstance(item, dict) and item.get("id")}
    selected = resolve_models(live_ids, roles, overrides)
    fallbacks = resolve_fallback_models(live_ids, roles, selected, not args.no_model_fallback)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(roles))) as pool:
        futures = {
            role: pool.submit(
                run_one,
                module,
                (base_url, api_key, timeout),
                args.mode,
                role,
                selected[role],
                fallbacks[role],
                packet,
                args.max_tokens,
                args.retry_max_tokens,
                args.retry_limit,
            )
            for role in roles
        }
        reports = [futures[role].result() for role in roles]

    output_dir.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema": "multi-model-review-batch.v2",
        "dry_run": False,
        "mode": args.mode,
        "snapshot_sha256": fingerprint,
        "review_wave": 1,
        "max_calls_per_role": args.retry_limit + 1,
        "selected_models": selected,
        "fallback_models": fallbacks,
        "reports": reports,
        "degraded": not all(item["ok"] for item in reports),
        "recovered": any(item["recovered"] for item in reports),
        "recovered_with_fallback": any(item["fallback_used"] and item["ok"] for item in reports),
        "gpt_fallback_required": not any(item["ok"] for item in reports),
    }
    output_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), **envelope}, ensure_ascii=False, indent=2))
    return 0 if any(item["ok"] for item in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
