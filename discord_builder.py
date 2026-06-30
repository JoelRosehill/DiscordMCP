#!/usr/bin/env python3
"""
Discord Server Builder — CLI
=============================
Generate a strict JSON server config with any AI, validate it, and apply it
to a real Discord server.

Setup:
    pip install -r requirements.txt
    echo "DISCORD_BOT_TOKEN=your-bot-token" >> .env
    echo "DISCORD_GUILD_ID=your-server-id"  >> .env

Usage:
    ./discord_builder.py                      interactive menu
    ./discord_builder.py prompt "description"  print the copyable AI prompt
    ./discord_builder.py validate config.json  check a config file
    ./discord_builder.py apply config.json     apply it to Discord
"""

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

console = Console()
ACCENT = "#5865F2"
GOOD = "#3ba55c"
BAD = "#ed4245"
WARN = "#faa61a"

# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
PERMS = {
    "CREATE_INSTANT_INVITE": 1 << 0, "KICK_MEMBERS": 1 << 1, "BAN_MEMBERS": 1 << 2,
    "ADMINISTRATOR": 1 << 3, "MANAGE_CHANNELS": 1 << 4, "MANAGE_GUILD": 1 << 5,
    "ADD_REACTIONS": 1 << 6, "VIEW_AUDIT_LOG": 1 << 7, "STREAM": 1 << 9,
    "VIEW_CHANNEL": 1 << 10, "SEND_MESSAGES": 1 << 11, "MANAGE_MESSAGES": 1 << 13,
    "EMBED_LINKS": 1 << 14, "ATTACH_FILES": 1 << 15, "READ_MESSAGE_HISTORY": 1 << 16,
    "MENTION_EVERYONE": 1 << 17, "USE_EXTERNAL_EMOJIS": 1 << 18, "CONNECT": 1 << 20,
    "SPEAK": 1 << 21, "USE_VAD": 1 << 25, "MANAGE_WEBHOOKS": 1 << 29,
}
VERIFICATION = {"low": 1, "medium": 2, "high": 3, "highest": 4}
CONTENT_FILTER = {"disabled": 0, "members_without_roles": 1, "all_members": 2}
NOTIFICATIONS = {"all_messages": 0, "only_mentions": 1}

SCHEMA_PROMPT = """I want you to act as a Discord server configuration generator.

Ask me a few clarifying questions first if you need more detail (who's involved, what should be private vs shared, whether voice channels are needed). Once you have enough information, respond with ONLY a single JSON object — no prose, no markdown code fences, no explanation — matching exactly this schema:

{{
  "guild": {{
    "name": string,
    "verification_level": "low" | "medium" | "high" | "highest",
    "explicit_content_filter": "disabled" | "members_without_roles" | "all_members",
    "default_notifications": "all_messages" | "only_mentions"
  }},
  "roles": [
    {{
      "name": string,
      "color": string,            // hex like "#C0392B"
      "hoist": boolean,
      "mentionable": boolean,
      "permissions": [string, ...]   // only from the allowed list below
    }}
  ],
  "categories": [
    {{
      "name": string,
      "overwrites": [ {{ "role": string, "allow": [string,...], "deny": [string,...] }} ],
      "channels": [
        {{
          "name": string,
          "type": "text" | "voice",
          "overwrites": [ {{ "role": string, "allow": [string,...], "deny": [string,...] }} ]
        }}
      ]
    }}
  ]
}}

Rules:
- List roles from LOWEST authority first to HIGHEST authority last — the last role in the array becomes the top of the hierarchy.
- Only use these permission names: {perm_names}.
- To fully strip a role's permissions, set "permissions": [].
- You may include a role named "@everyone" in the roles array to explicitly set default member permissions (usually []); otherwise it's stripped to no permissions automatically.
- Do not invent extra top-level fields.
- Output must be valid JSON — no trailing commas, no comments, nothing before or after the JSON object.

Here's what I want my server to do:
{description}"""


def banner():
    console.print(
        Panel.fit(
            Text.from_markup(
                f"[bold {ACCENT}]◆[/bold {ACCENT}] [bold white]Discord Server Builder[/bold white]\n"
                "[dim]generate → validate → apply[/dim]"
            ),
            border_style=ACCENT,
            box=box.ROUNDED,
            padding=(1, 4),
        )
    )


def step_header(n, title):
    console.print()
    console.print(f"[bold {ACCENT}]Step {n}[/bold {ACCENT}]  [bold white]{title}[/bold white]")
    console.print("[dim]" + "─" * 50 + "[/dim]")


def ok(msg):
    console.print(f"  [{GOOD}]✓[/{GOOD}] {escape(str(msg))}")


def fail(msg):
    console.print(f"  [{BAD}]✗[/{BAD}] {escape(str(msg))}")


def info(msg):
    console.print(f"  [dim]{escape(str(msg))}[/dim]")


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
def cmd_prompt(description, save_path=None):
    step_header(1, "Generate your config")
    if not description:
        description = Prompt.ask("What do you want this server to do?")

    text = SCHEMA_PROMPT.format(perm_names=", ".join(PERMS.keys()), description=description)
    console.print(Panel(Text(text), title="copy this into any AI chat", border_style=ACCENT, box=box.ROUNDED))

    copied = False
    try:
        import pyperclip
        pyperclip.copy(text)
        copied = True
    except Exception:
        copied = False

    if copied:
        ok("Copied to clipboard.")
    else:
        info("Couldn't reach the system clipboard — select and copy the text above manually.")

    if save_path:
        with open(save_path, "w") as f:
            f.write(text)
        ok(f"Also saved to {save_path}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def load_config(path):
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path) as f:
            raw = f.read()
    return json.loads(raw)


def validate_config(cfg):
    errors = []
    if not isinstance(cfg.get("guild"), dict):
        errors.append('Missing "guild" object.')
    else:
        g = cfg["guild"]
        if not g.get("name"):
            errors.append("guild.name is missing.")
        if g.get("verification_level") and g["verification_level"] not in VERIFICATION:
            errors.append("guild.verification_level must be low/medium/high/highest.")
        if g.get("explicit_content_filter") and g["explicit_content_filter"] not in CONTENT_FILTER:
            errors.append("guild.explicit_content_filter must be disabled/members_without_roles/all_members.")
        if g.get("default_notifications") and g["default_notifications"] not in NOTIFICATIONS:
            errors.append("guild.default_notifications must be all_messages/only_mentions.")

    roles = cfg.get("roles")
    if not isinstance(roles, list):
        errors.append('Missing "roles" array.')
        roles = []
    categories = cfg.get("categories")
    if not isinstance(categories, list):
        errors.append('Missing "categories" array.')
        categories = []

    role_names = {"@everyone"}
    for i, r in enumerate(roles):
        if not r.get("name"):
            errors.append(f"roles[{i}] is missing \"name\".")
        else:
            role_names.add(r["name"])
        for p in r.get("permissions") or []:
            if p not in PERMS:
                errors.append(f"roles[{i}] has unknown permission \"{p}\".")

    def check_overwrites(lst, where):
        for i, o in enumerate(lst or []):
            if not o.get("role"):
                errors.append(f"{where}.overwrites[{i}] missing \"role\".")
                continue
            if o["role"] not in role_names:
                errors.append(f"{where}.overwrites[{i}] references unknown role \"{o['role']}\".")
            for p in (o.get("allow") or []) + (o.get("deny") or []):
                if p not in PERMS:
                    errors.append(f"{where}.overwrites[{i}] has unknown permission \"{p}\".")

    for ci, c in enumerate(categories):
        if not c.get("name"):
            errors.append(f"categories[{ci}] is missing \"name\".")
        check_overwrites(c.get("overwrites"), f"categories[{ci}]")
        channels = c.get("channels")
        if not isinstance(channels, list):
            errors.append(f"categories[{ci}].channels must be an array.")
            channels = []
        for chi, ch in enumerate(channels):
            if not ch.get("name"):
                errors.append(f"categories[{ci}].channels[{chi}] is missing \"name\".")
            if ch.get("type") not in ("text", "voice"):
                errors.append(f"categories[{ci}].channels[{chi}].type must be \"text\" or \"voice\".")
            check_overwrites(ch.get("overwrites"), f"categories[{ci}].channels[{chi}]")

    return errors


def render_preview(cfg):
    table = Table(title="Roles (bottom → top)", box=box.SIMPLE, show_edge=False)
    table.add_column("Role")
    table.add_column("Color")
    table.add_column("Permissions")
    for r in cfg.get("roles", []):
        perms = escape(", ".join(r.get("permissions") or [])) or "[dim]none[/dim]"
        color = r.get("color", "#999999")
        table.add_row(f"[{color}]{escape(r['name'])}[/{color}]", escape(color), perms)
    console.print(table)

    tree = Tree("[bold]Channels[/bold]")
    for c in cfg.get("categories", []):
        branch = tree.add(f"[bold {ACCENT}]{escape(c['name'])}[/bold {ACCENT}]")
        for ch in c.get("channels", []):
            icon = "\U0001F50A" if ch.get("type") == "voice" else "#"
            branch.add(f"[dim]{icon} {escape(ch['name'])}[/dim]")
    console.print(tree)


def cmd_validate(path):
    step_header(2, "Validate the config")
    try:
        cfg = load_config(path)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"Couldn't read/parse JSON: {e}")
        return None

    errors = validate_config(cfg)
    if errors:
        fail(f"{len(errors)} issue(s) found:")
        for e in errors:
            console.print(f"    [{BAD}]•[/{BAD}] {escape(e)}")
        return None

    ok(f"Valid — {len(cfg['roles'])} role(s), {len(cfg['categories'])} categor"
       f"{'y' if len(cfg['categories']) == 1 else 'ies'}.")
    render_preview(cfg)
    return cfg


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
API = "https://discord.com/api/v10"


def perm_str(names):
    total = 0
    for n in names or []:
        total |= PERMS.get(n, 0)
    return str(total)


class DiscordAPI:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        for _ in range(5):
            resp = requests.request(method, API + path, headers=self.headers, json=body)
            if resp.status_code == 429:
                wait = resp.json().get("retry_after", 1)
                info(f"rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {resp.status_code} {resp.text}")
            time.sleep(0.35)
            return resp.json() if resp.text else {}
        raise RuntimeError(f"{method} {path} -> gave up after rate-limit retries")


def cmd_apply(path, yes=False):
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")

    step_header(3, "Apply to Discord")
    if not token or not guild_id:
        fail("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in your .env file.")
        return

    cfg = cmd_validate(path)
    if cfg is None:
        return

    console.print()
    if not yes and not Confirm.ask(
        f"[{WARN}]This will modify the live server {guild_id}. Continue?[/{WARN}]", default=False
    ):
        info("Cancelled.")
        return

    api = DiscordAPI(token)

    try:
        info("Updating server settings...")
        g = cfg.get("guild", {})
        api.call("PATCH", f"/guilds/{guild_id}", {
            "name": g.get("name"),
            "verification_level": VERIFICATION.get(g.get("verification_level"), 2),
            "explicit_content_filter": CONTENT_FILTER.get(g.get("explicit_content_filter"), 2),
            "default_message_notifications": NOTIFICATIONS.get(g.get("default_notifications"), 1),
        })
        ok("Server settings updated.")

        existing = api.call("GET", f"/guilds/{guild_id}/roles") or []
        everyone = next((r for r in existing if r["name"] == "@everyone"), None)
        if not everyone:
            raise RuntimeError("Couldn't find the @everyone role — check the guild ID and bot permissions.")
        role_ids = {"@everyone": everyone["id"]}

        everyone_cfg = next((r for r in cfg["roles"] if r["name"] == "@everyone"), None)
        api.call("PATCH", f"/guilds/{guild_id}/roles/{everyone['id']}",
                  {"permissions": perm_str(everyone_cfg["permissions"] if everyone_cfg else [])})
        ok("@everyone permissions set.")

        custom_roles = [r for r in cfg["roles"] if r["name"] != "@everyone"]
        for r in custom_roles:
            created = api.call("POST", f"/guilds/{guild_id}/roles", {
                "name": r["name"],
                "color": int(r.get("color", "#000000").lstrip("#"), 16),
                "hoist": bool(r.get("hoist", False)),
                "mentionable": bool(r.get("mentionable", False)),
                "permissions": perm_str(r.get("permissions")),
            })
            role_ids[r["name"]] = created["id"]
            ok(f'Role "{r["name"]}" created.')

        if custom_roles:
            api.call("PATCH", f"/guilds/{guild_id}/roles", [
                {"id": role_ids[r["name"]], "position": i + 1} for i, r in enumerate(custom_roles)
            ])
            ok("Role hierarchy set.")

        def overwrites(lst):
            out = []
            for o in lst or []:
                rid = role_ids.get(o["role"])
                if not rid:
                    continue
                out.append({"id": rid, "type": 0, "allow": perm_str(o.get("allow")), "deny": perm_str(o.get("deny"))})
            return out

        for cat in cfg["categories"]:
            cat_res = api.call("POST", f"/guilds/{guild_id}/channels", {
                "name": cat["name"], "type": 4, "permission_overwrites": overwrites(cat.get("overwrites")),
            })
            ok(f'Category "{cat["name"]}" created.')
            for ch in cat.get("channels", []):
                api.call("POST", f"/guilds/{guild_id}/channels", {
                    "name": ch["name"],
                    "type": 2 if ch.get("type") == "voice" else 0,
                    "parent_id": cat_res["id"],
                    "permission_overwrites": overwrites(ch.get("overwrites")),
                })
                ok(f'  Channel "{ch["name"]}" created.')

        console.print()
        console.print(Panel.fit(f"[bold {GOOD}]Server configured.[/bold {GOOD}]", border_style=GOOD, box=box.ROUNDED))

    except RuntimeError as e:
        fail(str(e))
    except requests.RequestException as e:
        fail(f"Network error: {e}")


# ---------------------------------------------------------------------------
# interactive menu
# ---------------------------------------------------------------------------
def interactive():
    banner()
    while True:
        console.print()
        choice = Prompt.ask(
            "What do you want to do?",
            choices=["prompt", "validate", "apply", "exit"],
            default="prompt",
        )
        if choice == "prompt":
            cmd_prompt(None)
        elif choice == "validate":
            path = Prompt.ask("Path to the JSON config file")
            cmd_validate(path)
        elif choice == "apply":
            path = Prompt.ask("Path to the JSON config file")
            cmd_apply(path)
        else:
            break


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(prog="discord-builder", description="Generate, validate, and apply Discord server configs.")
    sub = parser.add_subparsers(dest="command")

    p_prompt = sub.add_parser("prompt", help="Print the copyable AI prompt")
    p_prompt.add_argument("description", nargs="?", default=None)
    p_prompt.add_argument("--save", dest="save_path", default=None, help="Also save the prompt to a file")

    p_validate = sub.add_parser("validate", help="Validate a JSON config file")
    p_validate.add_argument("config", help='Path to config JSON, or "-" for stdin')

    p_apply = sub.add_parser("apply", help="Apply a JSON config to Discord")
    p_apply.add_argument("config", help="Path to config JSON")
    p_apply.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")

    args = parser.parse_args()

    if args.command is None:
        interactive()
        return

    banner()
    if args.command == "prompt":
        cmd_prompt(args.description, args.save_path)
    elif args.command == "validate":
        cmd_validate(args.config)
    elif args.command == "apply":
        cmd_apply(args.config, args.yes)


if __name__ == "__main__":
    main()
