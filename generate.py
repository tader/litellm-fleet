#!/usr/bin/env python3
"""Generate the LiteLLM fleet (docker-compose, per-instance configs, .env) from main.yaml.

Usage: uv run generate.py [--check]
"""

import argparse
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
OUT = ROOT / "generated"
LITELLM_IMAGE = "ghcr.io/berriai/litellm:main-stable"
POSTGRES_IMAGE = "postgres:16-alpine"

# Env var each subscription provider uses to locate its OAuth token store.
TOKEN_DIR_ENV = {
    "github_copilot": "GITHUB_COPILOT_TOKEN_DIR",
    "chatgpt": "CHATGPT_TOKEN_DIR",
}
# Wildcard passthrough prefix per sidecar type.
WILDCARD_PREFIX = {
    "github_copilot": "github_copilot",
    "chatgpt": "chatgpt",
    "bedrock": "bedrock",
}


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def ensure_secrets(env: dict[str, str]) -> dict[str, str]:
    env.setdefault("LITELLM_MASTER_KEY", "sk-" + secrets.token_urlsafe(32))
    env.setdefault("POSTGRES_PASSWORD", secrets.token_urlsafe(24))
    return env


def sidecar_config(acct_type: str, pinned: dict[str, str], extra_params: dict | None = None) -> dict:
    prefix = WILDCARD_PREFIX[acct_type]
    extra_params = extra_params or {}
    model_list = [
        {
            "model_name": model,
            "litellm_params": {"model": f"{prefix}/{model}", **extra_params},
            "model_info": {"mode": mode},
        }
        for model, mode in pinned.items()
    ]
    model_list.append(
        {
            "model_name": "*",
            "litellm_params": {"model": f"{prefix}/*", **extra_params},
        }
    )
    return {
        "model_list": model_list,
        "litellm_settings": {"drop_params": True},
    }


def pinned_models(cfg: dict) -> dict[str, dict[str, str]]:
    """Per sidecar type: upstream models whose endpoint mode must be pinned.

    Models absent from litellm's registry default to mode=chat, which makes the
    sidecar bridge /v1/responses back to /chat/completions — upstreams that only
    serve /responses (e.g. Copilot's gpt-5.5) then reject the call. An alias with
    `endpoint: responses` pins mode=responses in each wildcard sidecar it routes to.
    """
    accts = enabled_accounts(cfg)
    pins: dict[str, dict[str, str]] = {t: {} for t in WILDCARD_PREFIX}
    for alias_name, alias in cfg["aliases"].items():
        endpoint = alias.get("endpoint", "chat")
        if endpoint == "chat":
            continue
        for account in alias["chain"]:
            acct_type = accts.get(account, {}).get("type")
            if acct_type not in pins:
                continue
            existing = pins[acct_type].get(alias["model"])
            if existing and existing != endpoint:
                sys.exit(f"alias {alias_name!r}: model {alias['model']!r} pinned to "
                         f"conflicting endpoints ({existing!r} vs {endpoint!r})")
            pins[acct_type][alias["model"]] = endpoint
    return pins


def enabled_accounts(cfg: dict) -> dict:
    return {
        name: acct
        for name, acct in cfg["accounts"].items()
        if acct.get("enabled", True)
    }


def sidecar_accounts(cfg: dict) -> dict:
    """Accounts that run as their own container (everything except lm_studio)."""
    return {
        name: acct
        for name, acct in enabled_accounts(cfg).items()
        if acct["type"] != "lm_studio"
    }


def deployment(cfg: dict, model_name: str, account: str, alias: dict, order: int,
               access_group: str) -> dict | None:
    accts = enabled_accounts(cfg)
    if account not in accts:
        return None
    acct = accts[account]
    if acct["type"] == "lm_studio":
        local_model = alias.get("local_model", cfg["local_model"])
        params = {
            "model": f"lm_studio/{local_model}",
            "api_base": cfg["lmstudio_base"],
            "api_key": "dummy",
            "order": order,
            "cooldown_time": 0,  # last resort: never bench it
        }
    else:
        default_port = 3456 if acct["type"] == "claude_code_bridge" else 4000
        internal_port = acct.get("internal_port", default_port)
        # An alias's `model` is often an internal/meridian name, not the real
        # upstream slug every account's catalog expects (e.g. Copilot's
        # "claude-sonnet-4.5" vs. the alias's "sonnet-4.6", or a Bedrock model
        # ID/inference-profile ARN) — model_overrides keys off account name.
        model = alias.get("model_overrides", {}).get(account, alias["model"])
        if acct["type"] == "claude_code_bridge":
            params = {
                "model": f"anthropic/{model}",
                # Meridian exposes the native Anthropic Messages endpoint at
                # /v1/messages; LiteLLM's Anthropic provider appends that path.
                "api_base": f"http://{account}:{internal_port}",
                "api_key": "dummy",
                "order": order,
            }
        else:
            params = {
                "model": f"openai/{model}",
                "api_base": f"http://{account}:{internal_port}/v1",
                "api_key": "dummy",
                "order": order,
            }
    return {
        "model_name": model_name,
        "litellm_params": params,
        "model_info": {"access_groups": [access_group]},
    }


def router_config(cfg: dict) -> dict:
    model_list = []
    for project, pconf in cfg["projects"].items():
        allowed = pconf["accounts"]
        access_group = f"project-{project}"
        for alias_name, alias in cfg["aliases"].items():
            chain = alias["chain"]
            if allowed != "all":
                chain = [a for a in chain if a in allowed]
            if not chain:
                continue
            model_name = alias_name if project == "default" else f"{project}/{alias_name}"
            order = 1
            for account in chain:
                dep = deployment(cfg, model_name, account, alias, order, access_group)
                if dep:
                    model_list.append(dep)
                    order += 1

    r = cfg.get("router", {})
    return {
        "model_list": model_list,
        "router_settings": {
            "num_retries": r.get("num_retries", 2),
            "allowed_fails": r.get("allowed_fails", 1),
            "cooldown_time": r.get("cooldown_time", 3600),
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
            "allow_requests_on_db_unavailable": True,
            "store_model_in_db": False,
        },
        "litellm_settings": {"drop_params": True},
    }


def compose_config(cfg: dict) -> dict:
    services = {
        "postgres": {
            "image": POSTGRES_IMAGE,
            "environment": {
                "POSTGRES_USER": "litellm",
                "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
                "POSTGRES_DB": "litellm",
            },
            "volumes": ["pgdata:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U litellm"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 10,
            },
            "restart": "unless-stopped",
        },
    }

    router_deps = {}
    for name, acct in sidecar_accounts(cfg).items():
        if acct["type"] == "claude_code_bridge":
            # Meridian (rynfar/meridian), built from vendor/ checkout.
            # Auth: `claude setup-token` -> paste into tokens/{name}/token.
            # Meridian only reads the OAuth seed from MERIDIAN_PROFILES (env,
            # JSON) — no file support — so build that JSON from the mounted
            # token file at container start, same tokens/ convention as the
            # other sidecars. The image entrypoint ends with `exec "$@"`, so
            # the command below runs as its args.
            internal_port = acct.get("internal_port", 3456)
            services[name] = {
                "build": {"context": "../vendor/meridian"},
                "environment": {
                    "MERIDIAN_DEFAULT_PROFILE": "main",
                    "MERIDIAN_PORT": str(internal_port),
                },
                "command": [
                    "sh", "-c",
                    'export MERIDIAN_PROFILES="[{\\"id\\":\\"main\\",'
                    '\\"oauthToken\\":\\"$(cat /tokens/token)\\"}]" && '
                    "exec ./bin/claude-proxy-supervisor.sh",
                ],
                "ports": [f"127.0.0.1:{acct['port']}:{internal_port}"],
                "volumes": [f"../tokens/{name}/token:/tokens/token:ro"],
                "restart": "unless-stopped",
            }
        elif acct["type"] == "bedrock":
            # Long-term Bedrock API key auth: no OAuth flow, just an
            # AWS_BEARER_TOKEN_BEDROCK env var (boto3-global, one value per
            # process) — read it from the token file at container start,
            # same tokens/ convention as the OAuth sidecars.
            internal_port = acct.get("internal_port", 4000)
            services[name] = {
                "image": LITELLM_IMAGE,
                # override the image's `litellm` entrypoint so `sh -c` runs instead
                "entrypoint": ["sh", "-c"],
                "command": [
                    "export AWS_BEARER_TOKEN_BEDROCK=$(cat /tokens/token) && "
                    f"exec litellm --config /config.yaml --port {internal_port}",
                ],
                "ports": [f"127.0.0.1:{acct['port']}:{internal_port}"],
                "volumes": [
                    f"./{name}.yaml:/config.yaml:ro",
                    f"../tokens/{name}/token:/tokens/token:ro",
                ],
                "restart": "unless-stopped",
            }
        else:
            internal_port = acct.get("internal_port", 4000)
            services[name] = {
                "image": LITELLM_IMAGE,
                "command": ["--config", "/config.yaml", "--port", str(internal_port)],
                "environment": {TOKEN_DIR_ENV[acct["type"]]: "/tokens"},
                "ports": [f"127.0.0.1:{acct['port']}:{internal_port}"],
                "volumes": [
                    f"./{name}.yaml:/config.yaml:ro",
                    f"../tokens/{name}:/tokens",
                ],
                "restart": "unless-stopped",
            }
        router_deps[name] = {"condition": "service_started"}

    router_deps["postgres"] = {"condition": "service_healthy"}
    router_port = cfg.get("router", {}).get("port", 4000)
    services["router"] = {
        "image": LITELLM_IMAGE,
        "command": ["--config", "/config.yaml", "--port", "4000"],
        "environment": {
            "LITELLM_MASTER_KEY": "${LITELLM_MASTER_KEY}",
            "DATABASE_URL": "postgresql://litellm:${POSTGRES_PASSWORD}@postgres:5432/litellm",
        },
        "ports": [f"127.0.0.1:{router_port}:4000"],
        "volumes": ["./router.yaml:/config.yaml:ro"],
        "depends_on": router_deps,
        "extra_hosts": ["host.docker.internal:host-gateway"],
        "restart": "unless-stopped",
    }

    return {"name": "litellm-fleet", "services": services, "volumes": {"pgdata": {}}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate main.yaml only")
    args = parser.parse_args()

    cfg = yaml.safe_load((ROOT / "main.yaml").read_text())

    # Validate account references in aliases and projects.
    known = set(cfg["accounts"])
    for alias_name, alias in cfg["aliases"].items():
        unknown = set(alias["chain"]) - known
        if unknown:
            sys.exit(f"alias {alias_name!r}: unknown accounts {sorted(unknown)}")
        endpoint = alias.get("endpoint", "chat")
        if endpoint not in ("chat", "responses"):
            sys.exit(f"alias {alias_name!r}: endpoint must be 'chat' or 'responses', "
                     f"got {endpoint!r}")
    for project, pconf in cfg["projects"].items():
        if pconf["accounts"] != "all":
            unknown = set(pconf["accounts"]) - known
            if unknown:
                sys.exit(f"project {project!r}: unknown accounts {sorted(unknown)}")
    for name, acct in sidecar_accounts(cfg).items():
        if acct["type"] == "claude_code_bridge" and not (ROOT / "vendor" / "meridian" / "Dockerfile").exists():
            sys.exit(f"account {name!r}: clone the bridge first: "
                     "git clone https://github.com/rynfar/meridian vendor/meridian")

    # Validate host ports: every sidecar needs one, and none may collide
    # (with each other or with the router's published port).
    router_port = cfg.get("router", {}).get("port", 4000)
    if not isinstance(router_port, int):
        sys.exit(f"router: port must be an integer, got {router_port!r}")
    used_ports: dict[int, str] = {router_port: "router"}
    for name, acct in sidecar_accounts(cfg).items():
        port = acct.get("port")
        if not isinstance(port, int):
            sys.exit(f"account {name!r}: integer `port` is required "
                     f"(got {port!r})")
        internal_port = acct.get("internal_port")
        if internal_port is not None and not isinstance(internal_port, int):
            sys.exit(f"account {name!r}: internal_port must be an integer "
                     f"(got {internal_port!r})")
        if port in used_ports:
            sys.exit(f"account {name!r}: port {port} already used by "
                     f"{used_ports[port]!r}")
        used_ports[port] = name

    if args.check:
        print("main.yaml OK")
        return 0

    OUT.mkdir(exist_ok=True)
    env = ensure_secrets(load_env(OUT / ".env"))
    (OUT / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    pins = pinned_models(cfg)
    for name, acct in sidecar_accounts(cfg).items():
        token_dir = ROOT / "tokens" / name
        token_dir.mkdir(parents=True, exist_ok=True)
        if acct["type"] in ("bedrock", "claude_code_bridge"):
            token_file = token_dir / "token"
            if not token_file.exists():
                token_file.write_text("")
                secret = ("long-term Bedrock API key" if acct["type"] == "bedrock"
                          else "`claude setup-token` OAuth token")
                print(f"tokens/{name}/token created empty — paste its {secret} "
                      f"into it, then restart the {name} container")
        if acct["type"] in WILDCARD_PREFIX:
            extra = {"aws_region_name": acct.get("region", "us-east-1")} if acct["type"] == "bedrock" else None
            (OUT / f"{name}.yaml").write_text(
                yaml.safe_dump(sidecar_config(acct["type"], pins[acct["type"]], extra),
                               sort_keys=False))

    (OUT / "router.yaml").write_text(yaml.safe_dump(router_config(cfg), sort_keys=False))
    (OUT / "docker-compose.yml").write_text(yaml.safe_dump(compose_config(cfg), sort_keys=False))

    n_sidecars = len(sidecar_accounts(cfg))
    n_models = len(router_config(cfg)["model_list"])
    print(f"generated/: docker-compose.yml ({n_sidecars} sidecars + postgres + router), "
          f"router.yaml ({n_models} deployments), .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
