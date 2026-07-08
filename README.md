# litellm-fleet

Self-hosted model router: one LiteLLM router in front of per-account sidecars
(2× GitHub Copilot, 2× ChatGPT Codex, optional Claude Code bridge, LMStudio),
with strict priority failover, per-project virtual keys, and a macOS menu bar
app to control the fleet.

Subscription providers bind **one identity per process** (token dir is a
process-global env var in LiteLLM), so each account runs as its own sidecar
container; the router fronts them as OpenAI-compatible deployments.

```
clients (per-project key) ──> router :4000
     order=1..n per alias, 429 -> cooldown -> next
   ┌────────┬────────┬─────────┬─────────┬────────┐
copilot-1 copilot-2 chatgpt-1 chatgpt-2 [claude]  lmstudio (host :1234)
  :4001     :4002     :4003     :4004    :4005
                 postgres (virtual keys)
```

## Setup

```sh
cp main.example.yaml main.yaml                        # then edit: your accounts, aliases, projects
uv run generate.py                                    # main.yaml -> generated/
docker-compose -f generated/docker-compose.yml up -d  # start fleet
scripts/auth.sh                                       # interactive auth helper (see below)
uv run scripts/sync-keys.py                           # per-project virtual keys -> generated/keys.json
LiteLLMBar/build.sh --install                         # menu bar app
```

Requires: colima (give it RAM: `colima start --memory 12 --cpu 4`),
docker-compose, uv, LMStudio serving on :1234.

## Everything flows from `main.yaml`

- `accounts` — one sidecar per subscription account
- `aliases` — model names clients call; `chain` is strict priority order
- `projects` — per-project account allowlists; restricted projects call
  `<project>/<alias>` model names with their own key
- `router` — published port (`port`, default 4000) and retry/cooldown tuning
  (`cooldown_time` = how long an exhausted account stays benched)

After editing: `uv run generate.py && docker-compose -f generated/docker-compose.yml up -d`
(or menu bar → Regenerate Configs), then `uv run scripts/sync-keys.py` if
projects changed. Secrets in `generated/.env` are preserved across regens.

## Usage

```sh
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
export OPENAI_API_KEY=<key from generated/keys.json, or master key>
# models: codex | copilot | claude | local  (restricted: secretproj/codex, ...)
```

To wire Claude Code, Codex, OpenCode, or Copilot (CLI and desktop/IDE) at the
router, see [CLIENTS.md](CLIENTS.md).

## Auth helper

`scripts/auth.sh` (a wrapper for `uv run scripts/auth.py`) is an interactive
TUI that guides authentication for every configured account:

- status table for all accounts — JWTs are decoded so you can see which
  identity a token belongs to, plus expiry
- guided OAuth **device flows** (copilot/chatgpt): extracts the URL + user
  code from the sidecar's container logs, detects stale codes (>15 min) and
  offers a container restart to mint a fresh one
- imports credentials already on your machine instead of re-authing:
  `~/.codex/auth.json` (Codex CLI login — useful when a ChatGPT account
  blocks the device flow; the refresh token is interchangeable between
  flows), `~/.config/github-copilot/{apps,hosts}.json`, and
  `~/.claude/.credentials.json`
- manual token paste for `claude setup-token` output and Bedrock API keys,
  with a decoded-JWT preview before writing
- "delete current auth and start over" for when a flow went sideways

`scripts/codex-import.py` remains available standalone:

```sh
codex login                        # writes ~/.codex/auth.json
uv run scripts/codex-import.py     # menu of chatgpt targets; --account to skip
```

Failover: quota-exhausted deployment (429) is benched for `cooldown_time`
seconds, next in chain takes over; LMStudio is the never-benched last resort.

## Menu bar app

Status dot, Start/Stop fleet, Copy Master Key, Regenerate Configs, Open Logs,
Start at Login (also auto-starts the fleet on launch). Quit leaves the fleet
running — docker owns it.

The app assumes the repo lives at `~/litellm-fleet`; use **Set Repo Folder…**
in the menu (or `defaults write dev.litellm-fleet.LiteLLMBar RepoPath <path>`)
if you cloned it elsewhere.

## Claude bridge (optional, disabled by default)

[Meridian](https://github.com/rynfar/meridian) wraps the Claude Code SDK as an
OpenAI-compatible server. Enable:

```sh
git clone https://github.com/rynfar/meridian vendor/meridian   # review it first
# main.yaml: accounts.claude.enabled: true, then:
uv run generate.py         # creates empty tokens/<name>/token files
claude setup-token          # paste result into tokens/<name>/token
docker-compose -f generated/docker-compose.yml up -d --build
```

## Bedrock (optional, disabled by default)

Long-term Bedrock API key auth — no OAuth device flow, but otherwise a sidecar
just like copilot/chatgpt: one container per account/key, so you can add as
many `bedrock-N` accounts (different AWS orgs, keys, or regions) as you like.
Enable:

```sh
# main.yaml: accounts.bedrock-1.enabled: true (add more bedrock-N as needed), then:
uv run generate.py                     # creates tokens/bedrock-1/token (empty)
# paste that account's long-term Bedrock API key into tokens/bedrock-1/token
docker-compose -f generated/docker-compose.yml up -d --force-recreate bedrock-1
```

Add aliases with a `bedrock-1` (etc.) chain entry and a `bedrock_model`
override — upstream Bedrock model IDs don't match the other providers' names.

## Caveats

All subscription providers here are ToS-gray: multi-seat routing (Copilot),
OAuth reuse outside the official client (ChatGPT), and SDK bridging (Claude —
actively enforced by Anthropic since early 2026). Keep traffic modest.
