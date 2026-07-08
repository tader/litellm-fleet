# Pointing coding clients at the router

The fleet exposes an **OpenAI-compatible** API at `http://127.0.0.1:4000/v1` and,
because LiteLLM also implements the Anthropic Messages spec, an
**Anthropic-compatible** endpoint at `http://127.0.0.1:4000/v1/messages`. Any
client that lets you override the base URL and key can therefore use the whole
fleet — priority failover, cooldowns, and LMStudio fallback included.

## What to fill in everywhere

| Field | Value |
|---|---|
| OpenAI base URL | `http://127.0.0.1:4000/v1` |
| Anthropic base URL (Claude Code only) | `http://127.0.0.1:4000` (client appends `/v1/messages`) |
| API key | a virtual key from `generated/keys.json`, or the master key |
| Model names | `codex`, `copilot`, `claude`, `local` (restricted: `secretproj/codex`, …) |

Grab a key:

```sh
# per-project virtual key (recommended; scoped to that project's models)
python3 -c "import json; print(json.load(open('generated/keys.json'))['default'])"
# or the master key (menu bar → Copy Master Key, or:)
grep '^LITELLM_MASTER_KEY=' generated/.env | cut -d= -f2
```

`codex` / `copilot` / `claude` are just aliases whose `chain` in `main.yaml`
decides which account serves the request; `local` is the never-benched LMStudio
fallback. Pick the alias, not a real upstream model name.

---

## Claude Code (CLI)

Claude Code speaks the Anthropic Messages API, and LiteLLM translates
`/v1/messages` onto whatever backend the alias resolves to — so you can point it
at *any* model in the fleet, not only `claude`.

**Quick start — `ccf`** (`scripts/claude-fleet.sh`, symlinked as `~/.local/bin/ccf`):
run `ccf` in any working directory to pick a fleet project + default model
(project list from `generated/keys.json`, models from the router's `/v1/models`
for that key), persist them in that directory's `.claude/settings.local.json`,
and launch `claude`. Later runs launch directly; `ccf -c` re-picks,
`ccf -n` configures without launching. The manual equivalent:

`~/.claude/settings.json` (or a project `.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
    "ANTHROPIC_AUTH_TOKEN": "<key from keys.json or master key>",
    "ANTHROPIC_MODEL": "codex",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "local"
  }
}
```

- Use `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer …`), **not**
  `ANTHROPIC_API_KEY` — leave the latter unset so Claude Code doesn't fall back
  to Anthropic's own auth.
- `ANTHROPIC_BASE_URL` is the **host root**, without `/v1` — Claude Code appends
  `/v1/messages` itself.
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` is the small background model (titles,
  summaries); point it at `local` to keep those off your paid subscriptions.
  (The old `ANTHROPIC_SMALL_FAST_MODEL` is deprecated.)
- Set `ANTHROPIC_MODEL` to `claude` only if the Meridian bridge account is
  enabled; otherwise use `codex`/`copilot`/`local`.

Same as shell env if you prefer: `export ANTHROPIC_BASE_URL=… ANTHROPIC_AUTH_TOKEN=…`.

**Desktop:** the consumer **Claude Desktop** app does **not** support a custom
API base URL — it only does the official account login (plus MCP). No way to
route it at the fleet. CLI only.

---

## Codex (CLI + IDE extension)

`~/.codex/config.toml` (must be the **user-level** file — Codex ignores
`model_providers` in a project-local `.codex/config.toml`):

```toml
model = "codex"              # a fleet alias
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM fleet"
base_url = "http://127.0.0.1:4000/v1"
env_key = "OPENAI_API_KEY"  # env var name holding the key
wire_api = "responses"      # only supported value as of 2026
```

```sh
export OPENAI_API_KEY="<key from keys.json or master key>"
codex                       # or: codex -m copilot
```

- **Caveat — Responses API:** current Codex only speaks the `/responses`
  protocol (Chat Completions support was removed ~Feb 2026). LiteLLM exposes
  `/v1/responses` and bridges it onto the fleet's chat-completions backends, so
  this works — but if you get a 404 on `/responses`, upgrade the `litellm` image.
  `drop_params: true` is already set in the generated `router.yaml`.
- **IDE extension** (VS Code / Cursor, marketplace `openai.chatgpt`) shares the
  same `~/.codex/config.toml` — configure it exactly as above; nothing goes in
  editor settings.
- **Desktop:** no standalone app; the CLI and IDE extension are the only surfaces.

---

## OpenCode (TUI/CLI + Desktop beta)

`~/.config/opencode/opencode.json` (global) or `opencode.json` in a project root
(project overrides global):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "litellm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LiteLLM fleet",
      "options": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "{env:LITELLM_API_KEY}"
      },
      "models": {
        "codex":   { "name": "Codex" },
        "copilot": { "name": "Copilot" },
        "claude":  { "name": "Claude" },
        "local":   { "name": "Local" }
      }
    }
  },
  "model": "litellm/codex"
}
```

```sh
export LITELLM_API_KEY="<key from keys.json or master key>"
opencode                     # or: opencode --model litellm/copilot
```

- `npm: "@ai-sdk/openai-compatible"` is the right package for the fleet's
  chat-completions endpoint; `baseURL` keeps the `/v1` suffix.
- Runtime model switch: `/models` in the TUI, or `--model litellm/<alias>`.
- Optional `small_model` key mirrors Claude Code's haiku slot — set it to
  `litellm/local`.
- **Desktop:** an official desktop app is in beta and reads the same
  `opencode.json`; the terminal TUI is the mature surface.

---

## GitHub Copilot — partial (chat only, not completions)

Copilot is the odd one out: it is a *backend* in this fleet, and as a *client* it
can only be pointed at a custom endpoint for **chat/agent** requests via BYOK —
**not** inline code completions.

- **VS Code:** Command Palette → **Chat: Manage Language Models** → **Add
  Models** → **OpenAI Compatible / Custom Endpoint**. Base URL
  `http://127.0.0.1:4000/v1`, paste a key, add model IDs `codex` / `copilot` /
  `claude` / `local`. They then appear in the Copilot Chat model picker. (Stored
  under `github.copilot.chat.customOAIModels` / VS Code's `chatLanguageModels.json`.)
- **Copilot CLI / SDK:** BYOK via a custom provider with a `bearerToken` and a
  base URL ending in `/v1`.
- **Hard limitation:** BYOK covers **chat and agent tasks only**. Inline
  suggestions (ghost text), embeddings, and semantic search still go through
  GitHub's own backend and require a Copilot plan — there is no supported way to
  route the autocomplete engine at the fleet. For self-hosted inline completion
  use a different extension (Continue, Cline, an Ollama-compatible completer).
- **Desktop:** "Copilot desktop" = the VS Code / JetBrains extensions above;
  same BYOK constraints.

---

## Quick check

```sh
curl -s http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer <key>" -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","input":"ping"}'
```
