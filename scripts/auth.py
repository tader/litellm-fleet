#!/usr/bin/env python3
"""Interactive auth helper for the LiteLLM fleet.

Guides first-time users through authenticating every configured provider:

- lists all accounts from main.yaml with live auth status (decoding JWTs to
  show which identity a token belongs to)
- drives OAuth device flows: extracts the URL + user code from the sidecar's
  container logs, warns when the code is stale, and offers a container
  restart + re-trigger to mint a fresh one
- discovers existing credentials on the filesystem (~/.codex/auth.json,
  ~/.config/github-copilot/{apps,hosts}.json, ~/.claude/.credentials.json)
  and offers to import them instead
- supports pasting tokens manually (claude setup-token, Bedrock API keys)
- can wipe an account's auth first when a previous attempt went sideways

Usage: uv run scripts/auth.py
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

ROOT = Path(__file__).parent.parent
TOKENS = ROOT / "tokens"
COMPOSE_FILE = ROOT / "generated" / "docker-compose.yml"

DEVICE_CODE_TTL = 15 * 60  # GitHub/OpenAI device codes expire after ~15 min
TRIGGER_MODEL = {"github_copilot": "gpt-5", "chatgpt": "gpt-5.3-codex"}

console = Console()


# ---------------------------------------------------------------- utilities

def compose_cmd() -> list[str]:
    if shutil.which("docker-compose"):
        return ["docker-compose", "-f", str(COMPOSE_FILE)]
    return ["docker", "compose", "-f", str(COMPOSE_FILE)]


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying (we only read claims)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and bool(jwt_claims(token))


def jwt_identity(token: str) -> str | None:
    """Best-effort 'who is this' from JWT claims."""
    claims = jwt_claims(token)
    if not claims:
        return None
    for key in ("email", "preferred_username", "upn", "sub"):
        val = claims.get(key)
        if isinstance(val, str) and val:
            return val
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
        return f"account {auth['chatgpt_account_id']}"
    return None


def fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fmt_expiry(exp: int | None) -> str:
    if not isinstance(exp, (int, float)):
        return ""
    delta = exp - time.time()
    if delta < 0:
        return f"[red]expired {fmt_age(-delta)} ago[/]"
    return f"[green]expires in {fmt_age(delta)}[/]"


def mask(token: str, keep: int = 8) -> str:
    token = token.strip()
    return token[:keep] + "…" if len(token) > keep else token


def write_token(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


# ---------------------------------------------------------------- accounts

@dataclass
class Account:
    name: str
    type: str
    port: int | None
    enabled: bool
    state: str = "unknown"       # colored short status
    identity: str = ""           # decoded account/email if known
    expiry: str = ""
    files: list[Path] = field(default_factory=list)

    @property
    def token_dir(self) -> Path:
        return TOKENS / self.name


def load_accounts() -> list[Account]:
    cfg = yaml.safe_load((ROOT / "main.yaml").read_text())
    accounts = []
    for name, a in cfg["accounts"].items():
        if a["type"] == "lm_studio":
            continue  # no auth needed
        accounts.append(Account(
            name=name, type=a["type"], port=a.get("port"),
            enabled=a.get("enabled", True),
        ))
    for acct in accounts:
        refresh_status(acct)
    return accounts


def refresh_status(acct: Account) -> None:
    acct.state, acct.identity, acct.expiry = "[dim]no auth[/]", "", ""
    acct.files = [p for p in (acct.token_dir.glob("*") if acct.token_dir.exists() else []) if p.is_file()]

    if acct.type == "chatgpt":
        _chatgpt_status(acct)
    elif acct.type == "github_copilot":
        _copilot_status(acct)
    elif acct.type in ("claude_code_bridge", "bedrock"):
        _token_file_status(acct)


def _chatgpt_status(acct: Account) -> None:
    path = acct.token_dir / "auth.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        acct.state = "[red]unreadable[/]"
        return
    if data.get("access_token"):
        exp = data.get("expires_at") or jwt_claims(data["access_token"]).get("exp")
        expired = isinstance(exp, (int, float)) and exp < time.time()
        # expired access token is fine while the refresh_token works
        acct.state = "[yellow]authed (stale)[/]" if expired and not data.get("refresh_token") \
            else "[green]authed[/]"
        acct.identity = (jwt_identity(data.get("id_token", ""))
                         or jwt_identity(data["access_token"])
                         or (f"account {data['account_id']}" if data.get("account_id") else ""))
        acct.expiry = fmt_expiry(exp)
    elif "device_code_requested_at" in data:
        age = time.time() - data["device_code_requested_at"]
        color = "yellow" if age < DEVICE_CODE_TTL else "red"
        acct.state = f"[{color}]device flow pending ({fmt_age(age)} old)[/]"
    else:
        acct.state = "[yellow]partial auth[/]"


def _copilot_status(acct: Account) -> None:
    access = acct.token_dir / "access-token"
    api_key = acct.token_dir / "api-key.json"
    if not access.exists() or not access.read_text().strip():
        return
    tok = access.read_text().strip()
    acct.state = "[green]authed[/]"
    acct.identity = jwt_identity(tok) or f"GitHub token {mask(tok)}"
    # The GitHub OAuth token in access-token is long-lived; api-key.json is a
    # short-lived Copilot API key that LiteLLM automatically re-mints from it
    # on the next request. A past expiry there is normal (idle sidecar), not
    # an auth problem — communicate it as informational, never as an error.
    if not api_key.exists():
        acct.expiry = "[dim]api key minted on first request[/]"
        return
    try:
        data = json.loads(api_key.read_text())
        exp = data.get("expires_at")
        if not exp:
            m = re.search(r"exp=(\d+)", str(data.get("token", "")))
            exp = int(m.group(1)) if m else None
        if isinstance(exp, (int, float)):
            if exp < time.time():
                acct.expiry = ("[dim]api key stale (auto-renews on next "
                               "request)[/]")
            else:
                acct.expiry = (f"[green]api key valid {fmt_age(exp - time.time())}[/] "
                               "[dim](auto-renews)[/]")
    except (ValueError, OSError):
        pass


def _token_file_status(acct: Account) -> None:
    path = acct.token_dir / "token"
    if not path.exists():
        return
    tok = path.read_text().strip()
    if not tok:
        acct.state = "[yellow]token file empty[/]"
        return
    acct.state = "[green]token present[/]"
    if looks_like_jwt(tok):
        acct.identity = jwt_identity(tok) or ""
        acct.expiry = fmt_expiry(jwt_claims(tok).get("exp"))
    else:
        acct.identity = mask(tok)


# ------------------------------------------------- filesystem auth discovery

@dataclass
class FoundAuth:
    label: str          # where it came from + identity
    apply: object       # callable(acct) -> str message


def discover_chatgpt() -> list[FoundAuth]:
    found = []
    source = Path.home() / ".codex" / "auth.json"
    if source.exists():
        try:
            raw = json.loads(source.read_text())
            tok = raw.get("tokens", raw)
            ident = jwt_identity(tok.get("id_token", "")) or "unknown identity"
            if tok.get("access_token") and tok.get("refresh_token"):
                found.append(FoundAuth(
                    label=f"Codex CLI session at {source} ({ident})",
                    apply=lambda acct, src=source: _import_codex(acct, src),
                ))
        except (ValueError, OSError):
            pass
    return found


def _import_codex(acct: Account, source: Path) -> str:
    r = subprocess.run(
        ["uv", "run", "scripts/codex-import.py", "--source", str(source),
         "--account", acct.name, "--no-restart"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"[red]import failed:[/] {(r.stdout + r.stderr).strip()}"
    return f"[green]imported Codex session[/] → tokens/{acct.name}/auth.json"


def discover_copilot() -> list[FoundAuth]:
    found = []
    cfg_dir = Path.home() / ".config" / "github-copilot"
    for fname in ("apps.json", "hosts.json"):
        path = cfg_dir / fname
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for key, entry in data.items():
            token = entry.get("oauth_token") if isinstance(entry, dict) else None
            if not token:
                continue
            user = entry.get("user", "unknown user")
            found.append(FoundAuth(
                label=f"{path.name} entry {key} (user: {user})",
                apply=lambda acct, t=token: _import_copilot_token(acct, t),
            ))
    return found


def _import_copilot_token(acct: Account, token: str) -> str:
    write_token(acct.token_dir / "access-token", token)
    # stale api-key.json would pin the old identity; force re-mint
    (acct.token_dir / "api-key.json").unlink(missing_ok=True)
    return f"[green]wrote[/] tokens/{acct.name}/access-token (api-key.json cleared)"


def discover_claude() -> list[FoundAuth]:
    found = []
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.exists():
        try:
            oauth = json.loads(creds.read_text()).get("claudeAiOauth", {})
            if oauth.get("accessToken"):
                sub = oauth.get("subscriptionType", "?")
                exp = oauth.get("expiresAt")
                exp_s = fmt_expiry(exp / 1000 if isinstance(exp, (int, float)) and exp > 1e11 else exp)
                found.append(FoundAuth(
                    label=f"Claude Code login at {creds} ({sub}) {exp_s}\n"
                          "    [dim]note: the bridge needs a long-lived token from "
                          "`claude setup-token`; this session token may expire soon[/]",
                    apply=lambda acct, t=oauth["accessToken"]: _write_raw_token(acct, t),
                ))
        except (ValueError, OSError):
            pass
    return found


def _write_raw_token(acct: Account, token: str) -> str:
    write_token(acct.token_dir / "token", token.strip() + "\n")
    return f"[green]wrote[/] tokens/{acct.name}/token"


DISCOVERERS = {
    "chatgpt": discover_chatgpt,
    "github_copilot": discover_copilot,
    "claude_code_bridge": discover_claude,
}


# --------------------------------------------------------- device-flow logic

DEVICE_URL_RE = re.compile(r"https?://\S*(?:device|verify|activate)\S*", re.I)
DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6}|[A-Z0-9]{6,10})\b")


def container_device_prompt(acct: Account) -> tuple[str, str, float] | None:
    """Extract (url, code, age_seconds) of the latest device prompt in logs."""
    r = subprocess.run(
        compose_cmd() + ["logs", "--no-color", "--timestamps", "--tail", "400", acct.name],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    url = code = ts = None
    for line in r.stdout.splitlines():
        m_url = DEVICE_URL_RE.search(line)
        if m_url:
            url = m_url.group(0).rstrip(".,)")
            ts = _log_timestamp(line)
            # code is usually on the same or a nearby line
            m_code = DEVICE_CODE_RE.search(line.split(url)[-1]) or DEVICE_CODE_RE.search(line)
            if m_code:
                code = m_code.group(1)
        elif url and not code:
            m_code = re.search(r"code[:\s]+([A-Z0-9-]{6,12})", line, re.I)
            if m_code:
                code = m_code.group(1)
    if not url:
        return None
    age = time.time() - ts if ts else float("inf")
    return url, code or "(see logs)", age


def _log_timestamp(line: str) -> float | None:
    m = re.search(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
        line,
    )
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def trigger_request(acct: Account) -> None:
    """Fire one tiny request so LiteLLM starts the device flow (blocks until auth)."""
    if acct.port is None:
        console.print(
            f"[red]{acct.name} has no port configured in main.yaml; cannot trigger device flow.[/]"
        )
        return
    body = json.dumps({
        "model": TRIGGER_MODEL.get(acct.type, "gpt-5"),
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }).encode()

    def _fire():
        req = urllib.request.Request(
            f"http://127.0.0.1:{acct.port}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=DEVICE_CODE_TTL).read()
        except Exception:
            pass  # errors surface via the status table / logs

    threading.Thread(target=_fire, daemon=True).start()


def run_device_flow(acct: Account) -> None:
    if not COMPOSE_FILE.exists():
        console.print("[red]generated/docker-compose.yml missing — run `uv run generate.py` first[/]")
        return
    subprocess.run(compose_cmd() + ["up", "-d", acct.name], cwd=ROOT)

    prompt = container_device_prompt(acct)
    if prompt and prompt[2] > DEVICE_CODE_TTL:
        console.print(f"[yellow]found a device prompt but it's {fmt_age(prompt[2])} old — "
                      f"codes expire after ~15m.[/]")
        if Confirm.ask(f"Restart {acct.name} to get a fresh code?", default=True):
            subprocess.run(compose_cmd() + ["restart", acct.name], cwd=ROOT)
            prompt = None

    if not prompt:
        console.print("[cyan]sending a trigger request to start the device flow…[/]")
        trigger_request(acct)
        with console.status("waiting for the device prompt to appear in the logs…"):
            for _ in range(30):
                time.sleep(2)
                prompt = container_device_prompt(acct)
                if prompt and prompt[2] < DEVICE_CODE_TTL:
                    break

    if not prompt:
        console.print(f"[red]no device prompt found.[/] Inspect manually:\n"
                      f"  {' '.join(compose_cmd())} logs -f {acct.name}")
        return

    url, code, age = prompt
    console.print(Panel(
        f"[bold]1.[/] Open   [bold cyan]{url}[/]\n"
        f"[bold]2.[/] Enter  [bold yellow]{code}[/]   [dim](prompt is {fmt_age(age)} old)[/]",
        title=f"device flow — {acct.name}", border_style="cyan",
    ))
    with console.status("waiting for you to complete the login in the browser…"):
        deadline = time.time() + DEVICE_CODE_TTL
        while time.time() < deadline:
            time.sleep(5)
            refresh_status(acct)
            if "green" in acct.state:
                break
    refresh_status(acct)
    if "green" in acct.state:
        console.print(f"[green]✓ {acct.name} authenticated[/] {acct.identity}")
    else:
        console.print(f"[yellow]still not authed — current status: {acct.state}[/]. "
                      "Complete the browser login, then re-check from the menu.")


# ------------------------------------------------------------------ actions

def delete_auth(acct: Account) -> None:
    files = acct.files
    if not files:
        console.print("[dim]nothing to delete[/]")
        return
    console.print("will delete:")
    for f in files:
        console.print(f"  [red]{f.relative_to(ROOT)}[/]")
    if Confirm.ask("Delete these credential files?", default=False):
        for f in files:
            f.unlink()
        console.print("[green]deleted.[/] The account is now unauthenticated.")
        restart_container(acct, ask=True)


def restart_container(acct: Account, ask: bool = False) -> None:
    if not COMPOSE_FILE.exists():
        return
    if ask and not Confirm.ask(f"Restart the {acct.name} container now?", default=True):
        return
    subprocess.run(compose_cmd() + ["restart", acct.name], cwd=ROOT)


def paste_token(acct: Account) -> None:
    hint = {
        "claude_code_bridge": "run [bold]claude setup-token[/] in a terminal and paste the resulting token",
        "bedrock": "paste this account's long-term Bedrock API key",
    }.get(acct.type, "paste the token")
    console.print(f"[cyan]{hint}[/]")
    tok = Prompt.ask("token", password=True).strip()
    if not tok:
        console.print("[yellow]empty — nothing written[/]")
        return
    if looks_like_jwt(tok):
        ident = jwt_identity(tok)
        exp = fmt_expiry(jwt_claims(tok).get("exp"))
        console.print(f"decoded JWT: [bold]{ident or 'no identity claim'}[/] {exp}")
        if not Confirm.ask("Use this token?", default=True):
            return
    console.print(_write_raw_token(acct, tok))
    restart_container(acct, ask=True)


def import_from_filesystem(acct: Account) -> None:
    finder = DISCOVERERS.get(acct.type)
    found = finder() if finder else []
    if not found:
        console.print(f"[dim]no local credentials found for type {acct.type}[/]")
        return
    for i, f in enumerate(found, 1):
        console.print(f"  [bold]{i}[/]) {f.label}")
    console.print(f"  [bold]0[/]) cancel")
    idx = IntPrompt.ask("import which?", default=1)
    if not (1 <= idx <= len(found)):
        return
    console.print(found[idx - 1].apply(acct))
    restart_container(acct, ask=True)


def account_menu(acct: Account) -> None:
    while True:
        refresh_status(acct)
        actions: list[tuple[str, object]] = []
        if acct.type in ("github_copilot", "chatgpt"):
            actions.append(("OAuth device flow (guided, via container logs)", run_device_flow))
        if acct.type in DISCOVERERS:
            actions.append(("import credentials found on this machine", import_from_filesystem))
        if acct.type in ("claude_code_bridge", "bedrock"):
            actions.append(("paste a token manually", paste_token))
        actions.append(("delete current auth (start over)", delete_auth))
        actions.append(("restart container", lambda a: restart_container(a)))

        console.print(Panel(
            f"type [bold]{acct.type}[/]   port [bold]{acct.port}[/]   "
            f"status {acct.state}"
            + (f"   [bold]{acct.identity}[/]" if acct.identity else "")
            + (f"   {acct.expiry}" if acct.expiry else ""),
            title=f"[bold]{acct.name}[/]", border_style="blue",
        ))
        for i, (label, _) in enumerate(actions, 1):
            console.print(f"  [bold]{i}[/]) {label}")
        console.print("  [bold]0[/]) back")
        choice = IntPrompt.ask("action", default=0)
        if choice == 0:
            return
        if 1 <= choice <= len(actions):
            actions[choice - 1][1](acct)


# --------------------------------------------------------------------- main

def status_table(accounts: list[Account]) -> Table:
    table = Table(title="LiteLLM fleet auth status", header_style="bold cyan")
    table.add_column("#", justify="right", style="bold")
    table.add_column("account")
    table.add_column("type", style="dim")
    table.add_column("status")
    table.add_column("identity")
    table.add_column("expiry")
    for i, a in enumerate(accounts, 1):
        name = a.name if a.enabled else f"[dim]{a.name} (disabled)[/]"
        table.add_row(str(i), name, a.type, a.state, a.identity, a.expiry)
    return table


def main() -> int:
    accounts = load_accounts()
    if not accounts:
        console.print("[red]no auth-bearing accounts in main.yaml[/]")
        return 1
    console.print(Panel(
        "Pick an account to set up or renew its auth. Status refreshes on "
        "every visit; [bold]q[/] quits.",
        title="[bold]auth helper[/]", border_style="cyan",
    ))
    while True:
        for a in accounts:
            refresh_status(a)
        console.print(status_table(accounts))
        raw = Prompt.ask(f"account [1-{len(accounts)}, q to quit]", default="q").strip().lower()
        if raw in ("q", "quit", "exit", "0"):
            return 0
        if raw.isdigit() and 1 <= int(raw) <= len(accounts):
            account_menu(accounts[int(raw) - 1])
        else:
            console.print("[yellow]invalid selection[/]")


if __name__ == "__main__":
    raise SystemExit(main())
