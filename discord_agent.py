#!/usr/bin/env python3
"""
Discord Server Manager — live AI agent (Ollama-powered)
=======================================================
Runs a long-lived Discord bot that listens in a dedicated admin channel and
lets you manage the server in plain English. Every message is handed to a local
Ollama model with a *fresh snapshot of the current server* (roles, permissions,
channels), so it always acts on up-to-date context. The model then carries out
the change by calling real Discord API tools.

The AI runs entirely on your own machine via Ollama — no API keys, nothing
leaves your network.

Setup:
    pip install -r requirements.txt
    echo "DISCORD_BOT_TOKEN=your-bot-token"   >> .env
    echo "DISCORD_GUILD_ID=your-server-id"    >> .env

    # Install Ollama (https://ollama.com) and pull a tool-capable model:
    ollama pull llama3.1        # or qwen2.5, mistral-nemo, etc.

Then either run it directly, or via the CLI:
    ./discord_agent.py
    ./discord_builder.py server

Behaviour (model, Ollama host, etc.) is tuned in server_config.json — see
example_server_config.json.

The bot needs the **Message Content** privileged intent enabled in the Discord
Developer Portal, plus Manage Roles / Manage Channels / Manage Server perms.
"""

import asyncio
import json
import os

import requests
from dotenv import load_dotenv

# Reuse everything the CLI already knows about Discord.
from discord_builder import (
    ALL_PERMS,
    DiscordAPI,
    encode_perms,
    export_config,
)

DEFAULT_CONFIG = {
    # Ollama runs locally and handles the AI. Default is the standard port.
    "ollama_host": "http://localhost:11434",
    # Must be a tool-calling capable model you've pulled (ollama pull <model>).
    "model": "llama3.1",
    # Sampling temperature for the model.
    "temperature": 0.0,
    # Channel the bot listens in. Matched by name (without '#') or by raw ID.
    "admin_channel": "server-admin",
    # Only members with the Manage Server permission may command the bot.
    "require_manage_guild": True,
    # Allow destructive actions (deleting roles/channels).
    "allow_deletes": True,
    # How many prior turns of conversation to keep per channel.
    "max_history_turns": 12,
    # Extra persona / house-rules appended to the system prompt.
    "persona": "",
}


def load_server_config(path="server_config.json"):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[warn] couldn't read {path}: {e} — using defaults")
    # Env overrides, so you don't have to touch the file.
    if os.environ.get("DISCORD_ADMIN_CHANNEL"):
        cfg["admin_channel"] = os.environ["DISCORD_ADMIN_CHANNEL"]
    if os.environ.get("OLLAMA_HOST"):
        cfg["ollama_host"] = os.environ["OLLAMA_HOST"]
    if os.environ.get("OLLAMA_MODEL"):
        cfg["model"] = os.environ["OLLAMA_MODEL"]
    return cfg


# ---------------------------------------------------------------------------
# ServerManager — the actual actions the AI can take (thin wrappers over REST)
# ---------------------------------------------------------------------------
CREATE_CHANNEL_TYPES = {"text": 0, "voice": 2, "announcement": 5, "forum": 15, "stage": 13}


class ServerManager:
    """Every method returns a short human-readable result string. Names are
    resolved to IDs against a freshly fetched view of the guild each call, so
    the AI can refer to roles/channels by name."""

    def __init__(self, api, guild_id, allow_deletes=True):
        self.api = api
        self.guild_id = guild_id
        self.allow_deletes = allow_deletes

    # --- lookups -----------------------------------------------------------
    def _roles(self):
        return self.api.call("GET", f"/guilds/{self.guild_id}/roles") or []

    def _channels(self):
        return self.api.call("GET", f"/guilds/{self.guild_id}/channels") or []

    def _find_role(self, name):
        for r in self._roles():
            if r["name"].lower() == str(name).lower():
                return r
        return None

    def _find_channel(self, name, want_category=False):
        name = str(name).lstrip("#").lower()
        for c in self._channels():
            is_cat = c.get("type") == 4
            if is_cat == want_category and c["name"].lower() == name:
                return c
        return None

    # --- context -----------------------------------------------------------
    def snapshot(self):
        """Full, current server state as an apply-style config dict."""
        guild = self.api.call("GET", f"/guilds/{self.guild_id}?with_counts=true")
        return export_config(guild, self._roles(), self._channels())

    # --- guild -------------------------------------------------------------
    def update_guild_settings(self, name=None, verification_level=None,
                              explicit_content_filter=None, default_notifications=None):
        from discord_builder import VERIFICATION, CONTENT_FILTER, NOTIFICATIONS
        body = {}
        if name is not None:
            body["name"] = name
        if verification_level is not None:
            body["verification_level"] = VERIFICATION.get(verification_level, 2)
        if explicit_content_filter is not None:
            body["explicit_content_filter"] = CONTENT_FILTER.get(explicit_content_filter, 2)
        if default_notifications is not None:
            body["default_message_notifications"] = NOTIFICATIONS.get(default_notifications, 1)
        if not body:
            return "No settings provided; nothing changed."
        self.api.call("PATCH", f"/guilds/{self.guild_id}", body)
        return f"Server settings updated: {', '.join(body.keys())}."

    # --- roles -------------------------------------------------------------
    def create_role(self, name, color="#000000", hoist=False, mentionable=False, permissions=None):
        created = self.api.call("POST", f"/guilds/{self.guild_id}/roles", {
            "name": name,
            "color": int(str(color).lstrip("#") or "0", 16),
            "hoist": bool(hoist),
            "mentionable": bool(mentionable),
            "permissions": encode_perms(permissions),
        })
        return f'Created role "{name}" (id {created["id"]}).'

    def edit_role(self, role_name, new_name=None, color=None, hoist=None,
                  mentionable=None, permissions=None):
        role = self._find_role(role_name)
        if not role:
            return f'Role "{role_name}" not found.'
        body = {}
        if new_name is not None:
            body["name"] = new_name
        if color is not None:
            body["color"] = int(str(color).lstrip("#") or "0", 16)
        if hoist is not None:
            body["hoist"] = bool(hoist)
        if mentionable is not None:
            body["mentionable"] = bool(mentionable)
        if permissions is not None:
            body["permissions"] = encode_perms(permissions)
        self.api.call("PATCH", f"/guilds/{self.guild_id}/roles/{role['id']}", body)
        return f'Updated role "{role_name}": {", ".join(body.keys()) or "no changes"}.'

    def delete_role(self, role_name):
        if not self.allow_deletes:
            return "Deletes are disabled in server_config.json (allow_deletes=false)."
        role = self._find_role(role_name)
        if not role:
            return f'Role "{role_name}" not found.'
        if role["name"] == "@everyone":
            return "Refusing to delete the @everyone role."
        self.api.call("DELETE", f"/guilds/{self.guild_id}/roles/{role['id']}")
        return f'Deleted role "{role_name}".'

    # --- channels ----------------------------------------------------------
    def create_category(self, name):
        created = self.api.call("POST", f"/guilds/{self.guild_id}/channels",
                                {"name": name, "type": 4})
        return f'Created category "{name}" (id {created["id"]}).'

    def create_channel(self, name, type="text", category=None, topic=None):
        ctype = CREATE_CHANNEL_TYPES.get(type, 0)
        body = {"name": name, "type": ctype}
        if topic:
            body["topic"] = topic
        if category:
            cat = self._find_channel(category, want_category=True)
            if not cat:
                return f'Category "{category}" not found.'
            body["parent_id"] = cat["id"]
        created = self.api.call("POST", f"/guilds/{self.guild_id}/channels", body)
        where = f' under "{category}"' if category else ""
        return f'Created {type} channel "{name}"{where} (id {created["id"]}).'

    def delete_channel(self, channel_name):
        if not self.allow_deletes:
            return "Deletes are disabled in server_config.json (allow_deletes=false)."
        ch = self._find_channel(channel_name) or self._find_channel(channel_name, want_category=True)
        if not ch:
            return f'Channel "{channel_name}" not found.'
        self.api.call("DELETE", f"/channels/{ch['id']}")
        return f'Deleted channel "{channel_name}".'

    def edit_channel(self, channel_name, new_name=None, topic=None):
        ch = self._find_channel(channel_name) or self._find_channel(channel_name, want_category=True)
        if not ch:
            return f'Channel "{channel_name}" not found.'
        body = {}
        if new_name is not None:
            body["name"] = new_name
        if topic is not None:
            body["topic"] = topic
        self.api.call("PATCH", f"/channels/{ch['id']}", body)
        return f'Updated channel "{channel_name}".'

    def set_channel_permission(self, channel_name, role_name, allow=None, deny=None):
        ch = self._find_channel(channel_name) or self._find_channel(channel_name, want_category=True)
        if not ch:
            return f'Channel "{channel_name}" not found.'
        role = self._find_role(role_name)
        if not role:
            return f'Role "{role_name}" not found.'
        self.api.call("PUT", f"/channels/{ch['id']}/permissions/{role['id']}", {
            "type": 0,
            "allow": encode_perms(allow),
            "deny": encode_perms(deny),
        })
        return (f'Set overwrite on "{channel_name}" for "{role_name}" '
                f'(allow {allow or []}, deny {deny or []}).')

    # --- dispatch ----------------------------------------------------------
    def run_tool(self, name, args):
        method = getattr(self, name, None)
        if not method or name.startswith("_") or name in ("run_tool", "snapshot"):
            return f"Unknown tool: {name}"
        if not isinstance(args, dict):
            return f"Tool {name} received malformed arguments."
        return method(**args)


# ---------------------------------------------------------------------------
# Tool schemas exposed to the model (Ollama / OpenAI "function" format)
# ---------------------------------------------------------------------------
_PERM_LIST = sorted(ALL_PERMS.values())


def _fn(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


def build_tools():
    perm_items = {"type": "array", "items": {"type": "string", "enum": _PERM_LIST}}
    return [
        _fn("update_guild_settings", "Change server-wide settings.", {
            "name": {"type": "string"},
            "verification_level": {"type": "string", "enum": ["low", "medium", "high", "highest"]},
            "explicit_content_filter": {"type": "string", "enum": ["disabled", "members_without_roles", "all_members"]},
            "default_notifications": {"type": "string", "enum": ["all_messages", "only_mentions"]},
        }),
        _fn("create_role", "Create a new role.", {
            "name": {"type": "string"},
            "color": {"type": "string", "description": "Hex color like #C0392B"},
            "hoist": {"type": "boolean", "description": "Show members separately in the sidebar"},
            "mentionable": {"type": "boolean"},
            "permissions": perm_items,
        }, ["name"]),
        _fn("edit_role", "Edit an existing role by name. Only provided fields change. Passing 'permissions' replaces the role's permissions entirely.", {
            "role_name": {"type": "string"},
            "new_name": {"type": "string"},
            "color": {"type": "string"},
            "hoist": {"type": "boolean"},
            "mentionable": {"type": "boolean"},
            "permissions": perm_items,
        }, ["role_name"]),
        _fn("delete_role", "Delete a role by name.", {
            "role_name": {"type": "string"},
        }, ["role_name"]),
        _fn("create_category", "Create a channel category.", {
            "name": {"type": "string"},
        }, ["name"]),
        _fn("create_channel", "Create a channel, optionally inside an existing category.", {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": list(CREATE_CHANNEL_TYPES.keys())},
            "category": {"type": "string", "description": "Name of the parent category"},
            "topic": {"type": "string"},
        }, ["name"]),
        _fn("edit_channel", "Rename a channel/category or change its topic.", {
            "channel_name": {"type": "string"},
            "new_name": {"type": "string"},
            "topic": {"type": "string"},
        }, ["channel_name"]),
        _fn("delete_channel", "Delete a channel or category by name.", {
            "channel_name": {"type": "string"},
        }, ["channel_name"]),
        _fn("set_channel_permission", "Set a role's permission overwrite on a channel or category (allow/deny lists of permission flags).", {
            "channel_name": {"type": "string"},
            "role_name": {"type": "string"},
            "allow": perm_items,
            "deny": perm_items,
        }, ["channel_name", "role_name"]),
    ]


SYSTEM_PROMPT = """You are a Discord server manager operating live inside a Discord server via an admin channel.

Members talk to you in plain language and you carry out changes to THIS server by calling tools. Guidelines:
- Before every message you are given a fresh JSON snapshot of the current server state (guild settings, roles with decoded permissions, categories, and channels). Treat it as the source of truth for what currently exists — never assume; read the snapshot.
- Refer to roles and channels by their exact names as shown in the snapshot.
- Only use permission flag names from the allowed enum in the tools. To fully strip a role, pass an empty permissions list.
- Roles higher in the hierarchy have more authority; the bot can only manage roles below its own top role, and cannot grant permissions it doesn't have.
- Be decisive for clearly-specified requests: call the tool to make the change, then briefly confirm what you did in one or two sentences.
- For destructive actions (deleting roles or channels) or genuinely ambiguous requests, ask a short clarifying question in your reply INSTEAD of calling a tool, and wait for confirmation.
- If a tool returns an error, explain it plainly and suggest a fix; don't silently retry the same thing.
- Keep replies concise and readable — lead with the outcome. This is a chat channel, not a report."""


# ---------------------------------------------------------------------------
# The agent: one Ollama tool-use loop per user message
# ---------------------------------------------------------------------------
class ManagerAgent:
    def __init__(self, manager, config):
        self.manager = manager
        self.config = config
        self.tools = build_tools()
        self.host = config.get("ollama_host", "http://localhost:11434").rstrip("/")
        self.model = config.get("model", "llama3.1")
        self.history = {}  # channel_id -> list[message dict]

    def _chat(self, messages):
        """One round-trip to Ollama's /api/chat (non-streaming)."""
        resp = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "tools": self.tools,
                "stream": False,
                "options": {"temperature": self.config.get("temperature", 0.0)},
            },
            timeout=600,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Ollama returned {resp.status_code}: {resp.text}")
        return resp.json().get("message", {})

    def run_turn(self, channel_id, user_text):
        """Blocking: fetch fresh context, run the tool loop, return reply text.
        Runs off the event loop via asyncio.to_thread."""
        snapshot = self.manager.snapshot()
        context_block = (
            "Current server state (fresh snapshot):\n```json\n"
            + json.dumps(snapshot, indent=2)
            + "\n```\n\nRequest: "
            + user_text
        )

        history = self.history.setdefault(channel_id, [])
        system = SYSTEM_PROMPT + (
            f"\n\nAdditional house rules:\n{self.config['persona']}"
            if self.config.get("persona") else "")
        messages = ([{"role": "system", "content": system}]
                    + history
                    + [{"role": "user", "content": context_block}])

        actions = []
        final_text = ""
        for _ in range(12):  # safety bound on tool round-trips
            msg = self._chat(messages)
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    if isinstance(args, str):  # some models return a JSON string
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    try:
                        out = self.manager.run_tool(name, args)
                    except (RuntimeError, requests.RequestException) as e:
                        out = f"Error: {e}"
                    actions.append(f"{name} → {out}")
                    messages.append({"role": "tool", "tool_name": name, "content": out})
                continue

            final_text = msg.get("content", "") or ""
            break
        else:
            final_text = "Stopped after too many steps — please narrow the request."

        # Persist a trimmed, context-free history so future turns keep continuity
        # without re-storing giant snapshots. We store the plain user text.
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": final_text or "(done)"})
        max_msgs = self.config.get("max_history_turns", 12) * 2
        if len(history) > max_msgs:
            del history[:-max_msgs]

        if actions:
            print("  actions:")
            for a in actions:
                print(f"    - {a}")
        return final_text or "Done."


# ---------------------------------------------------------------------------
# Discord bot glue
# ---------------------------------------------------------------------------
def _chunk(text, size=1900):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _ollama_ready(host, model):
    """Best-effort check that Ollama is up and the model is present."""
    try:
        r = requests.get(f"{host.rstrip('/')}/api/tags", timeout=5)
        if r.status_code >= 400:
            return None
        names = [m.get("name", "") for m in r.json().get("models", [])]
        # Model names may carry a :tag suffix (e.g. llama3.1:latest).
        have = any(n == model or n.split(":")[0] == model.split(":")[0] for n in names)
        return have
    except requests.RequestException:
        return None


def run_server(config_path="server_config.json"):
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild_id:
        print("DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in your .env file.")
        return

    try:
        import discord
    except ImportError:
        print("discord.py is not installed. Run: pip install -r requirements.txt")
        return

    config = load_server_config(config_path)
    host = config.get("ollama_host", "http://localhost:11434")
    model = config.get("model", "llama3.1")

    ready = _ollama_ready(host, model)
    if ready is None:
        print(f"[warn] couldn't reach Ollama at {host} — is it running? (ollama serve)")
        print("       Continuing anyway; requests will fail until it's up.")
    elif ready is False:
        print(f"[warn] Ollama is up at {host} but model '{model}' isn't pulled.")
        print(f"       Run:  ollama pull {model}")

    api = DiscordAPI(token)
    manager = ServerManager(api, guild_id, allow_deletes=config.get("allow_deletes", True))
    agent = ManagerAgent(manager, config)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    admin_channel = str(config["admin_channel"]).lstrip("#")

    def _is_admin_channel(channel):
        return (str(getattr(channel, "id", "")) == admin_channel
                or getattr(channel, "name", None) == admin_channel)

    @client.event
    async def on_ready():
        print(f"Connected as {client.user}. Listening in #{admin_channel} on guild {guild_id}.")
        print(f"AI: Ollama model '{model}' at {host}.")
        print("Talk to it in that channel — e.g. 'add a #announcements channel that only mods can post in'.")

    @client.event
    async def on_message(message):
        if message.author.bot or message.author == client.user:
            return
        if str(getattr(message.guild, "id", "")) != str(guild_id):
            return
        if not _is_admin_channel(message.channel):
            return
        if not message.content.strip():
            return
        if config.get("require_manage_guild", True):
            perms = getattr(message.author, "guild_permissions", None)
            if not (perms and (perms.manage_guild or perms.administrator)):
                await message.reply("You need the Manage Server permission to command me.",
                                    mention_author=False)
                return

        async with message.channel.typing():
            try:
                reply = await asyncio.to_thread(
                    agent.run_turn, str(message.channel.id), message.content.strip())
            except Exception as e:  # surface anything rather than dying silently
                reply = f"Something went wrong: {e}"
        for chunk in _chunk(reply):
            await message.channel.send(chunk)

    client.run(token)


if __name__ == "__main__":
    run_server()
