#!/usr/bin/env bash
# claude-fleet.sh (ccf) — launch Claude Code against the litellm-fleet router.
#
# Picks a fleet project (virtual key) and default model, persists them in the
# working project's .claude/settings.local.json, then execs `claude`.
#
# Usage:
#   ccf                  # first run: pick project+model, save, launch claude
#                        # later runs: fast path, just launch claude
#   ccf -n               # configure only, don't launch
#   ccf -p [project]     # (re)pick project (picker if no value)
#   ccf -m [model]       # (re)pick model (picker if no value)
#   ccf -- --resume ...  # everything after flags is passed to claude
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v realpath >/dev/null 2>&1; then
  SCRIPT_PATH="$(realpath "$SCRIPT_PATH")"
elif command -v python3 >/dev/null 2>&1; then
  SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$SCRIPT_PATH")"
else
  echo "ccf: missing dependency: realpath (or python3 for fallback)" >&2
  exit 1
fi
FLEET_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
KEYS_JSON="$FLEET_ROOT/generated/keys.json"
SETTINGS=".claude/settings.local.json"

die() { echo "ccf: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }
need jq; need gum; need curl; need claude

# Router base URL: $CCF_BASE_URL wins; else main.yaml router.port, else 4000
# (fallback matches sync-keys.py's BASE = http://127.0.0.1:4000).
port="$(sed -n 's/^[[:space:]]*port:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
  "$FLEET_ROOT/main.yaml" 2>/dev/null | tail -1)"
BASE_URL="${CCF_BASE_URL:-http://127.0.0.1:${port:-4000}}"

# --- flag parsing -----------------------------------------------------------
launch=1 pick=0 project="" model=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--no-launch) launch=0; shift ;;
    -p|--project) pick=1; [[ "${2:-}" != "" && "${2:-}" != -* ]] && { project="$2"; shift; }; shift ;;
    -m|--model)   pick=1; [[ "${2:-}" != "" && "${2:-}" != -* ]] && { model="$2"; shift; }; shift ;;
    -c|--choose)  pick=1; shift ;;
    -h|--help)    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
# remaining "$@" is passed through to claude

# --- fast path: already configured for the fleet, nothing to (re)pick -------
if [[ $pick -eq 0 && -f "$SETTINGS" ]] \
   && jq -e --arg u "$BASE_URL" '.env.ANTHROPIC_BASE_URL == $u' "$SETTINGS" >/dev/null 2>&1; then
  if [[ $launch -eq 1 ]]; then
    echo "ccf: using existing $SETTINGS ($(jq -r '.env.ANTHROPIC_MODEL' "$SETTINGS")) — re-pick with: ccf -c"
    exec claude "$@"
  fi
  echo "ccf: already configured ($SETTINGS); re-pick with: ccf -c"
  exit 0
fi

# --- project pick ------------------------------------------------------------
[[ -f "$KEYS_JSON" ]] || die "no $KEYS_JSON — run: (cd $FLEET_ROOT && uv run scripts/sync-keys.py)"
if [[ -z "$project" ]]; then
  project="$(jq -r 'keys[]' "$KEYS_JSON" | gum choose --header "fleet project (customer-approved providers)")" \
    || die "no project chosen"
fi
key="$(jq -r --arg p "$project" '.[$p] // empty' "$KEYS_JSON")"
[[ -n "$key" ]] || die "project '$project' not in keys.json — run: (cd $FLEET_ROOT && uv run scripts/sync-keys.py)"

# --- model pick (router /v1/models is the authoritative allowlist) -----------
models="$(curl -sf --max-time 5 "$BASE_URL/v1/models" -H "Authorization: Bearer $key" \
  | jq -r '.data[].id' | sort)" \
  || die "fleet not running at $BASE_URL — start via menu bar or: docker-compose -f $FLEET_ROOT/generated/docker-compose.yml up -d"
[[ -n "$models" ]] || die "key for '$project' sees no models — run sync-keys.py?"

if [[ -n "$model" ]]; then
  grep -qxF "$model" <<<"$models" || die "model '$model' not available to project '$project'. Available:"$'\n'"$models"
else
  model="$(gum choose --header "default model for project '$project'" <<<"$models")" \
    || die "no model chosen"
fi

# --- haiku/background model: haiku alias visible to this key, else local ----
haiku="$(grep -E '(^|[-_/])haiku' <<<"$models" | head -1 || true)"
[[ -n "$haiku" ]] || haiku="$(grep -qxF local <<<"$models" && echo local || echo "$model")"

# --- write settings.local.json (merge, preserve unrelated keys) ---------------
mkdir -p .claude
existing="{}"
[[ -f "$SETTINGS" ]] && existing="$(jq -c '.' "$SETTINGS" 2>/dev/null || echo '{}')"
jq -n --argjson base "$existing" \
      --arg url "$BASE_URL" --arg key "$key" --arg model "$model" --arg haiku "$haiku" '
  $base * {env: (($base.env // {}) + {
    ANTHROPIC_BASE_URL: $url,
    ANTHROPIC_AUTH_TOKEN: $key,
    ANTHROPIC_MODEL: $model,
    ANTHROPIC_DEFAULT_HAIKU_MODEL: $haiku
  })}' > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"

# --- gitignore safety: keep the key out of the repo ---------------------------
if git rev-parse --git-dir >/dev/null 2>&1 \
   && ! git check-ignore -q "$SETTINGS" 2>/dev/null; then
  echo "$SETTINGS" >> "$(git rev-parse --git-dir)/info/exclude"
  echo "ccf: added $SETTINGS to .git/info/exclude (contains fleet key)"
fi

echo "ccf: project=$project model=$model haiku=$haiku -> $SETTINGS"
[[ $launch -eq 1 ]] && exec claude "$@"
exit 0
