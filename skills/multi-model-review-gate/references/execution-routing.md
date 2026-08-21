# Execution-model routing after Plan V2

Use this only after `PLAN_REVIEW` ends `READY_FOR_EXECUTION`. It recommends an executor; it never launches one. The final Plan V2 hash is the routing snapshot.

## Local default pipeline

Keep the planning and review ownership unchanged:

1. The current ChatGPT/Codex model owns the initial plan, evidence adjudication, and the only allowed Plan V2 rewrite.
2. For a HIGH plan, Kimi remains the coverage reviewer and Grok the adversarial reviewer in one parallel wave.
3. When the executor runtime is the locally configured Claude CLI, use `glm-5.2[1m]` in Claude Plan Mode as a read-only execution preflight. It may translate Plan V2 into a runtime-specific manifest—command order, checkpoints, acceptance commands, and unresolved runtime capabilities—but it must not change requirements, architecture, acceptance criteria, or create Plan V3.
4. For ordinary execution, switch the Claude CLI base model to the runtime-verified DeepSeek V4 Flash mapping. For difficult/long-horizon execution, keep the stronger runtime-verified GLM mapping instead of downgrading to Flash.

In the currently supplied Claude CLI menu, `Default` maps to `glm-5.2[1m]` and the custom `Haiku` slot maps to `deepseek-v4-flash`. Treat `Default`/`Haiku` as runtime slot labels; record and route by the resolved underlying model ID.

The local Claude CLI inventory is distinct from CPA. Kimi or Grok being available as reviewers through CPA does not make either one a Claude CLI executor. Kimi is an execution fallback only when the target executor runtime itself lists a compatible Kimi model with the required repository/tool capabilities.

## Durable routing prior

Treat public benchmarks, prices, and vendor claims as a starting prior rather than permanent facts. Agent harnesses differ, exact model IDs and prices change, and local tool integration can dominate benchmark rank. Resolve an exact ID from the target runtime immediately before handoff and let measured local outcomes supersede this table.

| Work profile | Primary family | One bounded fallback | Why |
|---|---|---|---|
| Ordinary, well-specified, text-only work with usable tests | DeepSeek V4 Flash | GLM 5.x | Default cost-efficient path |
| Difficult long-horizon, cross-system, concurrency, persistence, or agent orchestration | GLM 5.x or DeepSeek V4 Pro | Kimi K3 | Prefer DeepSeek/GLM; use Kimi only after an evidenced blocker |
| Terminal-heavy difficult work | DeepSeek V4 Pro | Kimi K3 | Favor the stronger DeepSeek execution tier before Kimi |
| Security-sensitive work | GLM 5.x | DeepSeek V4 Pro | Keep an independent GPT `high`/`max` completion gate |
| Frozen packet exceeds the verified context capacity of other candidates | Kimi K3 long-context | None unless another runtime candidate is verified to fit | Context capacity is a capability gate, not a reason to fragment the plan |
| Direct screenshot, UI, or design-image understanding is required | Kimi K3 or another runtime-verified vision executor | One different verified vision executor | Never silently fall back to a text-only model |

For `CRITICAL` work involving production writes, irreversible migration, payment, or destructive recovery, recommend one strongest verified DeepSeek/GLM executor and set `max_model_switches=0`. A partial execution must be audited before anyone hands it to another model.

## Handoff budget

- Keep one executor for the whole plan by default. A large plan may use a cohesive macro gate only when it has an independent acceptance and rollback boundary.
- Allow at most one executor switch per unit. Never cycle back to a failed model or walk a longer model list automatically.
- Switch only on objective evidence: runtime/tool capability mismatch, context limit, provider unavailability, or a blocking acceptance failure after the executor has produced a coherent attempt. P2/P3 preferences and ordinary intermediate test failures do not trigger a switch.
- Before switching, freeze the current diff/state, completed commands, acceptance output, external side effects, and exact blocker. The fallback continues from that handoff; it does not restart the plan blindly.
- After fallback failure, return `BLOCKED_EXECUTION_ROUTE`. GPT or the user decides the next task; do not start a third executor or a new Plan review.

## Runtime and telemetry boundaries

An exact ID is valid only when present in the intended runtime inventory. `deepseek-v4-flash` observed in CPA must not be renamed to an unobserved dated suffix or treated as proof that OpenCode exposes the same model. Model-list presence also does not prove vision, context, terminal, or tool-call reliability; capability-critical routes must be checked in that runtime.

Record only plan hash, runtime, exact model ID, profile, timestamps, acceptance result, handoff reason, token/latency counters when available, and human interventions. Do not log prompts, secrets, raw provider bodies, or reasoning content.

Use `scripts/recommend_executor.py` to resolve this recommendation. Prefer `--live-cpa` only when CPA is the actual execution runtime; otherwise pass that runtime's exported inventory with `--inventory-json` or explicit `--available-model` values.
