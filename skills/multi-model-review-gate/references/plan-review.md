# Plan review mode

Use this mode only after a Plan V1 exists. Review the plan as one artifact even when execution later has several macro phases.

## Frozen packet

Include only explicit, relevant inputs:

- original request, constraints, non-goals, and approval boundaries;
- Plan V1;
- acceptance criteria;
- verified repository, dependency, PR, and baseline-test facts;
- explicit assumptions and unresolved user decisions.

Record an artifact hash. Do not recursively upload a repository, secret-bearing configuration, credentials, `.env` files, private keys, or unrelated logs. If a material fact is missing, identify it instead of inventing it.

## Reviewer roles

For STANDARD work, use only `coverage`. For HIGH work, run `coverage` and `adversarial` concurrently. For CRITICAL work, keep the maximum at two and replace a generic role with a domain-specific `safety` role when appropriate.

Every reviewer receives the same packet and returns at most five findings in this shape:

```json
{
  "verdict": "sound | needs_changes | blocked",
  "findings": [
    {
      "id": "stable-id",
      "severity": "P0 | P1 | P2 | P3",
      "claim": "specific defect in the plan",
      "evidence": ["plan section, repository fact, or missing required evidence"],
      "impact": "what fails if unchanged",
      "minimal_change": "smallest sufficient correction"
    }
  ],
  "missing_evidence": [],
  "confidence": "high | medium | low"
}
```

Reject style preferences, speculative possibilities without evidence, duplicate findings, and suggestions that expand scope without a user decision.

## GPT adjudication and Plan V2

GPT must inspect the cited evidence and label each finding:

- `ACCEPT`: supported and necessary for correctness or acceptance.
- `REJECT`: unsupported, based on a false premise, already addressed, or harmful.
- `DEFER`: useful but non-blocking and outside the current delivery.

Do not merge incompatible suggestions mechanically. When reviewers conflict on a P0/P1 architecture decision, inspect the repository facts first. Escalate the GPT adjudicator from `high` to `max` only if evidence still requires difficult judgment; do not call a third critic.

When revision is authorized, produce one Plan V2 that incorporates all accepted items and preserves rejected/deferred decisions in a short decision log. Never auto-review Plan V2 and never generate Plan V3. End as:

- `READY_FOR_EXECUTION` when no unresolved P0/P1 or scope decision remains;
- `SCOPE_CHANGE_REQUIRED` when a necessary correction materially changes the assignment;
- `BLOCKED_NEEDS_DECISION` when required evidence or an authorized human choice is missing.

## Execution-model handoff

Only for `READY_FOR_EXECUTION`, read [execution-routing.md](execution-routing.md) and produce one `EXECUTION_ROUTE` from the final Plan V2 snapshot. Resolve exact model IDs from the target executor runtime's current inventory; a CPA model listing proves only CPA availability, not OpenCode, Claude Code, or another runtime's tool support.

The recommendation must remain advisory: `advisory_only=true` and `execute_authorized=false`. Use one model for the whole plan by default, or for a cohesive macro unit with an independent verification and rollback boundary. Never select different models per file, module, PR, phase title, or review finding. After emitting the route, stop and wait for a separate explicit implementation request.

If the target is the locally configured Claude CLI, the GLM Plan Mode pass is an execution preflight, not another plan review. It may emit an execution manifest from Plan V2 but cannot substantively rewrite Plan V2, restart reviewers, or create Plan V3.

## M5-sized example

Treat the complete M5 plan as one review unit. A suitable one-wave route is Kimi long-context coverage plus one Grok/GLM adversarial or feasibility view, followed by one Sol `high` adjudication. Use `max` only when the critics expose an unresolved P0/P1 contract decision. Keep M5's macro phases; do not create a review gate for every phase, PR, module, file, or test group.
