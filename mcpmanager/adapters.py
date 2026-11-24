"""Translate between the canonical server shape and each Agent's shape.

Canonical shape::

    {"command": "node", "args": ["server.js"], "env": {"K": "V"}}
    {"url": "https://example.com/mcp"}

Agent profiles describe the deviations: OpenCode wants a command array and an
``environment`` key, VS Code/Copilot want an explicit ``type``, Copilot adds
``tools``, and so on.
"""

from __future__ import annotations

import re
from typing import Any

from .model import Profile

META_KEYS = {
    "kind",
    "package",
    "version",
    "installedVersion",
    "versionPolicy",
    "agents",
    "secretSource",
    "authEnvironment",
    "runtimeNote",
    "status",
    "source",
    "description",
    "homepage",
    "envFromFile",
    "updateCommand",
}

SECRET_MARKERS = ("authorization", "api_key", "apikey", "token", "secret", "password", "xerj_auth")

SOURCE_CATEGORY_LABELS = {
    "managed": "mcp-manager 管理",
    "magpie_managed": "Magpie 管理",
    "user_config": "用户配置",
    "plugin_host": "插件/宿主",
    "remote": "远程",
    "backup": "历史备份",
    "candidate": "候选配置",
    "market_cache": "市场缓存",
    "unknown": "未知",
}

_SECRET_TEXT_PATTERNS = (
    (re.compile(r"(?i)\b(Bearer|ApiKey)\s+[A-Za-z0-9._\-+=/]{6,}"), r"\1 ***"),
    (re.compile(r"\b(github_pat_|ghp_|gho_|ghu_|sk-)[A-Za-z0-9_]{8,}"), r"\1***"),
    (re.compile(r"(?i)\b(authorization|api_key|apikey|token|secret|password|xerj_auth)\s*[:=]\s*[^\s,;\"']+"), r"\1=***"),
)


def redact_text(text: str) -> str:
    """Mask inline tokens/credentials in log, diff, or exception strings."""
    cleaned = str(text)
    for pattern, replacement in _SECRET_TEXT_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def clean_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop registry bookkeeping so only runnable fields reach an Agent."""
    return {key: value for key, value in spec.items() if key not in META_KEYS}


def to_agent(spec: dict[str, Any], profile: Profile) -> dict[str, Any]:
    src = clean_spec(spec)
    command = src.pop("command", None)
    args = list(src.pop("args", None) or [])
    env = src.pop("env", None)
    if env is None:
        env = src.pop("environment", None)
    url = src.pop("url", None)
    if url is None:
        url = src.pop("serverUrl", None)
    src.pop("type", None)

    out: dict[str, Any] = {}
    if isinstance(command, (list, tuple)):
        args = list(command[1:]) + args
        command = command[0] if command else ""
    if command:
        if profile.command == "array":
            out["command"] = [command, *args]
        else:
            out["command"] = command
            if args:
                out["args"] = args
        if profile.transport:
            out["type"] = profile.stdio_type
        if env:
            out[profile.env] = env
    elif url:
        out[profile.url_key] = url
        if profile.transport:
            out["type"] = profile.http_type
        if env:
            out[profile.env] = env
    else:
        raise ValueError("MCP 配置缺少 command 和 url")

    out.update(src)
    if profile.enabled:
        out.setdefault("enabled", True)
    if profile.tools:
        out.setdefault("tools", ["*"])
    return out


def from_agent(spec: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Best-effort canonical view of a live Agent entry (read-only use)."""
    out = dict(spec)
    command = out.get("command")
    if isinstance(command, (list, tuple)):
        out["command"] = command[0] if command else ""
        if len(command) > 1:
            out["args"] = list(command[1:])
    if profile.env != "env" and profile.env in out:
        out.setdefault("env", out.pop(profile.env))
    if profile.url_key != "url" and profile.url_key in out:
        out.setdefault("url", out.pop(profile.url_key))
    out.pop("type", None)
    return out


def command_of(spec: dict[str, Any]) -> str | None:
    command = spec.get("command")
    if isinstance(command, (list, tuple)):
        return str(command[0]) if command else None
    return str(command) if command else None


def args_of(spec: dict[str, Any]) -> list[str]:
    command = spec.get("command")
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command[1:]] + [str(item) for item in spec.get("args", []) or []]
    return [str(item) for item in spec.get("args", []) or []]


def url_of(spec: dict[str, Any], profile: Profile) -> str | None:
    for key in (profile.url_key, "url", "serverUrl", "httpUrl"):
        value = spec.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# Commands living under these runtime/plugin folders are provided by the Agent
# itself; they are not installed or upgraded through the registry.
HOST_PATH_MARKERS = (
    "/openai/codex/runtimes/",
    "/codex-runtimes/",
    "/agentplugins/",
)


def is_container(spec: Any) -> bool:
    """True for nested wrappers such as ``{"servers": {...}}``, not real MCPs."""
    if not isinstance(spec, dict) or command_of(spec) or spec.get("url") or spec.get("type"):
        return False
    return any(isinstance(value, dict) for value in spec.values())


def level_of(spec: dict[str, Any], profile: Profile) -> str:
    """Classify a live entry: ``manageable``, ``host`` or ``invalid``."""
    if spec.get("kind") == "copilot-plugin-host":
        return "host"
    command = command_of(spec)
    url = url_of(spec, profile)
    if command:
        path = str(command).replace("\\", "/").lower()
        if any(marker in path for marker in HOST_PATH_MARKERS):
            return "host"
        return "manageable"
    if url:
        return "manageable"
    return "invalid"


LAYER_HOST = "agent-host"
LAYER_MAGPIE = "magpie-managed"
LAYER_MCP_MANAGER = "mcp-manager"
LAYER_USER = "user-config"
LAYER_BACKUP_CACHE = "backup-or-cache"

LAYER_LABELS = {
    LAYER_HOST: "Agent 插件/宿主内置 MCP",
    LAYER_MAGPIE: "Magpie 管理的 MCP",
    LAYER_MCP_MANAGER: "mcp-manager 管理的 MCP",
    LAYER_USER: "用户配置 MCP",
    LAYER_BACKUP_CACHE: "历史备份或缓存 MCP",
}


def is_magpie_managed(spec: dict[str, Any]) -> bool:
    """Detect whether an entry is managed by Magpie without touching any Magpie DB."""
    if not isinstance(spec, dict):
        return False
    if str(spec.get("managedBy", "")).strip().lower() == "magpie":
        return True
    for candidate in (spec.get("url"), spec.get("serverUrl"), spec.get("httpUrl"), command_of(spec)):
        if candidate and "magpie" in str(candidate).lower():
            return True
    for arg in args_of(spec):
        if "magpie" in arg.lower():
            return True
    for container_key in ("env", "environment", "headers"):
        mapping = spec.get(container_key)
        if isinstance(mapping, dict):
            if any("magpie" in str(k).lower() for k in mapping):
                return True
    return False


def source_layer(
    spec: dict[str, Any],
    profile: Profile,
    managed_entry: dict[str, Any] | None = None,
    is_backup_or_cache: bool = False,
) -> str:
    """Classify an MCP entry into one of the five capability/provenance layers."""
    if is_backup_or_cache:
        return LAYER_BACKUP_CACHE
    if level_of(spec, profile) == "host":
        return LAYER_HOST
    if is_magpie_managed(spec):
        return LAYER_MAGPIE
    if managed_entry is not None:
        return LAYER_MCP_MANAGER
    return LAYER_USER


def source_category(
    spec: Any,
    profile: Profile,
    managed_entry: dict[str, Any] | None = None,
    file_source_type: str | None = None,
) -> str:
    """Classify an MCP entry into Magpie-style canonical source categories."""
    if file_source_type == "历史备份":
        return "backup"
    if file_source_type == "候选配置":
        return "candidate"
    if file_source_type == "市场缓存":
        return "market_cache"
    if file_source_type == "插件配置":
        return "plugin_host"
    if not isinstance(spec, dict):
        return "unknown"
    if level_of(spec, profile) == "host":
        return "plugin_host"
    if is_magpie_managed(spec):
        return "magpie_managed"
    if managed_entry is not None:
        return "managed"
    if url_of(spec, profile):
        return "remote"
    if command_of(spec):
        return "user_config"
    return "unknown"


def has_inline_secret(value: Any, key: str = "") -> bool:
    if any(marker in key.lower() for marker in SECRET_MARKERS):
        return True
    if isinstance(value, dict):
        return any(has_inline_secret(item, str(name)) for name, item in value.items())
    if isinstance(value, list):
        return any(has_inline_secret(item, key) for item in value)
    return False


def redact_secrets(value: Any, key: str = "") -> Any:
    """Return a copy of ``value`` with secret-like keys or token strings masked as ``***``."""
    if key and any(marker in key.lower() for marker in SECRET_MARKERS):
        return "***" if value not in (None, "") else value
    if isinstance(value, dict):
        return {k: redact_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, key) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def server_names(section: dict[str, Any]) -> list[str]:
    return list(section)
