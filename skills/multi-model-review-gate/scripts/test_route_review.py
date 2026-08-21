#!/usr/bin/env python3
"""Behavior tests for the bounded review router."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("route_review.py")
SPEC = importlib.util.spec_from_file_location("route_review", MODULE_PATH)
assert SPEC and SPEC.loader
ROUTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROUTER
SPEC.loader.exec_module(ROUTER)


def args(**overrides):
    values = {
        "mode": "plan",
        "stage": "initial",
        "scope": "small",
        "flags": [],
        "evidence": "complete",
        "tests": "pass",
        "separable_lanes": 1,
        "no_external": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RouteTests(unittest.TestCase):
    def test_m5_is_one_high_plan_unit_and_one_wave(self):
        route = ROUTER.build_route(
            args(
                scope="large",
                flags=["contract-change,distributed,concurrency,cross-platform,live-devices,baseline-failures"],
                tests="not-applicable",
                separable_lanes=3,
            )
        )
        self.assertEqual(route.risk_tier, "HIGH")
        self.assertEqual(route.external_roles, ["coverage", "adversarial"])
        self.assertEqual(route.reasoning_effort, "high")
        self.assertFalse(route.review_use_ultra)
        self.assertEqual(route.full_review_waves, 1)
        self.assertEqual(route.targeted_verifications, 0)
        self.assertEqual(route.max_macro_review_units, 1)

    def test_small_bugfix_stays_lightweight(self):
        route = ROUTER.build_route(args())
        self.assertEqual(route.risk_tier, "LOW")
        self.assertEqual(route.gpt_model, "gpt-5.6-terra")
        self.assertEqual(route.reasoning_effort, "medium")
        self.assertEqual(route.external_roles, [])

    def test_completed_medium_feature_uses_one_fresh_gpt_review(self):
        route = ROUTER.build_route(args(mode="execution", scope="medium"))
        self.assertEqual(route.risk_tier, "STANDARD")
        self.assertEqual(route.gpt_model, "gpt-5.6-sol")
        self.assertEqual(route.reasoning_effort, "high")
        self.assertEqual(route.external_roles, [])
        self.assertEqual(route.full_review_waves, 1)

    def test_critical_verification_cannot_restart_review(self):
        route = ROUTER.build_route(
            args(
                mode="execution",
                stage="verification",
                scope="large",
                flags=["security,data-migration"],
                evidence="partial",
                tests="fail",
                separable_lanes=3,
            )
        )
        self.assertEqual(route.risk_tier, "CRITICAL")
        self.assertEqual(route.reasoning_effort, "max")
        self.assertEqual(route.external_roles, [])
        self.assertEqual(route.full_review_waves, 0)
        self.assertEqual(route.consolidated_rewrites_or_fix_batches, 0)
        self.assertEqual(route.targeted_verifications, 1)
        self.assertFalse(route.review_use_ultra)


if __name__ == "__main__":
    unittest.main()
