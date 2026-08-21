#!/usr/bin/env python3
"""Recommend, but never launch, an executor for an approved final plan."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


READY = "READY_FOR_EXECUTION"
PROFILES = {
    "general",
    "high-volume",
    "long-horizon",
    "agentic",
    "terminal-heavy",
    "vision",
    "context-overflow",
    "security",
}
DISPLAY_FAMILY = {
    "deepseek_flash": "deepseek-v4-flash",
    "deepseek_pro": "deepseek-v4-pro",
    "glm": "glm-5.x",
    "glm_long": "glm-5.x[1m]",
    "kimi": "kimi-k3",
    "kimi_long": "kimi-k3-long-context",
    "gemini_vision": "gemini-flash-vision",
}
MAX_PLAN_BYTES = 4 * 1024 * 1024


def plan_sha256(path_text: str) -> str:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"plan not found: {path}")
    if path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError(f"plan exceeds {MAX_PLAN_BYTES} bytes: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_inventory(value: Any) -> set[str]:
    items = value if isinstance(value, list) else value.get("data", []) if isinstance(value, dict) else []
    result: set[str] = set()
    for item in items:
        if isinstance(item, str) and item:
            result.add(item)
        elif isinstance(item, dict) and item.get("id"):
            result.add(str(item["id"]))
    return result


def read_inventory(path_text: str) -> set[str]:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"inventory not found: {path}")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"inventory is too large: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"inventory is not valid UTF-8 JSON: {path}") from exc
    return parse_inventory(value)


def parse_available_models(values: list[str]) -> set[str]:
    return {
        item.strip()
        for value in values
        for item in value.split(",")
        if item.strip()
    }


def load_remote_cpa() -> Any:
    path = Path.home() / ".codex" / "skills" / "remote-cpa" / "scripts" / "cpa_request.py"
    if not path.is_file():
        raise RuntimeError(f"remote-cpa helper not found: {path}")
    spec = importlib.util.spec_from_file_location("executor_route_remote_cpa", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load remote-cpa helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_cpa_inventory() -> set[str]:
    module = load_remote_cpa()
    base_url, api_key, timeout = module.client_config()
    response = module.request_json(base_url, api_key, timeout, "GET", "/models")
    return parse_inventory(response)


def version_tuple(model: str, marker: str) -> tuple[int, ...]:
    match = re.search(marker + r"(\d+(?:\.\d+)*)", model.lower())
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def family_matches(family: str, model: str) -> bool:
    name = model.lower()
    if family == "deepseek_flash":
        return "deepseek" in name and "v4" in name and "flash" in name
    if family == "deepseek_pro":
        return "deepseek" in name and "v4" in name and "pro" in name
    if family == "glm":
        return "glm-5" in name
    if family == "glm_long":
        return "glm-5" in name and "[1m]" in name
    if family == "kimi":
        return "kimi-k3" in name
    if family == "kimi_long":
        return "kimi-k3" in name and "256k" in name
    if family == "gemini_vision":
        return "gemini" in name and "image" not in name and ("flash" in name or "pro" in name)
    return False


def family_score(family: str, model: str) -> tuple[Any, ...]:
    name = model.lower()
    canonical = int(name.startswith(("deepseek-", "glm-", "kimi-", "gemini-")))
    rolling_alias = int(name in {"deepseek-v4-flash", "deepseek-v4-pro", "kimi-k3"})
    if family in {"glm", "glm_long"}:
        version = version_tuple(name, r"glm-")
    elif family in {"kimi", "kimi_long"}:
        version = version_tuple(name, r"kimi-k")
    elif family == "gemini_vision":
        version = version_tuple(name, r"gemini-")
    else:
        version = version_tuple(name, r"deepseek-v")
    normal_context = int("256k" not in name) if family == "kimi" else 1
    long_context_alias = int("[1m]" in name) if family in {"glm", "glm_long"} else 0
    return version, long_context_alias, rolling_alias, canonical, normal_context, name


def resolve_family(family: str, live_ids: set[str], exclude: set[str] | None = None) -> str | None:
    excluded = exclude or set()
    candidates = [item for item in live_ids if item not in excluded and family_matches(family, item)]
    return max(candidates, key=lambda item: family_score(family, item), default=None)


def resolve_groups(groups: list[str], live_ids: set[str], exclude: set[str] | None = None) -> tuple[str, str | None]:
    for family in groups:
        resolved = resolve_family(family, live_ids, exclude)
        if resolved:
            return family, resolved
    return groups[0], None


def normalize_profiles(values: list[str]) -> set[str]:
    profiles: set[str] = set()
    for value in values:
        profiles.update(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(profiles - PROFILES)
    if unknown:
        raise ValueError(f"unknown profiles: {', '.join(unknown)}")
    return profiles or {"general"}


def family_route(
    risk_tier: str,
    profiles: set[str],
    cost_priority: str,
    tests: str,
) -> tuple[str, list[str], list[str], list[str], bool]:
    if "vision" in profiles and "context-overflow" in profiles:
        capabilities = ["vision", "verified-context-capacity", "repository-tools"]
        if "security" in profiles:
            capabilities.append("security-sensitive-execution")
        return "VISION_LONG_CONTEXT", ["kimi_long"], [], capabilities, True
    if "vision" in profiles:
        return "VISION", ["kimi"], ["gemini_vision"], ["vision", "repository-tools"], True
    if "context-overflow" in profiles:
        capabilities = ["verified-context-capacity", "repository-tools"]
        if "security" in profiles:
            capabilities.append("security-sensitive-execution")
        return "LONG_CONTEXT", ["kimi_long"], [], capabilities, True
    if "security" in profiles:
        return "SECURITY", ["glm", "deepseek_pro"], ["deepseek_pro"], ["repository-tools"], False
    if "agentic" in profiles:
        return "HARD_AGENT", ["glm", "deepseek_pro"], ["kimi"], ["reliable-tool-calling"], False
    hard = risk_tier in {"HIGH", "CRITICAL"} or "long-horizon" in profiles
    if hard:
        primary = ["deepseek_pro", "glm"] if cost_priority == "economy" or "terminal-heavy" in profiles else ["glm", "deepseek_pro"]
        return "HARD_CODE", primary, [] if risk_tier == "CRITICAL" else ["kimi"], ["repository-tools"], False
    if "high-volume" in profiles:
        return "HIGH_VOLUME", ["deepseek_flash"], ["glm", "deepseek_pro"], ["repository-tools"], False
    if cost_priority == "reliability" or tests in {"weak", "none"}:
        return "STRONG_DEFAULT", ["deepseek_pro", "glm"], ["glm", "kimi"], ["repository-tools"], False
    return "DEFAULT", ["deepseek_flash"], ["glm", "deepseek_pro"], ["repository-tools"], False


def model_record(family: str, exact_id: str | None) -> dict[str, Any]:
    return {
        "family": DISPLAY_FAMILY[family],
        "resolved_exact_id": exact_id,
        "model_id_availability": "VERIFIED_IN_INVENTORY" if exact_id else "UNRESOLVED",
    }


def claude_cli_preflight(
    args: argparse.Namespace,
    profiles: set[str],
    live_ids: set[str],
) -> dict[str, Any] | None:
    if "claude" not in args.runtime.lower():
        return None
    exact_id = resolve_family("glm_long", live_ids)
    hard = args.risk_tier in {"HIGH", "CRITICAL"} or bool(
        profiles & {"agentic", "long-horizon", "security", "context-overflow"}
    )
    effort = "xHigh" if hard or args.tests in {"weak", "none"} else "high"
    return {
        "mode": "CLAUDE_CLI_PLAN_MODE",
        "purpose": "Compile approved Plan V2 into a runtime-specific execution manifest.",
        "model": model_record("glm_long", exact_id),
        "effort": effort,
        "required": True,
        "may_modify_approved_plan": False,
        "may_create_plan_v3": False,
        "may_start_execution": False,
        "output_only": [
            "command_and_tool_order",
            "verification_checkpoints",
            "acceptance_commands",
            "runtime_capability_blockers",
        ],
    }


def build_recommendation(
    args: argparse.Namespace,
    live_ids: set[str],
    plan_hash: str,
    inventory_source: str,
) -> dict[str, Any]:
    profiles = normalize_profiles(args.profile)
    base = {
        "schema": "multi-model-execution-route.v1",
        "advisory_only": True,
        "execute_authorized": False,
        "plan_status": args.plan_status,
        "review_snapshot_sha256": plan_hash,
        "runtime": args.runtime,
        "inventory_source": inventory_source,
        "inventory_observed_at": (
            None
            if inventory_source == "none" or inventory_source.startswith("not-queried:")
            else datetime.now(timezone.utc).isoformat()
        ),
        "risk_tier": args.risk_tier,
        "profiles": sorted(profiles),
        "cost_priority": args.cost_priority,
        "execution_unit": "WHOLE_PLAN",
        "default_execution_units": 1,
        "max_macro_units": 3 if args.risk_tier == "CRITICAL" else 2 if args.risk_tier == "HIGH" else 1,
        "exhausted_verdict": "BLOCKED_EXECUTION_ROUTE",
    }
    if args.plan_status != READY:
        return {
            **base,
            "status": "WITHHELD",
            "profile": None,
            "primary": None,
            "fallback": None,
            "max_model_switches": 0,
            "reason": "Execution is withheld until the plan reaches READY_FOR_EXECUTION.",
        }

    profile, primary_groups, fallback_groups, capabilities, capability_check = family_route(
        args.risk_tier,
        profiles,
        args.cost_priority,
        args.tests,
    )
    preflight = claude_cli_preflight(args, profiles, live_ids)
    primary_family, primary_id = resolve_groups(primary_groups, live_ids)
    primary_promoted = False
    remaining_fallback_groups = list(fallback_groups)
    if not primary_id and fallback_groups and args.risk_tier != "CRITICAL":
        promoted_family, promoted_id = resolve_groups(fallback_groups, live_ids)
        if promoted_id:
            primary_family, primary_id = promoted_family, promoted_id
            primary_promoted = True
            remaining_fallback_groups = [family for family in fallback_groups if family != primary_family]
    fallback_family: str | None = None
    fallback_id: str | None = None
    if primary_id and remaining_fallback_groups and args.risk_tier != "CRITICAL":
        fallback_family, fallback_id = resolve_groups(
            remaining_fallback_groups,
            live_ids,
            {primary_id},
        )

    if not primary_id:
        status = "NO_VERIFIED_EXECUTOR"
    elif preflight is not None and preflight["model"]["resolved_exact_id"] is None:
        status = "NO_VERIFIED_PREFLIGHT"
    elif capability_check:
        status = "CAPABILITY_CHECK_REQUIRED"
    else:
        status = "READY"
    fallback = model_record(fallback_family, fallback_id) if fallback_family and fallback_id else None
    max_switches = 1 if fallback is not None and args.risk_tier != "CRITICAL" else 0
    return {
        **base,
        "status": status,
        "profile": profile,
        "required_capabilities": capabilities,
        "preflight": preflight,
        "primary": model_record(primary_family, primary_id),
        "primary_promoted_from_fallback": primary_promoted,
        "fallback": fallback,
        "unresolved_fallback": (
            model_record(fallback_family, None)
            if fallback_family is not None and fallback_id is None
            else None
        ),
        "max_model_switches": max_switches,
        "max_continuous_sessions_per_model": 1,
        "switch_only_on": [
            "CAPABILITY_MISMATCH",
            "CONTEXT_LIMIT",
            "PROVIDER_UNAVAILABLE",
            "BLOCKED_AFTER_ACCEPTANCE_CHECKS",
        ] if max_switches else [],
        "handoff_requires": [
            "frozen_current_state_or_diff",
            "completed_commands_and_acceptance_output",
            "external_side_effects",
            "evidenced_blocker",
        ] if max_switches else [],
        "tool_execution_capability": "REQUIRES_TARGET_RUNTIME_CHECK",
        "reason": "Exact IDs are resolved only from the named runtime inventory; the route never launches them.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="approved final plan used only for its SHA-256")
    parser.add_argument(
        "--plan-status",
        choices=(READY, "SCOPE_CHANGE_REQUIRED", "BLOCKED_NEEDS_DECISION"),
        required=True,
    )
    parser.add_argument("--risk-tier", choices=("LOW", "STANDARD", "HIGH", "CRITICAL"), required=True)
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--cost-priority", choices=("economy", "balanced", "reliability"), default="balanced")
    parser.add_argument("--tests", choices=("mature", "weak", "none"), default="mature")
    parser.add_argument("--runtime", required=True, help="actual executor runtime, for example codex-cpa or opencode")
    inventory = parser.add_mutually_exclusive_group()
    inventory.add_argument("--live-cpa", action="store_true", help="query CPA /models; valid only for a CPA runtime")
    inventory.add_argument("--inventory-json", help="target runtime's exported model inventory")
    inventory.add_argument("--available-model", action="append", default=[], help="verified exact runtime model ID; repeat")
    args = parser.parse_args()

    try:
        fingerprint = plan_sha256(args.plan)
        if args.plan_status != READY:
            live_ids = set()
            source = "not-queried:plan-not-ready"
        elif args.live_cpa:
            if args.runtime not in {"cpa", "codex-cpa"}:
                raise ValueError("--live-cpa can verify only the cpa or codex-cpa runtime")
            live_ids = live_cpa_inventory()
            source = "live-cpa:/models"
        elif args.inventory_json:
            live_ids = read_inventory(args.inventory_json)
            source = f"runtime-inventory:{Path(args.inventory_json).expanduser().resolve()}"
        elif args.available_model:
            live_ids = parse_available_models(args.available_model)
            source = "explicit-runtime-inventory"
        else:
            live_ids = set()
            source = "none"
        route = build_recommendation(args, live_ids, fingerprint, source)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(route, ensure_ascii=False, indent=2))
    return 0 if route["status"] in {"READY", "CAPABILITY_CHECK_REQUIRED", "WITHHELD"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
