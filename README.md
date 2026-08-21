# claude-model-router

A two-sided setup that lets **Claude Code** and **Codex** share one multi-model routing stack:

- On the **Claude Code** side, local Anthropic-compatible shims sit between the CLI and any
  OpenAI-compatible provider, so plan mode / execution / background tasks can each run on a
  different model — switched automatically.
- On the **Codex** side, the `multi-model-review-gate` skill reviews plans and implementations
  with heterogeneous external critics, then writes its executor recommendation to a route file
  that the shim picks up.

Nothing here is tied to a specific vendor. The reference setup uses OpenCode Go (GLM / DeepSeek)
for execution and Kimi + Grok as review critics, but every model is just a config entry — swap in
any OpenAI-compatible endpoint.

## Architecture

```
Codex (planning & review)                     Claude Code (daily driver)
─────────────────────────                     ─────────────────────────
multi-model-review-gate skill
  ├─ critics via remote-cpa ──► any OpenAI-compatible reviewers
  │                             (default: Kimi coverage, Grok adversarial)
  └─ set_exec_route.py ──► ~/.claude/exec_route.json ──┐
                                                       ▼
Claude Code ──► shim 127.0.0.1:11437 ──► OpenAI-compatible upstream
                (opencode_anthropic_shim)   (default: OpenCode Go)
                plan mode    → strong model      (default glm-5.3)
                execution    → route file, else difficulty heuristic
                               (default deepseek-v4-flash / glm-5.2)
                background   → cheap fast model  (default deepseek-v4-flash)
```

`exec_route.json` is the only interface between the two sides: the skill writes it, the shim
reads it. It carries a TTL (default 2h); after expiry the shim falls back to its heuristic.

## Components

| Path | What it is |
|---|---|
| `shims/opencode_anthropic_shim.py` | Anthropic `/v1/messages` → OpenAI `/chat/completions` proxy with exec-tier auto routing, session stickiness, skill route-file support, `reasoning_effort` defaulting |
| `shims/ollama_anthropic_shim.py` | Same idea for a local Ollama backend |
| `shims/cpa_anthropic_shim.py` | Same idea for a cli-proxy-api (ChatGPT) backend; depends on `skills/remote-cpa` |
| `skills/multi-model-review-gate` | Bounded plan/execution review with ≤2 external critics + one adjudication; emits `EXECUTION_ROUTE` |
| `skills/remote-cpa` | CPA client library used by the review skill and the cpa shim |
| `skills/claude-cpa-switch` | Switch Claude Code between shim routes (ports 11435/11436/11437) |

## Setup

### 1. Pick and configure your providers

Every model in this stack is reached through an OpenAI-compatible endpoint. To use different
models than the reference setup, you only change these config points — no code changes needed
unless a provider speaks a non-OpenAI protocol.

**Execution provider (for the shim).** Create `~/.claude/opencode_keys.json`:

```json
{
  "api_key": "YOUR_KEY",
  "base_url": "https://opencode.ai/zen/go/v1"
}
```

Point `base_url` at any OpenAI-compatible provider and put the model IDs it serves into the
shim env vars (below) and into `set_exec_route.py`'s `MODEL_ALIASES`.

**Review critics (for the review skill).** `review_batch.py` resolves critics through
`remote-cpa`'s live model list. Configure remote-cpa with whatever reviewer endpoints you have
(Kimi, Grok, or others) — the skill picks the nearest available candidate per role
(`coverage` / `adversarial` / `implementation` / `safety`).

### 2. Run the shim

```bash
python shims/opencode_anthropic_shim.py --host 127.0.0.1 --port 11437
```

Optional env vars:

| Var | Default | Meaning |
|---|---|---|
| `OPENCODE_SHIM_MODEL` | `glm-5.3` | fallback model when the request names none |
| `OPENCODE_EXEC_MODEL` | `glm-5.2` | the "hard" execution tier |
| `OPENCODE_EXEC_MODEL_EASY` | `deepseek-v4-flash` | the "easy" execution tier |
| `OPENCODE_EXEC_ROUTING` | `on` | `off` disables all auto routing |
| `OPENCODE_REASONING_EFFORT` | `low` | injected when the client sends none (prevents reasoning models from burning the whole token budget on hidden thinking) |
| `OPENCODE_EXEC_ROUTE_FILE` | `~/.claude/exec_route.json` | where the skill route file lives |

### 3. Point Claude Code at the shim

In `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "opencode-local",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:11437",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
    "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-v4-flash"
  },
  "model": "opusplan"
}
```

`opusplan` is Claude Code's built-in split: plan mode uses the Opus mapping, execution falls
back to the Sonnet mapping. Map those aliases to whatever your provider serves.

### 4. Use the review skill (Codex)

Install `skills/multi-model-review-gate` and `skills/remote-cpa` into Codex's skills
directory, configure your critics (step 1), then run a review as documented in the skill.
When a plan reaches `READY_FOR_EXECUTION`:

```bash
python scripts/set_exec_route.py --model glm-5.2 --plan-sha256 <sha> --ttl 7200
```

The shim then prefers that model for execution-tier requests until the TTL expires;
`--clear` removes the route early. Without a valid route file, the shim's heuristic decides
per request (hard-signal keywords, tool errors, large context), with per-session stickiness:
once a session goes hard it stays hard, never oscillates.

## Notes

- The shims are stateless; per-request model switching mid-conversation is safe because the
  client resends full context every call.
- Keep API keys out of the repo: `opencode_keys.json`, `exec_route.json`, and logs are
  gitignored by design.
