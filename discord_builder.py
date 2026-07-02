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

# ---------------------------------------------------------------------------
# Introspection lookups (decode the raw values Discord hands back)
# ---------------------------------------------------------------------------
VERIFICATION_REV = {v: k for k, v in VERIFICATION.items()}
CONTENT_FILTER_REV = {v: k for k, v in CONTENT_FILTER.items()}
NOTIFICATIONS_REV = {v: k for k, v in NOTIFICATIONS.items()}

# Full Discord permission table (bit position -> name). Broader than PERMS on
# purpose: apply only writes the common flags, but inspect should be able to
# decode whatever a live server already has set.
ALL_PERMS = {
    0: "CREATE_INSTANT_INVITE", 1: "KICK_MEMBERS", 2: "BAN_MEMBERS",
    3: "ADMINISTRATOR", 4: "MANAGE_CHANNELS", 5: "MANAGE_GUILD",
    6: "ADD_REACTIONS", 7: "VIEW_AUDIT_LOG", 8: "PRIORITY_SPEAKER",
    9: "STREAM", 10: "VIEW_CHANNEL", 11: "SEND_MESSAGES",
    12: "SEND_TTS_MESSAGES", 13: "MANAGE_MESSAGES", 14: "EMBED_LINKS",
    15: "ATTACH_FILES", 16: "READ_MESSAGE_HISTORY", 17: "MENTION_EVERYONE",
    18: "USE_EXTERNAL_EMOJIS", 19: "VIEW_GUILD_INSIGHTS", 20: "CONNECT",
    21: "SPEAK", 22: "MUTE_MEMBERS", 23: "DEAFEN_MEMBERS", 24: "MOVE_MEMBERS",
    25: "USE_VAD", 26: "CHANGE_NICKNAME", 27: "MANAGE_NICKNAMES",
    28: "MANAGE_ROLES", 29: "MANAGE_WEBHOOKS", 30: "MANAGE_GUILD_EXPRESSIONS",
    31: "USE_APPLICATION_COMMANDS", 32: "REQUEST_TO_SPEAK", 33: "MANAGE_EVENTS",
    34: "MANAGE_THREADS", 35: "CREATE_PUBLIC_THREADS", 36: "CREATE_PRIVATE_THREADS",
    37: "USE_EXTERNAL_STICKERS", 38: "SEND_MESSAGES_IN_THREADS",
    39: "USE_EMBEDDED_ACTIVITIES", 40: "MODERATE_MEMBERS",
    41: "VIEW_CREATOR_MONETIZATION_ANALYTICS", 42: "USE_SOUNDBOARD",
    43: "CREATE_GUILD_EXPRESSIONS", 44: "CREATE_EVENTS", 45: "USE_EXTERNAL_SOUNDS",
    46: "SEND_VOICE_MESSAGES", 49: "SEND_POLLS", 50: "USE_EXTERNAL_APPS",
}

# Discord channel type -> human label (and whether apply can recreate it).
CHANNEL_TYPES = {
    0: "text", 1: "dm", 2: "voice", 3: "group_dm", 4: "category",
    5: "announcement", 10: "announcement_thread", 11: "public_thread",
    12: "private_thread", 13: "stage", 14: "directory", 15: "forum",
    16: "media",
}
CHANNEL_ICONS = {
    "text": "#", "voice": "\U0001F50A", "announcement": "\U0001F4E3",
    "stage": "\U0001F3A4", "forum": "\U0001F5E3", "media": "\U0001F5BC",
}

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


def decode_perms(value):
    """Turn a Discord permission bitfield string into a list of flag names."""
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return []
    names = []
    for i in range(64):
        if bits & (1 << i):
            names.append(ALL_PERMS.get(i, f"UNKNOWN_BIT_{i}"))
    return names


def int_to_hex(color):
    """Discord role colors come back as ints; 0 means 'no color'."""
    try:
        color = int(color)
    except (TypeError, ValueError):
        return "#000000"
    return f"#{color:06X}"


# Reverse of ALL_PERMS, so callers can encode any known permission name (not
# just the apply-time subset in PERMS) back into a Discord bitfield string.
NAME_TO_BIT = {name: bit for bit, name in ALL_PERMS.items()}


def encode_perms(names):
    """Turn a list of permission flag names into a Discord bitfield string."""
    total = 0
    for n in names or []:
        if n in NAME_TO_BIT:
            total |= 1 << NAME_TO_BIT[n]
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
# inspect  —  read a live server and show/export everything it contains
# ---------------------------------------------------------------------------
def _overwrite_summary(overwrites, id_to_role):
    """Compact 'RoleName +N/-M' string for a channel's permission overwrites."""
    parts = []
    for o in overwrites or []:
        if str(o.get("type")) not in ("0", "role"):
            continue  # skip member-specific overwrites
        name = id_to_role.get(o.get("id"), o.get("id"))
        allow = len(decode_perms(o.get("allow")))
        deny = len(decode_perms(o.get("deny")))
        bits = []
        if allow:
            bits.append(f"[{GOOD}]+{allow}[/{GOOD}]")
        if deny:
            bits.append(f"[{BAD}]-{deny}[/{BAD}]")
        parts.append(f"{escape(str(name))} {' '.join(bits)}".strip())
    return "  ".join(parts)


def render_inspection(guild, roles, channels, emojis, stickers):
    id_to_role = {r["id"]: r["name"] for r in roles}

    # --- Guild summary -----------------------------------------------------
    approx_members = guild.get("approximate_member_count")
    approx_online = guild.get("approximate_presence_count")
    features = guild.get("features") or []
    lines = [
        f"[bold white]{escape(guild.get('name', '?'))}[/bold white]  [dim]#{guild.get('id')}[/dim]",
    ]
    if guild.get("description"):
        lines.append(f"[dim]{escape(guild['description'])}[/dim]")
    lines.append("")
    lines.append(f"Members: [bold]{approx_members if approx_members is not None else '?'}[/bold]"
                 f"   Online: [bold]{approx_online if approx_online is not None else '?'}[/bold]")
    lines.append(
        "Verification: [bold]{}[/bold]   Content filter: [bold]{}[/bold]   Notifications: [bold]{}[/bold]".format(
            VERIFICATION_REV.get(guild.get("verification_level"), guild.get("verification_level")),
            CONTENT_FILTER_REV.get(guild.get("explicit_content_filter"), guild.get("explicit_content_filter")),
            NOTIFICATIONS_REV.get(guild.get("default_message_notifications"), guild.get("default_message_notifications")),
        )
    )
    lines.append(
        f"Boost tier: [bold]{guild.get('premium_tier', 0)}[/bold]"
        f"   Boosts: [bold]{guild.get('premium_subscription_count', 0)}[/bold]"
        f"   Emojis: [bold]{len(emojis)}[/bold]   Stickers: [bold]{len(stickers)}[/bold]"
    )
    if features:
        lines.append(f"[dim]Features: {escape(', '.join(sorted(features)))}[/dim]")
    console.print(Panel(Text.from_markup("\n".join(lines)),
                        title="Server", border_style=ACCENT, box=box.ROUNDED))

    # --- Roles (highest authority first, the way Discord shows them) --------
    table = Table(title="Roles (top → bottom)", box=box.SIMPLE, show_edge=False)
    table.add_column("Role")
    table.add_column("Color")
    table.add_column("Flags")
    table.add_column("Permissions")
    for r in sorted(roles, key=lambda x: x.get("position", 0), reverse=True):
        color = int_to_hex(r.get("color"))
        color_disp = color if r.get("color") else "[dim]none[/dim]"
        flags = []
        if r.get("hoist"):
            flags.append("hoist")
        if r.get("mentionable"):
            flags.append("mention")
        if r.get("managed"):
            flags.append("[dim]managed[/dim]")
        perms = decode_perms(r.get("permissions"))
        if "ADMINISTRATOR" in perms:
            perm_disp = f"[{WARN}]ADMINISTRATOR[/{WARN}] [dim](all)[/dim]"
        else:
            perm_disp = escape(", ".join(perms)) or "[dim]none[/dim]"
        name_disp = f"[{color}]{escape(r['name'])}[/{color}]" if r.get("color") else escape(r["name"])
        table.add_row(name_disp, escape(color_disp) if r.get("color") else color_disp,
                      " ".join(flags) or "[dim]—[/dim]", perm_disp)
    console.print(table)

    # --- Channel tree ------------------------------------------------------
    cats = {c["id"]: c for c in channels if c.get("type") == 4}
    tree = Tree("[bold]Channels[/bold]")
    branches = {}
    for cid, c in sorted(cats.items(), key=lambda kv: kv[1].get("position", 0)):
        label = f"[bold {ACCENT}]{escape(c['name'])}[/bold {ACCENT}]"
        ow = _overwrite_summary(c.get("permission_overwrites"), id_to_role)
        if ow:
            label += f"   [dim]{ow}[/dim]"
        branches[cid] = tree.add(label)

    orphan = tree.add("[dim](no category)[/dim]")
    non_cats = [c for c in channels if c.get("type") != 4
                and c.get("type") not in (10, 11, 12)]  # skip threads
    for ch in sorted(non_cats, key=lambda x: (x.get("position", 0), x.get("name", ""))):
        ctype = CHANNEL_TYPES.get(ch.get("type"), str(ch.get("type")))
        icon = CHANNEL_ICONS.get(ctype, "#")
        label = f"[dim]{icon} {escape(ch['name'])}[/dim]"
        ow = _overwrite_summary(ch.get("permission_overwrites"), id_to_role)
        if ow:
            label += f"   [dim]{ow}[/dim]"
        parent = branches.get(ch.get("parent_id"), orphan)
        parent.add(label)
    if not orphan.children:
        tree.children.remove(orphan)
    console.print(tree)


def export_config(guild, roles, channels):
    """Reverse the live server into an apply-compatible config, plus a
    read-only `meta` block that captures the extra context apply can't set."""
    id_to_role = {r["id"]: r["name"] for r in roles}

    def ow_list(overwrites):
        out = []
        for o in overwrites or []:
            if str(o.get("type")) not in ("0", "role"):
                continue
            name = id_to_role.get(o.get("id"))
            if not name:
                continue
            out.append({
                "role": name,
                "allow": decode_perms(o.get("allow")),
                "deny": decode_perms(o.get("deny")),
            })
        return out

    ordered_roles = sorted(roles, key=lambda x: x.get("position", 0))  # lowest → highest
    cfg_roles = []
    for r in ordered_roles:
        cfg_roles.append({
            "name": r["name"],
            "color": int_to_hex(r.get("color")),
            "hoist": bool(r.get("hoist")),
            "mentionable": bool(r.get("mentionable")),
            "permissions": decode_perms(r.get("permissions")),
        })

    cats = sorted([c for c in channels if c.get("type") == 4],
                  key=lambda x: x.get("position", 0))
    cfg_categories = []
    for cat in cats:
        kids = [c for c in channels if c.get("parent_id") == cat["id"]
                and c.get("type") in (0, 2, 5)]
        cfg_categories.append({
            "name": cat["name"],
            "overwrites": ow_list(cat.get("permission_overwrites")),
            "channels": [{
                "name": ch["name"],
                "type": "voice" if ch.get("type") == 2 else "text",
                "overwrites": ow_list(ch.get("permission_overwrites")),
            } for ch in sorted(kids, key=lambda x: x.get("position", 0))],
        })

    return {
        "guild": {
            "name": guild.get("name"),
            "verification_level": VERIFICATION_REV.get(guild.get("verification_level"), "medium"),
            "explicit_content_filter": CONTENT_FILTER_REV.get(guild.get("explicit_content_filter"), "all_members"),
            "default_notifications": NOTIFICATIONS_REV.get(guild.get("default_message_notifications"), "only_mentions"),
        },
        "roles": cfg_roles,
        "categories": cfg_categories,
        "meta": {
            "guild_id": guild.get("id"),
            "description": guild.get("description"),
            "approximate_member_count": guild.get("approximate_member_count"),
            "approximate_presence_count": guild.get("approximate_presence_count"),
            "premium_tier": guild.get("premium_tier"),
            "premium_subscription_count": guild.get("premium_subscription_count"),
            "features": guild.get("features"),
            "channel_types": {
                CHANNEL_TYPES.get(t, str(t)): sum(1 for c in channels if c.get("type") == t)
                for t in sorted({c.get("type") for c in channels})
            },
        },
    }


def cmd_inspect(guild_id=None, json_out=None):
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = guild_id or os.environ.get("DISCORD_GUILD_ID")

    step_header(1, "Inspect a live server")
    if not token:
        fail("DISCORD_BOT_TOKEN must be set in your .env file.")
        return None
    if not guild_id:
        fail("No guild ID — pass one as an argument or set DISCORD_GUILD_ID in .env.")
        return None

    api = DiscordAPI(token)
    try:
        info("Fetching server...")
        guild = api.call("GET", f"/guilds/{guild_id}?with_counts=true")
        roles = api.call("GET", f"/guilds/{guild_id}/roles") or []
        channels = api.call("GET", f"/guilds/{guild_id}/channels") or []
        emojis = api.call("GET", f"/guilds/{guild_id}/emojis") or []
        stickers = api.call("GET", f"/guilds/{guild_id}/stickers") or []
    except RuntimeError as e:
        fail(str(e))
        return None
    except requests.RequestException as e:
        fail(f"Network error: {e}")
        return None

    ok(f"Read {len(roles)} role(s), {len(channels)} channel(s), "
       f"{len(emojis)} emoji(s), {len(stickers)} sticker(s).")
    console.print()
    render_inspection(guild, roles, channels, emojis, stickers)

    cfg = export_config(guild, roles, channels)
    if json_out:
        with open(json_out, "w") as f:
            json.dump(cfg, f, indent=2)
        console.print()
        ok(f"Full config exported to {json_out}")
    return cfg


# ---------------------------------------------------------------------------
# server  —  launch the live AI server-manager bot
# ---------------------------------------------------------------------------
def cmd_server(config_path="server_config.json"):
    step_header(1, "Launch the AI server manager")
    try:
        from discord_agent import run_server
    except ImportError as e:
        fail(f"Couldn't load the server module: {e}")
        info("Install the extras with:  pip install -r requirements.txt")
        return
    info(f"Config: {config_path if os.path.exists(config_path) else 'defaults (no server_config.json found)'}")
    info("Starting bot... press Ctrl+C to stop.")
    console.print()
    run_server(config_path)


# ---------------------------------------------------------------------------
# interactive menu
# ---------------------------------------------------------------------------
def interactive():
    banner()
    while True:
        console.print()
        choice = Prompt.ask(
            "What do you want to do?",
            choices=["prompt", "validate", "apply", "inspect", "server", "exit"],
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
        elif choice == "inspect":
            gid = Prompt.ask("Guild ID (blank to use DISCORD_GUILD_ID from .env)", default="")
            save = Prompt.ask("Export path for the full config (blank to skip)", default="")
            cmd_inspect(gid or None, save or None)
        elif choice == "server":
            cmd_server()
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

    p_inspect = sub.add_parser("inspect", help="Read a live server: roles, permissions, channels, and more")
    p_inspect.add_argument("guild_id", nargs="?", default=None,
                           help="Guild ID to inspect (defaults to DISCORD_GUILD_ID from .env)")
    p_inspect.add_argument("--json", dest="json_out", default=None,
                           help="Export the full server config to this JSON file")

    p_server = sub.add_parser("server", help="Launch the live AI server-manager bot")
    p_server.add_argument("--config", dest="config_path", default="server_config.json",
                          help="Path to the server config JSON (default: server_config.json)")

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
    elif args.command == "inspect":
        cmd_inspect(args.guild_id, args.json_out)
    elif args.command == "server":
        cmd_server(args.config_path)


if __name__ == "__main__":
    main()
