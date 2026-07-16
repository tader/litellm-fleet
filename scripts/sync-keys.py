#!/usr/bin/env python3
"""Create/refresh one virtual key per project on the running router.

Idempotent: existing keys (matched by key_alias) are updated in place so their
model allowlist follows main.yaml; missing ones are created. Keys are written
to generated/keys.json.

Usage: uv run scripts/sync-keys.py
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def router_base(cfg: dict) -> str:
    port = cfg.get("router", {}).get("port", 4000)
    return f"http://127.0.0.1:{port}"


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip("\"'")
    return env


def api(base: str, path: str, master_key: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()
        e.msg = f"{e.msg}: {detail}" if detail else e.msg
        raise


def main() -> int:
    cfg = yaml.safe_load((ROOT / "main.yaml").read_text())
    base = router_base(cfg)
    env = load_env(ROOT / "generated" / ".env")
    master_key = env["LITELLM_MASTER_KEY"]

    keys_path = ROOT / "generated" / "keys.json"
    keys = json.loads(keys_path.read_text()) if keys_path.exists() else {}

    projects = list(cfg["projects"])

    for project in projects:
        alias = f"project-{project}"
        models = [alias]  # access group name doubles as the models entry
        existing = keys.get(project)
        if existing:
            try:
                api(base, "/key/update", master_key,
                    {"key": existing, "models": models})
                print(f"{project}: updated ({existing[:12]}...)")
                continue
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                print(f"{project}: stored key gone upstream, regenerating")
        # No usable stored key. The alias may already exist upstream (from an
        # earlier sync whose plaintext we never recorded), and aliases must be
        # unique — so delete by alias first, then create fresh. Idempotent:
        # deleting a nonexistent alias is a no-op.
        api(base, "/key/delete", master_key, {"key_aliases": [alias]})
        resp = api(base, "/key/generate", master_key,
                   {"key_alias": alias, "models": models})
        keys[project] = resp["key"]
        print(f"{project}: created ({resp['key'][:12]}...)")

    # Prune keys for projects no longer in main.yaml (e.g. renamed/removed).
    for stale in [p for p in keys if p not in projects]:
        alias = f"project-{stale}"
        try:
            api(base, "/key/delete", master_key, {"key_aliases": [alias]})
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        del keys[stale]
        print(f"{stale}: pruned (not in main.yaml)")

    keys_path.write_text(json.dumps(keys, indent=2) + "\n")
    print(f"wrote {keys_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
