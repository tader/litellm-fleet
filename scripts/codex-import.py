#!/usr/bin/env python3
"""Import a Codex CLI OAuth session into a chatgpt sidecar's token cache.

For ChatGPT accounts that block the device flow LiteLLM triggers, log in with
the official Codex CLI instead (authorization-code + PKCE, browser redirect to
http://localhost:1455 -- not a device code):

    codex login            # writes ~/.codex/auth.json

then run this to transplant that session into a sidecar. The refresh_token is
interchangeable between the two flows (same OAuth client), so LiteLLM keeps the
session alive from there without any further login.

Usage:
    uv run scripts/codex-import.py [--source ~/.codex/auth.json]
                                   [--account chatgpt-1] [--no-restart]

With no --account, prints a menu of every chatgpt target to choose from.
"""

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
DEFAULT_SOURCE = Path.home() / ".codex" / "auth.json"


def chatgpt_accounts(cfg: dict) -> list[str]:
    return [
        name
        for name, a in cfg["accounts"].items()
        if a.get("type") == "chatgpt"
    ]


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying (we only read claims)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def token_status(account: str) -> str:
    """One-word state of a sidecar's current token cache, for the menu."""
    path = ROOT / "tokens" / account / "auth.json"
    if not path.exists():
        return "empty"
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return "unreadable"
    if data.get("access_token"):
        exp = data.get("expires_at")
        if isinstance(exp, int) and exp < time.time():
            return "authed (expired)"
        return "authed"
    if "device_code_requested_at" in data:
        return "pending device flow"
    return "unknown"


def load_codex_session(source: Path) -> dict:
    """Map a Codex CLI auth.json onto LiteLLM's flat chatgpt token schema."""
    raw = json.loads(source.read_text())
    tok = raw.get("tokens", raw)  # codex nests under "tokens"; tolerate flat too

    access = tok.get("access_token")
    refresh = tok.get("refresh_token")
    id_token = tok.get("id_token")
    if not (access and refresh and id_token):
        missing = [
            k for k in ("access_token", "refresh_token", "id_token")
            if not tok.get(k)
        ]
        raise SystemExit(f"{source}: missing field(s): {', '.join(missing)}")

    id_claims = jwt_claims(id_token)
    account_id = (
        tok.get("account_id")
        or id_claims.get("chatgpt_account_id")
        or id_claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    )
    if not account_id:
        raise SystemExit(
            f"{source}: could not find account_id (checked tokens.account_id "
            "and id_token claims)"
        )

    # expires_at tracks the access token; fall back to the id_token's exp.
    expires_at = jwt_claims(access).get("exp") or id_claims.get("exp")
    if not expires_at:
        raise SystemExit(f"{source}: no exp claim in access_token or id_token")

    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "expires_at": int(expires_at),
        "account_id": account_id,
    }


def choose_account(accounts: list[str]) -> str:
    print("chatgpt targets:")
    for i, name in enumerate(accounts, 1):
        print(f"  {i}) {name:12s} [{token_status(name)}]")
    while True:
        raw = input(f"select destination [1-{len(accounts)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(accounts):
            return accounts[int(raw) - 1]
        print("invalid selection")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"Codex CLI auth.json (default: {DEFAULT_SOURCE})")
    ap.add_argument("--account", help="destination sidecar (skips the menu)")
    ap.add_argument("--no-restart", action="store_true",
                    help="don't restart the sidecar after import")
    args = ap.parse_args()

    if not args.source.exists():
        raise SystemExit(
            f"{args.source}: not found. Run `codex login` first, or pass "
            "--source."
        )

    cfg = yaml.safe_load((ROOT / "main.yaml").read_text())
    accounts = chatgpt_accounts(cfg)
    if not accounts:
        raise SystemExit("no chatgpt accounts in main.yaml")

    if args.account:
        if args.account not in accounts:
            raise SystemExit(
                f"{args.account}: not a chatgpt account "
                f"(choices: {', '.join(accounts)})"
            )
        account = args.account
    else:
        account = choose_account(accounts)

    session = load_codex_session(args.source)

    dest_dir = ROOT / "tokens" / account
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "auth.json"
    dest.write_text(json.dumps(session, indent=2) + "\n")
    dest.chmod(0o600)
    exp_in = int(session["expires_at"] - time.time())
    print(
        f"wrote {dest} (account_id {session['account_id']}, "
        f"access token valid ~{exp_in // 60}m)"
    )

    compose = ROOT / "generated" / "docker-compose.yml"
    if args.no_restart:
        print(f"restart when ready: docker-compose -f {compose} restart {account}")
        return 0
    if not compose.exists():
        print(f"note: {compose} not found; start the fleet, then restart {account}")
        return 0
    print(f"restarting {account}...")
    try:
        subprocess.run(
            ["docker-compose", "-f", str(compose), "restart", account],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"restart failed ({e}); run manually: "
              f"docker-compose -f {compose} restart {account}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
