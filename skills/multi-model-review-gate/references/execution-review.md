# Execution review and targeted verification

Use `EXECUTION_REVIEW` for one completed cohesive implementation. Prefer a fresh GPT context so the executor does not approve its own work.

## Frozen packet

Include:

- original request and approved final plan;
- frozen commit SHA or diff;
- build, lint, test, and acceptance outputs;
- executor deviations from the plan and their evidence;
- known baseline failures, environment limitations, and unverified claims.

Review actual changed behavior and plan compliance. Do not relitigate an approved architecture unless implementation evidence proves it infeasible or unsafe.

## Review granularity

Ordinary work gets one review after implementation. Large work may get two macro gates when the first freezes an interface needed by later work or occurs before live/irreversible validation. A third gate requires a distinct deployment and rollback boundary. Do not review every PR or internal phase.

For an M5-sized implementation, normally use:

1. one integrated-code review after contracts, runtime wiring, tests, and trace behavior are complete but before live-device fault injection;
2. one final evidence/acceptance review after the live exercise.

The second gate validates the final acceptance evidence; it does not rerun the first broad code review.

## Findings and fixing

Use evidence-first P0-P3 findings. P0/P1 can block; P2/P3 become backlog unless they directly fail an explicit acceptance criterion. The GPT adjudicator consolidates all reports into one list.

If the request authorizes fixes, apply accepted fixes as one batch and preserve unrelated user changes. If the request asks only for review, return `FIX_REQUIRED` with the consolidated action brief and do not edit.

## VERIFY_FIXES

This is a targeted verification, not a second review wave. It may inspect only:

- previously accepted P0/P1 finding IDs;
- the code directly changed to address them;
- directly related regression tests;
- the original failed and acceptance commands.

Do not scan the whole repository, consult new external critics, add unrelated findings, or make another fix batch. A regression directly caused by the accepted fix may be reported, but the terminal verdict remains:

- `PASS` when every accepted blocker is resolved and relevant checks pass;
- `FAIL_FINAL` otherwise.

After `FAIL_FINAL`, stop. The user must choose whether to accept risk, change scope, or start a separately authorized task.
