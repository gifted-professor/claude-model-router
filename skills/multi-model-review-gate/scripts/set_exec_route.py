#!/usr/bin/env python3
"""Write the executor route chosen by multi-model-review-gate for Claude Code's shim.

After a PLAN_REVIEW ends READY_FOR_EXECUTION and EXECUTION_ROUTE picks an executor,
record it here so opencode_anthropic_shim.py (127.0.0.1:11437) prefers this model
for execution-tier requests instead of falling back to its keyword heuristic.

Writes ~/.claude/exec_route.json:
    {"model": "<model-id>", "expires_at": <epoch>, "plan_sha256": "...", "source": ...}

The shim honors the route only until expires_at; after that it returns to heuristics.
This file is advice for the local shim only — it never grants execution authority.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROUTE_FILE = Path.home() / ".claude" / "exec_route.json"

# Executor family -> concrete OpenCode Go model id the shim can send upstream.
MODEL_ALIASES = {
    "flash": "deepseek-v4-flash",
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "glm-5.2": "glm-5.2",
    "glm": "glm-5.2",
    "glm-5.3": "glm-5.3",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",
                        help="executor model id or alias (flash / deepseek-v4-pro / glm-5.2 / glm-5.3)")
    parser.add_argument("--ttl", type=int, default=7200,
                        help="seconds the route stays valid (default 7200 = 2h, one execution session)")
    parser.add_argument("--plan-sha256", default="",
                        help="sha256 of the approved plan, for traceability only")
    parser.add_argument("--clear", action="store_true",
                        help="remove any active route instead of writing one")
    args = parser.parse_args()

    if args.clear:
        ROUTE_FILE.unlink(missing_ok=True)
        print(f"cleared {ROUTE_FILE}")
        return

    if not args.model:
        parser.error("--model is required unless --clear")

    model = MODEL_ALIASES.get(args.model.lower())
    if model is None:
        raise SystemExit(f"unknown model {args.model!r}; known: {sorted(MODEL_ALIASES)}")

    payload = {
        "model": model,
        "expires_at": time.time() + args.ttl,
        "plan_sha256": args.plan_sha256,
        "source": "multi-model-review-gate",
    }
    ROUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROUTE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"route written: exec -> {model}, valid {args.ttl}s ({ROUTE_FILE})")


if __name__ == "__main__":
    main()
