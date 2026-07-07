#!/usr/bin/env bash
# First-run OAuth device flows for each subscription sidecar.
#
# LiteLLM's github_copilot/chatgpt providers trigger the device flow on the
# first request when no token is cached. This script starts the stack, sends
# one tiny request to each sidecar, and streams that sidecar's logs so you can
# see the "go to https://... and enter code XXXX" prompt. Tokens persist in
# ./tokens/<account>/ (mounted volumes), so this is one-time per account.
#
# Usage: scripts/auth.sh [account ...]   (default: all enabled sidecars)

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker-compose -f generated/docker-compose.yml)

port_for() {
  uv run python - "$1" <<'EOF'
import sys, yaml
acct = yaml.safe_load(open("main.yaml"))["accounts"][sys.argv[1]]
print(acct["port"])
EOF
}

model_for() {
  case "$1" in
    copilot-*) echo "gpt-5" ;;
    chatgpt-*) echo "gpt-5.3-codex" ;;
    *) echo "unknown account type: $1" >&2; exit 1 ;;
  esac
}

accounts=("$@")
if [ ${#accounts[@]} -eq 0 ]; then
  accounts=($(uv run python - <<'EOF'
import yaml
cfg = yaml.safe_load(open("main.yaml"))
for name, a in cfg["accounts"].items():
    if a.get("enabled", True) and a["type"] in ("github_copilot", "chatgpt"):
        print(name)
EOF
))
fi

"${COMPOSE[@]}" up -d "${accounts[@]}"

for acct in "${accounts[@]}"; do
  port=$(port_for "$acct")
  model=$(model_for "$acct")
  echo
  echo "=== $acct (port $port) ==="
  echo "Sending trigger request; watch below for a device-flow URL + code."
  echo "Press Ctrl-C once this account shows a successful response or you've"
  echo "completed the login, then re-run for the next account if needed."
  echo

  # Trigger in background; the request blocks until auth completes.
  curl -s -o /dev/null -X POST "http://127.0.0.1:${port}/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\": \"${model}\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}], \"max_tokens\": 1}" &
  curl_pid=$!

  "${COMPOSE[@]}" logs -f "$acct" &
  logs_pid=$!

  wait "$curl_pid" || true
  kill "$logs_pid" 2>/dev/null || true
  echo "=== $acct done ==="
done
