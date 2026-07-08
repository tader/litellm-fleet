#!/usr/bin/env bash
# Interactive auth helper — see scripts/auth.py for the real thing.
# Kept as a wrapper so existing docs/muscle memory (`scripts/auth.sh`) work.
cd "$(dirname "$0")/.."
exec uv run scripts/auth.py "$@"
