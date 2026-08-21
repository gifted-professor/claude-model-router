#!/usr/bin/env python3
"""Behavior tests for advisory execution-model routing."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("recommend_executor.py")
SPEC = importlib.util.spec_from_file_location("recommend_executor", MODULE_PATH)
assert SPEC and SPEC.loader
ROUTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROUTER
SPEC.loader.exec_module(ROUTER)


LIVE = {
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "glm-5.2",
    "kimi-k3",
    "kimi-k3-256k",
    "gemini-3.7-flash-high",
}


def args(**overrides):
    values = {
        "plan_status": "READY_FOR_EXECUTION",
        "risk_tier": "STANDARD",
        "profile": [],
        "cost_priority": "balanced",
        "tests": "mature",
        "runtime": "codex-cpa",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def route(**overrides):
    return ROUTER.build_recommendation(args(**overrides), LIVE, "a" * 64, "test-inventory")


class ExecutorRecommendationTests(unittest.TestCase):
    def test_default_complete_plan_uses_flash_then_glm(self):
        result = route()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["primary"]["resolved_exact_id"], "deepseek-v4-flash")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "glm-5.2")
        self.assertEqual(result["max_model_switches"], 1)

    def test_hard_agentic_plan_prefers_glm_and_uses_kimi_only_as_fallback(self):
        result = route(risk_tier="HIGH", profile=["agentic,long-horizon"])
        self.assertEqual(result["profile"], "HARD_AGENT")
        self.assertEqual(result["primary"]["resolved_exact_id"], "glm-5.2")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "kimi-k3")
        self.assertNotEqual(result["primary"]["family"], "kimi-k3")

    def test_terminal_heavy_hard_plan_prefers_deepseek_pro(self):
        result = route(risk_tier="HIGH", profile=["terminal-heavy"])
        self.assertEqual(result["primary"]["resolved_exact_id"], "deepseek-v4-pro")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "kimi-k3")

    def test_context_overflow_requires_kimi_256k_capability_check(self):
        result = route(risk_tier="HIGH", profile=["context-overflow"])
        self.assertEqual(result["status"], "CAPABILITY_CHECK_REQUIRED")
        self.assertEqual(result["primary"]["resolved_exact_id"], "kimi-k3-256k")
        self.assertIsNone(result["fallback"])
        self.assertEqual(result["max_model_switches"], 0)

    def test_visual_route_never_falls_back_to_text_only_model(self):
        result = route(profile=["vision"])
        self.assertEqual(result["status"], "CAPABILITY_CHECK_REQUIRED")
        self.assertEqual(result["primary"]["resolved_exact_id"], "kimi-k3")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "gemini-3.7-flash-high")

    def test_visual_and_context_overflow_requires_one_model_with_both_capabilities(self):
        result = route(risk_tier="HIGH", profile=["vision,context-overflow"])
        self.assertEqual(result["profile"], "VISION_LONG_CONTEXT")
        self.assertEqual(result["primary"]["resolved_exact_id"], "kimi-k3-256k")
        self.assertIsNone(result["fallback"])
        self.assertIn("vision", result["required_capabilities"])
        self.assertIn("verified-context-capacity", result["required_capabilities"])

    def test_security_route_uses_glm_then_deepseek_pro(self):
        result = route(risk_tier="HIGH", profile=["security"])
        self.assertEqual(result["primary"]["resolved_exact_id"], "glm-5.2")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "deepseek-v4-pro")

    def test_non_ready_plan_withholds_every_executor(self):
        result = route(plan_status="BLOCKED_NEEDS_DECISION", risk_tier="HIGH")
        self.assertEqual(result["status"], "WITHHELD")
        self.assertIsNone(result["primary"])
        self.assertIsNone(result["fallback"])
        self.assertFalse(result["execute_authorized"])

    def test_critical_route_has_no_automatic_model_switch(self):
        result = route(risk_tier="CRITICAL", profile=["long-horizon"])
        self.assertEqual(result["primary"]["resolved_exact_id"], "glm-5.2")
        self.assertIsNone(result["fallback"])
        self.assertEqual(result["max_model_switches"], 0)

    def test_missing_runtime_inventory_never_invents_exact_id(self):
        result = ROUTER.build_recommendation(args(), set(), "b" * 64, "none")
        self.assertEqual(result["status"], "NO_VERIFIED_EXECUTOR")
        self.assertIsNone(result["primary"]["resolved_exact_id"])
        self.assertEqual(result["primary"]["family"], "deepseek-v4-flash")
        self.assertIsNone(result["fallback"])
        self.assertIsNone(result["inventory_observed_at"])

    def test_comma_separated_inventory_trims_model_ids(self):
        inventory = ROUTER.parse_available_models(["deepseek-v4-flash, glm-5.2"])
        self.assertEqual(inventory, {"deepseek-v4-flash", "glm-5.2"})

    def test_withheld_route_does_not_claim_inventory_observation(self):
        result = ROUTER.build_recommendation(
            args(plan_status="BLOCKED_NEEDS_DECISION"),
            set(),
            "0" * 64,
            "not-queried:plan-not-ready",
        )
        self.assertIsNone(result["inventory_observed_at"])

    def test_available_fallback_is_promoted_before_execution_without_spending_a_switch(self):
        available = {"glm-5.2", "deepseek-v4-pro"}
        result = ROUTER.build_recommendation(args(), available, "c" * 64, "test-inventory")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["primary"]["resolved_exact_id"], "glm-5.2")
        self.assertTrue(result["primary_promoted_from_fallback"])
        self.assertEqual(result["fallback"]["resolved_exact_id"], "deepseek-v4-pro")

    def test_high_volume_standard_work_keeps_flash_despite_weak_tests(self):
        result = route(profile=["high-volume"], tests="weak")
        self.assertEqual(result["profile"], "HIGH_VOLUME")
        self.assertEqual(result["primary"]["resolved_exact_id"], "deepseek-v4-flash")

    def test_claude_cli_standard_route_uses_glm_preflight_then_flash_execution(self):
        inventory = {"glm-5.2[1m]", "glm-5.2", "deepseek-v4-flash"}
        result = ROUTER.build_recommendation(
            args(runtime="claude-cli-custom"),
            inventory,
            "d" * 64,
            "claude-cli-inventory",
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["preflight"]["model"]["resolved_exact_id"], "glm-5.2[1m]")
        self.assertEqual(result["preflight"]["effort"], "high")
        self.assertFalse(result["preflight"]["may_modify_approved_plan"])
        self.assertFalse(result["preflight"]["may_create_plan_v3"])
        self.assertEqual(result["primary"]["resolved_exact_id"], "deepseek-v4-flash")
        self.assertEqual(result["fallback"]["resolved_exact_id"], "glm-5.2[1m]")

    def test_claude_cli_hard_route_keeps_glm_and_does_not_invent_kimi(self):
        inventory = {"glm-5.2[1m]", "glm-5.2", "deepseek-v4-flash"}
        result = ROUTER.build_recommendation(
            args(runtime="claude-cli-custom", risk_tier="HIGH", profile=["agentic,long-horizon"]),
            inventory,
            "e" * 64,
            "claude-cli-inventory",
        )
        self.assertEqual(result["preflight"]["effort"], "xHigh")
        self.assertEqual(result["primary"]["resolved_exact_id"], "glm-5.2[1m]")
        self.assertIsNone(result["fallback"])
        self.assertEqual(result["unresolved_fallback"]["family"], "kimi-k3")
        self.assertEqual(result["max_model_switches"], 0)

    def test_claude_cli_route_blocks_when_required_glm_preflight_is_unavailable(self):
        result = ROUTER.build_recommendation(
            args(runtime="claude-cli-custom"),
            {"deepseek-v4-flash"},
            "f" * 64,
            "claude-cli-inventory",
        )
        self.assertEqual(result["status"], "NO_VERIFIED_PREFLIGHT")

    def test_plain_glm_cannot_impersonate_required_one_million_context_preflight(self):
        result = ROUTER.build_recommendation(
            args(runtime="claude-cli-custom"),
            {"glm-5.2", "deepseek-v4-flash"},
            "1" * 64,
            "claude-cli-inventory",
        )
        self.assertEqual(result["status"], "NO_VERIFIED_PREFLIGHT")
        self.assertIsNone(result["preflight"]["model"]["resolved_exact_id"])

    def test_output_is_always_advisory_and_one_whole_plan_route(self):
        result = route(risk_tier="HIGH")
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["execute_authorized"])
        self.assertEqual(result["execution_unit"], "WHOLE_PLAN")
        self.assertEqual(result["default_execution_units"], 1)
        self.assertLessEqual(result["max_model_switches"], 1)


if __name__ == "__main__":
    unittest.main()
