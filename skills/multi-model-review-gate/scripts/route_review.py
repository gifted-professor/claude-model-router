#!/usr/bin/env python3
"""Deterministically recommend a bounded review route."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass


CRITICAL_FLAGS = {
    "auth",
    "security",
    "payment",
    "data-migration",
    "destructive",
    "irreversible",
    "production-write",
}

HIGH_FLAGS = {
    "baseline-failures",
    "ci-deploy",
    "concurrency",
    "contract-change",
    "cross-platform",
    "distributed",
    "external-side-effects",
    "hardware",
    "live-devices",
    "multi-repo",
    "persistence",
    "rollback-complex",
    "significant-deviation",
}

ALL_FLAGS = CRITICAL_FLAGS | HIGH_FLAGS


@dataclass(frozen=True)
class Route:
    schema: str
    mode: str
    stage: str
    risk_tier: str
    matched_flags: list[str]
    gpt_model: str
    reasoning_effort: str
    pro_mode: bool
    review_use_ultra: bool
    external_roles: list[str]
    full_review_waves: int
    consolidated_rewrites_or_fix_batches: int
    targeted_verifications: int
    default_review_units: int
    max_macro_review_units: int
    terminal_after_run: bool
    escalation: str


def parse_flags(values: list[str]) -> set[str]:
    flags: set[str] = set()
    for value in values:
        flags.update(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(flags - ALL_FLAGS)
    if unknown:
        raise ValueError(f"unknown risk flags: {', '.join(unknown)}")
    return flags


def classify(scope: str, flags: set[str], evidence: str, tests: str) -> str:
    if flags & CRITICAL_FLAGS:
        return "CRITICAL"
    if scope == "large" or flags & HIGH_FLAGS or evidence == "missing" or tests == "fail":
        return "HIGH"
    if scope == "medium" or flags or evidence == "partial" or tests == "not-run":
        return "STANDARD"
    return "LOW"


def build_route(args: argparse.Namespace) -> Route:
    flags = parse_flags(args.flags)
    tier = classify(args.scope, flags, args.evidence, args.tests)

    if tier == "LOW":
        model, effort = "gpt-5.6-terra", "medium"
    elif tier in {"STANDARD", "HIGH"}:
        model, effort = "gpt-5.6-sol", "high"
    else:
        model, effort = "gpt-5.6-sol", "max"

    if args.stage == "verification":
        roles: list[str] = []
        full_waves, rewrites, verifies = 0, 0, 1
        ultra = False
        escalation = "No escalation: return PASS or FAIL_FINAL and stop."
    else:
        full_waves, rewrites = 1, 1
        verifies = 0 if args.mode == "plan" else 1
        if args.no_external or tier == "LOW":
            roles = []
        elif args.mode == "plan":
            roles = ["coverage"] if tier == "STANDARD" else ["coverage", "adversarial"]
        elif tier == "STANDARD":
            roles = []
        elif tier == "HIGH":
            roles = ["implementation"]
        else:
            roles = ["safety", "adversarial"]
        ultra = bool(tier == "CRITICAL" and args.separable_lanes >= 3 and not roles)
        escalation = (
            "Escalate Sol high to max only for an unresolved evidenced P0/P1 conflict; "
            "never add another critic or review wave."
            if tier == "HIGH"
            else "Do not add another critic or review wave."
        )

    if args.mode == "plan":
        max_units = 1
    elif tier == "CRITICAL":
        max_units = 3
    elif tier == "HIGH":
        max_units = 2
    else:
        max_units = 1

    return Route(
        schema="multi-model-review-route.v1",
        mode=args.mode.upper() + "_REVIEW" if args.stage == "initial" else "VERIFY_FIXES",
        stage=args.stage,
        risk_tier=tier,
        matched_flags=sorted(flags),
        gpt_model=model,
        reasoning_effort=effort,
        pro_mode=False,
        review_use_ultra=ultra,
        external_roles=roles,
        full_review_waves=full_waves,
        consolidated_rewrites_or_fix_batches=rewrites,
        targeted_verifications=verifies,
        default_review_units=1,
        max_macro_review_units=max_units,
        terminal_after_run=True,
        escalation=escalation,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "execution"), required=True)
    parser.add_argument("--stage", choices=("initial", "verification"), default="initial")
    parser.add_argument("--scope", choices=("small", "medium", "large"), required=True)
    parser.add_argument("--flags", action="append", default=[])
    parser.add_argument("--evidence", choices=("complete", "partial", "missing"), default="complete")
    parser.add_argument("--tests", choices=("pass", "fail", "not-run", "not-applicable"), default="not-applicable")
    parser.add_argument("--separable-lanes", type=int, default=1)
    parser.add_argument("--no-external", action="store_true")
    args = parser.parse_args()

    if args.separable_lanes < 1:
        parser.error("--separable-lanes must be positive")
    if args.stage == "verification" and args.mode != "execution":
        parser.error("--stage verification is only valid with --mode execution")
    try:
        route = build_route(args)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(route), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
