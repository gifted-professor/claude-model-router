---
name: multi-model-review-gate
description: Run a bounded, adaptive review after a substantial plan or completed implementation, using at most two complementary external model perspectives and one GPT adjudication, then optionally recommend a bounded execution-model handoff for an approved final plan. Use for multi-model plan review, Plan V2 convergence, completion review, or model/effort routing; do not use for routine single-pass code review or every small implementation step.
---

# Multi-model Review Gate

Review one frozen plan or implementation snapshot, converge the useful findings once, and stop. Support three entry modes:

- `PLAN_REVIEW`: evaluate an existing Plan V1 and, when revision is requested, produce at most Plan V2.
- `EXECUTION_REVIEW`: evaluate a completed cohesive implementation against its approved plan and evidence.
- `VERIFY_FIXES`: check only previously accepted blocking findings and their related regressions.

Read [plan-review.md](references/plan-review.md) only for `PLAN_REVIEW`. Read [execution-review.md](references/execution-review.md) only for `EXECUTION_REVIEW` or `VERIFY_FIXES`.
For a `PLAN_REVIEW` that ends `READY_FOR_EXECUTION`, also read [execution-routing.md](references/execution-routing.md) and attach one advisory execution route.

## Invariants

1. Freeze one review packet before calling reviewers. Every reviewer sees the same artifact and evidence. Never revise between reviewers.
2. One full review wave may contain up to two independent external reviewers running in parallel, followed by one GPT adjudication. Multiple calls inside that batch still count as one wave.
3. External reviewers are read-only critics. GPT owns evidence checking, conflict resolution, and the single consolidated rewrite or fix decision; do not decide by majority vote.
4. Allow at most one consolidated rewrite/fix batch. Plan V2 is terminal and is not automatically reviewed again.
5. For execution fixes only, allow at most one targeted verification. It may check only accepted P0/P1 findings, directly related regressions, and the original acceptance commands. Plan V2 gets no verification or second review.
6. Never invoke this skill recursively. A reviewer must not recommend calling another reviewer.
7. P2/P3 suggestions go to a backlog and never trigger another round. Each reviewer may return at most five high-confidence findings backed by concrete evidence.
8. Authorization to revise Plan V1 covers at most Plan V2. It never authorizes executing Plan V2, starting another agent, changing providers, or modifying code.

## Preserve coarse review units

Default to one review unit: the whole plan or the completed cohesive deliverable. Split execution only at independently integrable, verifiable, and reversible boundaries. Do not split by file, class, reviewer specialty, review comment, commit, or small PR.

- Ordinary work: one unit.
- Large cross-system work: normally two macro gates; never more than three without an independent deploy/rollback boundary.
- A plan itself is always reviewed as one artifact.
- For an M5-sized milestone, keep the existing macro phases together for plan review. Execution normally gets one integrated-code gate before live or irreversible validation and one final acceptance gate, not a full review for M5-0/A/B/C or every PR.

## Route before spending quota

Classify risk from semantics, not line count alone. Use `scripts/route_review.py` when inputs can be summarized into scope and flags.

```powershell
python scripts/route_review.py --mode plan --scope large `
  --flags contract-change,distributed,concurrency,cross-platform,live-devices `
  --evidence complete --tests not-applicable --separable-lanes 3
```

Use the highest matching tier:

| Tier | Typical conditions | GPT adjudicator | External perspectives |
|---|---|---|---|
| LOW | Local, reversible, mature tests, no contract/data/permission effect | GPT-5.6 Terra `medium` | None |
| STANDARD | One subsystem or ordinary multi-file feature | GPT-5.6 Sol `high` | Plan: one coverage critic; execution: normally none |
| HIGH | Cross-system, public contract, concurrency, persistence, CI/deploy, hardware/live devices, weak baseline, or major plan deviation | GPT-5.6 Sol `high`; use `max` only for unresolved P0/P1 architecture conflict | Plan: coverage + adversarial; execution: one implementation critic |
| CRITICAL | Auth/security, payment, irreversible data migration/deletion, production writes, or destructive recovery semantics | GPT-5.6 Sol `max` | At most two risk-specific critics |

Keep the local HIGH-plan ownership stable: the current ChatGPT/Codex model remains planner and final adjudicator; Kimi is the default coverage critic and Grok the default adversarial critic. `EXECUTION_ROUTE` changes only the later executor handoff and never replaces this planning/review stack.

Treat exact model IDs as live configuration, not permanent truth. `review_batch.py` loads `remote-cpa`, queries the live model list once, and resolves the nearest available candidate per role. Useful roles are:

- `coverage`: long-context Kimi for requirement coverage, cross-file consistency, and omissions.
- `adversarial`: Grok for counterexamples, false assumptions, and alternative architecture. It may verify current facts only when an actual search tool is available.
- `implementation`: GLM, DeepSeek, or Kimi Code for feasibility, code paths, and tests.
- `safety`: an independent model focused on authorization, data integrity, rollback, and abuse paths.

Do not use the execution model as the only implementation reviewer. If current GPT model or effort is below the route, report the recommendation explicitly; use an exact fresh agent override when the runtime supports it, otherwise use the strongest available GPT and disclose the downgrade.

Ultra is primarily an initial-planning or parallel-investigation choice. A HIGH review already using two heterogeneous external critics should normally use a single Sol `high` adjudicator, not Ultra again. Consider Ultra only for CRITICAL work with at least three truly independent evidence lanes; it still consumes the same single review wave.

## Single-wave workflow

1. Identify the mode and freeze the packet, including artifact hash or commit SHA.
2. Emit `REVIEW_ROUTE` before external calls: tier, review unit, selected GPT/effort, critic roles, rationale, and remaining budget.
3. If critics are selected, run all of them concurrently against the frozen packet. Use `scripts/review_batch.py` for explicit long-file input and a maximum concurrency of two. Do not hand-roll `/chat/completions` payloads; the script owns JSON mode, thinking-model output budget, and recovery. Leave `--timeout` unset unless a longer wait is required.
4. GPT verifies evidence, deduplicates findings, and labels every item `ACCEPT`, `REJECT`, or `DEFER`.
5. Only evidenced P0/P1 findings can block. Apply all accepted changes once when the user authorized revision or fixing; otherwise return one consolidated action brief.
6. Run the original acceptance checks plus targeted checks for accepted blockers. End immediately with a terminal verdict.
7. Only after a plan reaches `READY_FOR_EXECUTION`, run `scripts/recommend_executor.py` against the final plan and the target runtime's current inventory. Attach `EXECUTION_ROUTE`, then stop. Do not start the recommended executor. When the chosen executor is served by the local Claude Code shim (deepseek-v4-flash / deepseek-v4-pro / glm-5.2 / glm-5.3), also record it with `python scripts/set_exec_route.py --model <id> --plan-sha256 <sha>` so the shim prefers this model for execution-tier requests until the TTL expires; heuristic routing is only the fallback when no valid route file exists.

Each critic gets at most two provider calls total: one primary attempt and one recovery attempt. This remains one review wave. `review_batch.py` records sanitized per-attempt diagnostics (`finish_reason`, token usage, response/message keys, request option keys, final-content length, and reasoning-content length) but never stores reasoning text.

Primary calls are JSON-mode chat completions. Kimi K3 is sent `reasoning_effort=low` plus a thinking output floor so its default max thinking cannot consume the entire `max_tokens` budget before the JSON object. Grok is JSON-mode only: on this CPA path `reasoning_effort` makes grok-4.5/4.6 fail closed with an upstream 500. If a provider rejects request extras, the recovery call retries the same model with extras stripped.

- Authentication failures stop that critic immediately.
- Invalid JSON/schema uses the sole recovery call on the next live same-role candidate when one exists. Only `--no-model-fallback` or a missing candidate keeps format repair on the same model.
- Truncated output, or empty final content accompanied by reasoning output, stays on the same model and raises the output allowance. A complete JSON object is accepted even when `finish_reason` is `length`.
- A connection failure retries the same model. Normal-stop empty final content, timeout, rate limit, model unavailability (including HTTP 404), or upstream 5xx uses the next live same-role candidate when available. Prefer a different model family and never duplicate the other critic's primary model when an alternative exists.
- Authentication/authorization, content filtering, and known context-window failures are non-retryable. Do not use a fallback to bypass them.
- A successful recovery is marked `recovered_with_fallback` when the model changed. It is not a new critic or review wave.

If one critic still fails after its bounded recovery, continue with the other and mark the batch `degraded`. If `gpt_fallback_required` is true, fall back once to the routed GPT reviewer and adjudicate from the frozen packet plus any partial diagnostics. Do not wait indefinitely, add another external call, or create another wave. Near-miss critic JSON (verdict aliases, numeric confidence, extra findings) is coerced locally; do not treat that as a reason to start another wave.

## Terminal outputs

Return a compact record containing the frozen snapshot, route, accepted/rejected/deferred findings, revision or fix batch, verification evidence, and remaining risk.

- Plan: `READY_FOR_EXECUTION`, `SCOPE_CHANGE_REQUIRED`, or `BLOCKED_NEEDS_DECISION`.
- Execution: `READY_TO_MERGE`, `FIX_REQUIRED`, or `BLOCKED_UNVERIFIABLE`.
- Targeted verification: `PASS` or `FAIL_FINAL`.

When a recommendation changes user requirements, public contracts, data models, security boundaries, deployment architecture, or acceptance criteria, return `SCOPE_CHANGE_REQUIRED`; never smuggle that expansion into Plan V2. After `FAIL_FINAL` or any exhausted budget, stop and request a human decision rather than starting another review/fix loop.

For `READY_FOR_EXECUTION`, include one `EXECUTION_ROUTE` with the final plan hash, target runtime inventory source, primary and fallback executor, switch budget, and capability caveats. Omit it for `SCOPE_CHANGE_REQUIRED` and `BLOCKED_NEEDS_DECISION`. The route is advice only and does not change the terminal verdict or grant execution authority.
