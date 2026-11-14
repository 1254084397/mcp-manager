"""Discover MCP config files that are not covered by known Agent profiles."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from . import formats, paths

DISCOVERY_NAMES = {
    "mcp.json",
    ".mcp.json",
    "mcp_config.json",
    "opencode.json",
    "opencode.jsonc",
    "cline_mcp_settings.json",
    "claude_desktop_config.json",
}

SKIP_DIRS = {
    "node_modules",
    "logs",
    "Cache",
    "cache",
    "Cached",
    "CachedProfilesData",
    "CachedConfigurations",
    "workspaceStorage",
    "mcp-tools",
    ".git",
    ".venv",
    "venv",
    "env",
}

DEFAULT_ROOTS = [
    paths.APPDATA / "Code" / "User",
    paths.APPDATA / "Code" / "agentPlugins",
    paths.APPDATA / "Codex",
    paths.LOCALAPPDATA / "Codex",
    paths.LOCALAPPDATA / "copilot" / "marketplaces",
    paths.APPDATA / "Claude",
    paths.APPDATA / "Cursor" / "User",
    paths.HOME / ".config",
    paths.HOME / ".codex",
    paths.HOME / ".claude",
    paths.HOME / ".cursor",
    paths.HOME / ".codeium" / "windsurf",
    paths.HOME / ".continue",
    paths.HOME / ".cline",
    paths.HOME / ".vscode",
    paths.HOME / ".gemini",
    paths.HOME / ".trae-cn",
    paths.HOME / ".trae-aicc",
]


def extra_roots() -> list[Path]:
    file = paths.DISCOVERY_ROOTS_FILE
    if not file.exists():
        return []
    roots = []
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            roots.append(Path(text).expanduser())
    return roots


def source_type(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    if parts & {"marketplaces", "agentplugins", "plugins"}:
        return "插件/市场清单"
    return "Agent/用户配置"


def guess_format(path: Path) -> str:
    if path.suffix.lower() == ".toml":
        return "toml"
    if path.suffix.lower() == ".jsonc":
        return "jsonc"
    return "json"


def discover_configs(profile_paths: list[Path] | None = None) -> list[dict]:
    """Return ``{path, sourceType, servers}`` for every readable config."""
    roots = list(DEFAULT_ROOTS) + extra_roots()
    found: list[dict] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = os.path.normcase(str(path))
        if key in seen or not path.is_file():
            return
        seen.add(key)
        try:
            fmt = guess_format(path)
            data = formats.load_config(path, fmt)
            section = data.get("mcpServers") or data.get("servers") or data.get("mcp") or data.get("mcp_servers")
            if not isinstance(section, dict) or not section:
                return
            found.append({"path": str(path), "sourceType": source_type(path), "servers": list(section)})
        except Exception:
            return

    for path in profile_paths or []:
        add(path)

    for root in roots:
        if not root.exists():
            continue
        try:
            for current, dirs, files in os.walk(root):
                current_path = Path(current)
                depth = len(current_path.relative_to(root).parts)
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                if depth >= 7:
                    dirs[:] = []
                for filename in files:
                    is_codex_toml = filename == "config.toml" and current_path.name.lower() == ".codex"
                    if filename not in DISCOVERY_NAMES and not is_codex_toml:
                        continue
                    add(current_path / filename)
        except (OSError, PermissionError):
            continue
    return found


def scan_report(registry_data: dict, profile_paths: list[Path] | None = None) -> tuple[dict, list[str]]:
    from . import registry

    known = set(registry.servers(registry_data))
    found = discover_configs(profile_paths)
    discovered: dict[str, list[dict]] = {}
    for item in found:
        for server in item["servers"]:
            discovered.setdefault(server, []).append({"path": item["path"], "sourceType": item["sourceType"]})

    def registered_as(name: str) -> str | None:
        wanted = registry.canonical(name)
        for candidate in known:
            if registry.canonical(candidate) == wanted:
                return candidate
        return None

    report = {
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "knownRegistryServers": sorted(known),
        "discoveredServers": {
            name: {"registered": registered_as(name) is not None, "matchesRegistryAs": registered_as(name), "configs": items}
            for name, items in sorted(discovered.items())
        },
        "configFiles": found,
    }
    unknown = sorted(name for name in discovered if registered_as(name) is None)
    return report, unknown
