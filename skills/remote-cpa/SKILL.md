---
name: remote-cpa
description: Call the configured private CLIProxyAPI instance through its OpenAI-compatible API, list live models, send chat requests, or route image requests through the Mac mini Dashboard into CPA Responses image_generation. Use when the user asks to use the remote CPA, inspect available models, test Grok, or use the established Dashboard-to-CPA image chain.
---

# Remote CPA v2

Use the configured internal CLIProxyAPI instance as an OpenAI-compatible backend.
The bundled helpers report version `2.0.0`.

## Connection and credentials

- Keep machine-specific, non-secret connection settings outside the skill in `~/.codex/remote-cpa.local.json`. The MacBook and Windows installation use the same skill code and separate local config files.
- The local config sets the API base URL to `http://<your-cpa-host>:8317/v1` and enables the authorized SSH credential lookup through `user@<your-cpa-host>`.
- The SSH lookup uses the machine's private-key path and reads only the configured `api-keys` entry from `/Users/a1234/.local/opt/CLIProxyAPI/config.local.yaml` into process memory. It sends a complete remote command and never streams a script over SSH standard input. On Windows, the credential returns through a temporary loopback-only reverse forward inside the same encrypted SSH connection, avoiding the Windows OpenSSH stdout-pipe deadlock without writing the key to disk.
- Prefer `CPA_API_KEY` when it is already set. Environment variables override the local config: `CPA_BASE_URL`, `CPA_TIMEOUT`, `CPA_SSH_AUTO`, `CPA_SSH_HOST`, `CPA_SSH_USER`, `CPA_SSH_KEY`, and `CPA_REMOTE_CONFIG`.
- The local config must never contain the CPA API key, passwords, or OAuth tokens. On macOS it must remain mode `0600`; on Windows protect it with the user's normal ACL.
- Image requests use the Mac mini Dashboard at remote `127.0.0.1:8907` and are sent through the existing SSH connection. The Dashboard remains bound to localhost and does not need to be exposed on Tailscale.
- Treat the API key as secret: the SSH fallback reads only the configured `api-keys` entry into process memory; never print it, save it in this skill, put it in source files, or confuse it with the management password.
- The `<your-cpa-host>` address is a private Tailscale address. If it is unreachable, check Tailscale connectivity before changing CPA configuration.

## Codex provider mode

Codex is configured to use this CPA instance as its default custom model provider:

- Provider: `cliproxyapi`
- Base URL: `http://<your-cpa-host>:8317/v1`
- Default model: `gpt-5.6-sol`
- Authentication: Codex invokes the bundled `auth-token` helper, which uses the authorized SSH fallback and does not store the CPA API key in `config.toml` or `auth.json`.

In this mode, model inference is routed to the OAuth accounts managed by CPA, so the upstream provider quota and rate limits belong to those accounts rather than the local Codex ChatGPT model quota. Multiple accounts do not create unlimited capacity or guarantee serialized requests; CPA's account routing and each provider's concurrency, rate, and quota limits still apply.

Switch models with Codex's model picker or `/model`. If a CPA-specific model ID is not shown in the picker, pass the live ID directly, for example `codex -m gpt-5.6-terra`. Use the live `models` command below as the source of truth.

When the user asks to enable or re-enable CPA as the local Codex provider, run the bundled configurator before making model calls:

```text
python3 scripts/cpa_request.py doctor
python3 scripts/codex_provider.py enable --model gpt-5.6-sol
python3 scripts/codex_provider.py doctor
```

`enable` first verifies the local config, SSH credential lookup, and live `/models` endpoint. A failed preflight leaves Codex configuration unchanged. A successful first switch writes `config.toml` and a non-secret `~/.codex/remote-cpa.previous.json` routing snapshot for safe restore. It must not modify `auth.json`, `state_5.sqlite`, or rollout JSONL files. Routing model inference through CPA is separate from sidebar-history metadata.

Codex Desktop may filter the sidebar history by `model_provider`. Only when the user explicitly asks to migrate sidebar history, first show the dry-run and obtain confirmation before applying it:

```text
python3 scripts/codex_provider.py sync-history --to cliproxyapi --dry-run
python3 scripts/codex_provider.py sync-history --to cliproxyapi
python3 scripts/codex_provider.py verify-history --provider cliproxyapi
```

History sync backs up `state_5.sqlite` and every affected transcript under `~/.codex/provider-history-backups/`, then updates provider metadata in the database and rollout JSONL files. It is not required for CPA authentication or quota routing.

History sync behavior:

- Always run `--dry-run` first. If `threads_to_sync=0`, do not create another backup or migration.
- On macOS/APFS, transcript backups use copy-on-write clones first, so future backups consume little additional physical space. If cloning is unavailable, the script falls back to a normal copy.
- The thread executing the provider switch may be migrated, but other threads with live processes are skipped by default to avoid rewriting a transcript while it is being written.
- If `active_threads_skipped` is non-zero, let those tasks finish and run the same sync command again. Do not use `--include-active` unless the user explicitly accepts the concurrent-write risk.
- Every applied sync validates both `state_5.sqlite` and each affected transcript before committing the database transaction.

The configurator is idempotent, preserves unrelated `config.toml` settings, and uses command-backed SSH authentication. It must not write a CPA API key or modify `auth.json`.

To switch back to the exact model/provider captured before the first v2 enable, without migrating history:

```text
python3 scripts/codex_provider.py disable
python3 scripts/codex_provider.py status
```

An installation upgraded while CPA was already active has no trustworthy previous snapshot. In that case `disable` fails closed and requires an explicit built-in model, for example `disable --model gpt-5.4`; do not guess that value for the user.

If the user also explicitly requests history migration back to `openai`, run the corresponding `sync-history --to openai --dry-run` first and obtain confirmation before applying it.

To inspect or reverse the most recent history metadata migration:

```text
python3 scripts/codex_provider.py history-status
python3 scripts/codex_provider.py restore-history --backup latest
```

Restart Codex Desktop after a provider history sync so the sidebar reloads its thread list.

This history migration is a workaround for provider-filtered sidebars, not part of CPA authentication. `enable` and `disable` deliberately have no history flags; history changes are available only through the explicit `sync-history` command.

The Codex app may still make separate background requests to ChatGPT for plugin catalogs or analytics. Those are auxiliary app services and are not the model inference path used by this provider.

## Model discovery

Run the bundled script to query the live model list:

```text
python3 scripts/cpa_request.py models
python3 scripts/cpa_request.py models --grok-only
python3 scripts/cpa_request.py models --contains 4.3
python3 scripts/cpa_request.py models --json
```

Present model IDs exactly as returned by CPA. Treat the live response as authoritative; do not rely on a stale hard-coded model list.

## Grok and chat calls

Use the OpenAI-compatible chat endpoint through the bundled script:

```text
python3 scripts/cpa_request.py chat --model grok-4.3 --message "..."
```

Use a model returned by the live model query. Keep test prompts short unless the user asks for a larger request. Report the HTTP/API result without exposing credentials.

## Dashboard image mode

For image generation or repair, do not call Codex's built-in image tool directly. Send one JSON request to the existing Dashboard on the Mac mini:

```text
caller -> Mac mini Dashboard 127.0.0.1:8907/api/image/generate
       -> CPA 127.0.0.1:8317/v1/responses
       -> gpt-5.5 -> image_generation(gpt-image-2)
       -> Dashboard returns imageDataUrl/imageDataUrls
```

Because port 8907 is localhost-only on the Mac mini, send the request through the existing SSH connection. Given a local `request.json` containing the payload, the complete call is:

```bash
ssh -i ~/.ssh/xhs-windows-from-1234macmini-20260714-200359 \
  -o BatchMode=yes \
  user@<your-cpa-host> \
  'curl -sS -X POST http://127.0.0.1:8907/api/image/generate \
    -H "content-type: application/json" --data-binary @-' \
  < request.json > response.json
```

The request JSON is simply:

```json
{
  "prompt": "generation or repair instruction",
  "type": "slot or task type",
  "images": ["data:image/...;base64,..."],
  "productContext": {
    "sku": "SKU-123",
    "verifiedFacts": ["grounded fact"]
  }
}
```

The `images` entries are Data URLs built from the reference files. The Dashboard owns the guarded prompt, CPA `/v1/responses` payload, model selection, retries, SSE parsing, and Base64 normalization; do not duplicate those internals in the skill. Read `imageDataUrl` or the first item in `imageDataUrls` from `response.json`. Treat `fallback=true` as failure.

For a connectivity/config check that does not generate an image, use the same SSH command with `curl -sS http://127.0.0.1:8907/api/config`.

## Operational boundaries

- Do not call `/management.html` or management endpoints for ordinary model requests.
- Do not call `/api/image/generate` merely to test connectivity; read Dashboard `/api/config` instead.
- Do not generate or repair an image unless the user explicitly requests that image operation.
- Do not request, print, export, or inspect OAuth token files.
- Do not modify remote CPA configuration, accounts, firewall, Tailscale, or SSH settings unless the user explicitly asks for that exact change.
- For a connectivity check, first use `models`; a `401` means the endpoint is reachable but the API key is missing or invalid.
- For a model call, distinguish transport failures, API authentication failures, and upstream provider/model errors in the response.
- Treat missing config, embedded secrets, loose macOS config permissions, missing SSH keys, failed credential lookup, and an unreachable `/models` endpoint as hard failures. Never silently fall back to `127.0.0.1`.

## Bundled resource

Use `scripts/cpa_request.py` for deterministic model listing and chat requests. For images, send the documented JSON request to the Mac mini Dashboard through SSH; the Dashboard owns the CPA image-generation internals.
