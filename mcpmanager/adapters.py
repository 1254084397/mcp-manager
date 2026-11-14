"""Translate between the canonical server shape and each Agent's shape.

Canonical shape::

    {"command": "node", "args": ["server.js"], "env": {"K": "V"}}
    {"url": "https://example.com/mcp"}

Agent profiles describe the deviations: OpenCode wants a command array and an
``environment`` key, VS Code/Copilot want an explicit ``type``, Copilot adds
``tools``, and so on.
"""

from __future__ import annotations

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
}

SECRET_MARKERS = ("authorization", "api_key", "apikey", "token", "secret", "password")


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


def has_inline_secret(value: Any, key: str = "") -> bool:
    if any(marker in key.lower() for marker in SECRET_MARKERS):
        return True
    if isinstance(value, dict):
        return any(has_inline_secret(item, str(name)) for name, item in value.items())
    if isinstance(value, list):
        return any(has_inline_secret(item, key) for item in value)
    return False


def server_names(section: dict[str, Any]) -> list[str]:
    return list(section)
