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
    ollama pull gemma4:12b      # native tool-calling; or qwen2.5, llama3.1, etc.

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
import re

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
    "model": "gemma4-heretic:12b",
    # Sampling temperature for the model.
    "temperature": 0.0,
    # Channel the bot listens in. Matched by name (without '#') or by raw ID.
    "admin_channel": "bot-manager-chat",
    # Only members with the Manage Server permission may command the bot.
    "require_manage_guild": True,
    # Allow destructive actions (deleting roles/channels).
    "allow_deletes": True,
    # How many prior turns of conversation to keep per channel.
    "max_history_turns": 12,
    # Ollama context window to request for each chat call.
    "ollama_context_size": 8192,
    # Maximum compact server snapshot characters to send before summarizing.
    "max_snapshot_chars": 8000,
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
    if os.environ.get("OLLAMA_NUM_CTX"):
        cfg["ollama_context_size"] = os.environ["OLLAMA_NUM_CTX"]
    if os.environ.get("OLLAMA_MAX_SNAPSHOT_CHARS"):
        cfg["max_snapshot_chars"] = os.environ["OLLAMA_MAX_SNAPSHOT_CHARS"]
    return cfg


def _int_config(config, key, default, minimum=0):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


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
        permission_error = _permission_validation_error(permissions=permissions)
        if permission_error:
            return permission_error
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
        permission_error = _permission_validation_error(permissions=permissions)
        if permission_error:
            return permission_error
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
        permission_error = _permission_validation_error(allow=allow, deny=deny)
        if permission_error:
            return permission_error
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


def _permission_validation_error(**fields):
    for field, names in fields.items():
        if names is None:
            continue
        if not isinstance(names, list):
            return f"{field} must be a list of Discord permission flag names."
        unknown = [str(name) for name in names if name not in _PERM_LIST]
        if unknown:
            return f"Unknown permission flag(s) in {field}: {', '.join(unknown)}."
    return None


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
    perm_items = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Discord permission flag names listed in the system prompt. Empty list means no permissions.",
    }
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


SYSTEM_PROMPT = f"""You are a Discord server manager operating live inside a Discord server via an admin channel.

Members talk to you in plain language and you carry out changes to THIS server by calling tools. Guidelines:
- Before every message you are given a fresh JSON snapshot of the current server state (guild settings, roles with decoded permissions, categories, and channels). Treat it as the source of truth for what currently exists — never assume; read the snapshot.
- Refer to roles and channels by their exact names as shown in the snapshot.
- Only use these permission flag names in permissions, allow, or deny arrays: {", ".join(_PERM_LIST)}.
- To fully strip a role, pass an empty permissions list.
- Roles higher in the hierarchy have more authority; the bot can only manage roles below its own top role, and cannot grant permissions it doesn't have.
- CRITICAL: the ONLY way anything actually changes on the server is by calling a tool. Describing a change in words does NOT perform it. NEVER say you created, edited, deleted, renamed, moved, or set up anything unless you called the matching tool in this same turn and saw its result. Do not narrate a plan instead of acting — call the tool.
- Be decisive for clearly-specified requests: call the tool(s) to make the change, then briefly confirm what you did in one or two sentences.
- A single request often needs SEVERAL tool calls (e.g. create a channel, THEN set a role's permissions on it; or create a category, THEN create channels inside it). Keep calling tools until the WHOLE request is done, using each tool's result and the snapshot to decide the next call. Only write your final confirmation once nothing is left to do.
- For destructive actions (deleting roles or channels) or genuinely ambiguous requests, ask ONE short clarifying question in your reply INSTEAD of calling a tool, and wait for confirmation.
- If a tool returns an error, explain it plainly and suggest a fix; don't silently retry the same thing.
- Keep replies concise and readable — lead with the outcome. This is a chat channel, not a report."""


class OllamaContextError(RuntimeError):
    pass


# Words a reply uses when it CLAIMS to have made a change. Used to catch the
# model "daydreaming" — narrating an action it never performed via a tool call.
_CLAIMS_ACTION = re.compile(
    r"\b(created?|added?|made|renamed?|deleted?|removed?|updated?|changed?|"
    r"set up|set the|configured?|assigned?|granted?|revoked?|moved?|"
    r"i'?ll|i have|i've|i just|done|all set)\b",
    re.IGNORECASE,
)


def _compact_overwrites(overwrites):
    compact = []
    for overwrite in overwrites or []:
        role = overwrite.get("role")
        if not role:
            continue
        item = {"role": role}
        allow = overwrite.get("allow") or []
        deny = overwrite.get("deny") or []
        if allow:
            item["allow"] = allow
        if deny:
            item["deny"] = deny
        compact.append(item)
    return compact


def _compact_snapshot(snapshot, include_meta=True):
    compact = {
        "_notes": [
            "roles are ordered lowest to highest",
            "missing color means #000000; missing hoist/mentionable means false",
            "missing allow/deny/overwrites means empty",
        ],
        "guild": snapshot.get("guild") or {},
        "roles": [],
        "categories": [],
    }

    for role in snapshot.get("roles") or []:
        item = {
            "name": role.get("name"),
            "permissions": role.get("permissions") or [],
        }
        color = role.get("color")
        if color and color != "#000000":
            item["color"] = color
        if role.get("hoist"):
            item["hoist"] = True
        if role.get("mentionable"):
            item["mentionable"] = True
        compact["roles"].append(item)

    for category in snapshot.get("categories") or []:
        item = {"name": category.get("name"), "channels": []}
        overwrites = _compact_overwrites(category.get("overwrites"))
        if overwrites:
            item["overwrites"] = overwrites
        for channel in category.get("channels") or []:
            channel_item = {
                "name": channel.get("name"),
                "type": channel.get("type", "text"),
            }
            channel_overwrites = _compact_overwrites(channel.get("overwrites"))
            if channel_overwrites:
                channel_item["overwrites"] = channel_overwrites
            item["channels"].append(channel_item)
        compact["categories"].append(item)

    if include_meta:
        meta = snapshot.get("meta") or {}
        compact["meta"] = {
            key: meta.get(key)
            for key in (
                "guild_id",
                "approximate_member_count",
                "approximate_presence_count",
                "premium_tier",
                "premium_subscription_count",
                "channel_types",
            )
            if meta.get(key) is not None
        }

    return compact


def _summary_snapshot(snapshot, max_chars):
    categories = []
    for category in snapshot.get("categories") or []:
        channels = [
            {"name": channel.get("name"), "type": channel.get("type", "text")}
            for channel in category.get("channels") or []
        ]
        categories.append({"name": category.get("name"), "channels": channels})

    summary = {
        "_warning": (
            "Snapshot summarized because it exceeded the configured prompt budget. "
            "Ask a narrower question before replacing permissions if needed details are missing."
        ),
        "guild": snapshot.get("guild") or {},
        "roles_low_to_high": [role.get("name") for role in snapshot.get("roles") or []],
        "categories": categories,
        "counts": {
            "roles": len(snapshot.get("roles") or []),
            "categories": len(snapshot.get("categories") or []),
            "channels": sum(len(category.get("channels") or []) for category in snapshot.get("categories") or []),
        },
    }
    text = json.dumps(summary, separators=(",", ":"))
    if len(text) <= max_chars:
        return text

    # Last-resort valid JSON for very large servers or very small budgets.
    role_names = summary["roles_low_to_high"]
    category_names = [category.get("name") for category in categories]
    reduced = {
        "_warning": summary["_warning"],
        "guild": summary["guild"],
        "roles_low_to_high": role_names[:75],
        "categories": category_names[:75],
        "counts": summary["counts"],
    }
    if len(role_names) > 75:
        reduced["roles_omitted"] = len(role_names) - 75
    if len(category_names) > 75:
        reduced["categories_omitted"] = len(category_names) - 75
    return json.dumps(reduced, separators=(",", ":"))


def _snapshot_payload(snapshot, max_chars):
    max_chars = max(1200, max_chars)
    for compact in (_compact_snapshot(snapshot, include_meta=True),
                    _compact_snapshot(snapshot, include_meta=False)):
        text = json.dumps(compact, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
    return _summary_snapshot(snapshot, max_chars)


def _ollama_error_text(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = data.get("error") if isinstance(data, dict) else data
    if isinstance(error, str):
        try:
            nested = json.loads(error)
        except json.JSONDecodeError:
            return error
        if isinstance(nested, dict):
            nested_error = nested.get("error")
            if isinstance(nested_error, dict):
                return nested_error.get("message") or json.dumps(nested_error)
            return nested.get("message") or json.dumps(nested)
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)
    return str(error)


# ---------------------------------------------------------------------------
# The agent: one Ollama tool-use loop per user message
# ---------------------------------------------------------------------------
class ManagerAgent:
    def __init__(self, manager, config):
        self.manager = manager
        self.config = config
        self.tools = build_tools()
        self.host = config.get("ollama_host", "http://localhost:11434").rstrip("/")
        self.model = config.get("model", "gemma4-heretic:12b")
        self.history = {}  # channel_id -> list[message dict]

    def _chat(self, messages):
        """One round-trip to Ollama's /api/chat (non-streaming)."""
        options = {"temperature": self.config.get("temperature", 0.0)}
        num_ctx = _int_config(self.config, "ollama_context_size", 8192, minimum=0)
        if num_ctx:
            options["num_ctx"] = num_ctx
        resp = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "tools": self.tools,
                "stream": False,
                "options": options,
            },
            timeout=600,
        )
        if resp.status_code >= 400:
            error_text = _ollama_error_text(resp.text)
            if ("exceed_context_size" in resp.text
                    or "exceeds the available context size" in error_text):
                raise OllamaContextError(error_text)
            raise RuntimeError(f"Ollama returned {resp.status_code}: {error_text}")
        return resp.json().get("message", {})

    def _system_prompt(self):
        return SYSTEM_PROMPT + (
            f"\n\nAdditional house rules:\n{self.config['persona']}"
            if self.config.get("persona") else "")

    def _context_block(self, snapshot, user_text, max_snapshot_chars):
        payload = _snapshot_payload(snapshot, max_snapshot_chars)
        return (
            "Current server state (fresh compact snapshot):\n```json\n"
            + payload
            + "\n```\n\nRequest: "
            + user_text
        )

    def _estimate_prompt_tokens(self, messages):
        payload = json.dumps({"messages": messages, "tools": self.tools}, separators=(",", ":"))
        return max(1, len(payload) // 4)

    def _build_messages(self, history, snapshot, user_text, minimal=False):
        context_size = _int_config(self.config, "ollama_context_size", 8192, minimum=1024)
        max_snapshot_chars = _int_config(self.config, "max_snapshot_chars", 8000, minimum=1200)
        if minimal:
            history_options = [[]]
            snapshot_limits = [min(max_snapshot_chars, 2400), 1200]
            target_tokens = int(context_size * 0.55)
        else:
            history_options = [history, history[-8:], history[-4:], []]
            snapshot_limits = [max_snapshot_chars, max_snapshot_chars // 2, max_snapshot_chars // 4, 1200]
            target_tokens = int(context_size * 0.8)

        seen = set()
        last_messages = None
        for candidate_history in history_options:
            history_key = len(candidate_history)
            if history_key in seen:
                continue
            seen.add(history_key)
            for snapshot_limit in snapshot_limits:
                messages = ([{"role": "system", "content": self._system_prompt()}]
                            + list(candidate_history)
                            + [{"role": "user", "content": self._context_block(
                                snapshot, user_text, snapshot_limit)}])
                last_messages = messages
                if self._estimate_prompt_tokens(messages) <= target_tokens:
                    return messages
        return last_messages

    def run_turn(self, channel_id, user_text):
        """Blocking: fetch fresh context, run the tool loop, return reply text.
        Runs off the event loop via asyncio.to_thread."""
        snapshot = self.manager.snapshot()

        history = self.history.setdefault(channel_id, [])
        messages = self._build_messages(history, snapshot, user_text)

        actions = []
        final_text = ""
        retried_with_minimal_context = False
        nudged = False
        for _ in range(12):  # safety bound on tool round-trips
            try:
                msg = self._chat(messages)
            except OllamaContextError:
                if actions:
                    final_text = (
                        "The change was applied, but Ollama could not fit the follow-up response "
                        "inside this model's context window."
                    )
                    break
                if retried_with_minimal_context:
                    final_text = (
                        "The server snapshot is still too large for this Ollama model's context window. "
                        "Increase ollama_context_size in server_config.json or ask about a narrower role/channel."
                    )
                    break
                retried_with_minimal_context = True
                messages = self._build_messages([], snapshot, user_text, minimal=True)
                continue
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
                    actions.append((name, out))
                    messages.append({"role": "tool", "tool_name": name, "content": out})
                continue

            content = msg.get("content", "") or ""
            # Anti-daydream: the model wrote prose but called no tool. If it CLAIMED
            # a change (created/renamed/…) yet did nothing, and it isn't asking a
            # question, nudge it once to actually call the tool instead of pretending.
            if (not actions and not nudged and "?" not in content
                    and _CLAIMS_ACTION.search(content)):
                nudged = True
                messages.append({
                    "role": "user",
                    "content": ("You described a change but called no tool, so NOTHING happened. "
                                "Call the correct tool now to actually perform it. If the request "
                                "is unclear or destructive, ask one short question instead."),
                })
                continue

            final_text = content
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
            for name, out in actions:
                print(f"    - {name} → {out}")
        return final_text or "Done.", actions


# ---------------------------------------------------------------------------
# Discord bot glue
# ---------------------------------------------------------------------------
def _chunk(text, size=1900):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _format_reply(reply, actions):
    """Combine the model's reply with a log of every change it applied, so the
    channel shows both what it said and exactly what it did."""
    if not actions:
        return reply
    log = "\n".join(f"• {out}" for _, out in actions)
    return f"{reply}\n\n**🛠️ Changes applied:**\n{log}"


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
    model = config.get("model", "gemma4-heretic:12b")

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
                reply, actions = await asyncio.to_thread(
                    agent.run_turn, str(message.channel.id), message.content.strip())
            except Exception as e:  # surface anything rather than dying silently
                reply, actions = f"Something went wrong: {e}", []
        for chunk in _chunk(_format_reply(reply, actions)):
            await message.channel.send(chunk)

    client.run(token)


if __name__ == "__main__":
    run_server()
