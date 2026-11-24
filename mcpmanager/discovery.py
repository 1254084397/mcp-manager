"""Discover MCP config files that are not covered by known Agent profiles."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from . import adapters, formats, paths

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
    paths.CODEX_HOME,
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


def source_type(path: Path, effective_paths: set[str] | None = None) -> str:
    """Grade a config file so cache/backup hits are not mistaken for live ones."""
    if effective_paths and os.path.normcase(str(path)) in effective_paths:
        return LEVEL_EFFECTIVE
    parts = {part.lower() for part in path.parts}
    filename = path.name.lower()
    if "candidate" in filename or "generated" in parts:
        return LEVEL_CANDIDATE
    if parts & {"backups", "backup"} or "-before-apply" in filename or "before-restore" in filename or "legacy" in filename or filename.endswith(".bak"):
        return LEVEL_BACKUP
    if "marketplaces" in parts:
        return LEVEL_MARKET
    if parts & {"agentplugins", "plugins"}:
        return LEVEL_PLUGIN
    if parts & USER_ROOT_MARKERS:
        return LEVEL_USER
    return LEVEL_OTHER


LEVEL_EFFECTIVE = "当前生效配置"
LEVEL_USER = "用户配置"
LEVEL_PLUGIN = "插件配置"
LEVEL_MARKET = "市场缓存"
LEVEL_BACKUP = "历史备份"
LEVEL_CANDIDATE = "候选配置"
LEVEL_OTHER = "其他"

LEVELS = (
    LEVEL_EFFECTIVE,
    LEVEL_USER,
    LEVEL_PLUGIN,
    LEVEL_MARKET,
    LEVEL_BACKUP,
    LEVEL_CANDIDATE,
    LEVEL_OTHER,
)

USER_ROOT_MARKERS = {
    ".gemini",
    ".codex",
    ".claude",
    ".cursor",
    ".codeium",
    ".continue",
    ".cline",
    ".vscode",
    ".trae-cn",
    ".trae-aicc",
    ".config",
    "code",
    "claude",
    "cursor",
    "codex",
}


def _is_host_spec(spec: dict) -> bool:
    command = adapters.command_of(spec)
    if not command:
        return False
    path = str(command).replace("\\", "/").lower()
    return any(marker in path for marker in adapters.HOST_PATH_MARKERS)


def guess_format(path: Path) -> str:
    if path.suffix.lower() == ".toml":
        return "toml"
    if path.suffix.lower() == ".jsonc":
        return "jsonc"
    return "json"


def discover_configs(profile_paths: list[Path] | None = None) -> list[dict]:
    """Return ``{path, sourceType, effective, servers}`` for every readable config."""
    roots = list(DEFAULT_ROOTS)
    dynamic_codex = paths.codex_home()
    if dynamic_codex not in roots:
        roots.append(dynamic_codex)
    roots.extend(extra_roots())
    found: list[dict] = []
    seen: set[str] = set()
    effective_paths = {os.path.normcase(str(path)) for path in profile_paths or []}

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
            names = [
                name for name, spec in section.items()
                if isinstance(spec, dict) and not adapters.is_container(spec)
            ]
            if not names:
                return
            hosts = [name for name in names if _is_host_spec(section[name])]
            level = source_type(path, effective_paths)
            found.append({
                "path": str(path),
                "sourceType": level,
                "level": level,
                "effective": level == LEVEL_EFFECTIVE,
                "servers": names,
                "hosts": hosts,
            })
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
            discovered.setdefault(server, []).append({
                "path": item["path"],
                "sourceType": item["sourceType"],
                "level": item["level"],
                "effective": item["effective"],
                "host": server in item.get("hosts", []),
            })

    def registered_as(name: str) -> str | None:
        wanted = registry.canonical(name)
        for candidate in known:
            if registry.canonical(candidate) == wanted:
                return candidate
        return None

    def group(predicate) -> dict[str, dict]:
        return {
            name: {
                "registered": registered_as(name) is not None,
                "matchesRegistryAs": registered_as(name),
                "configs": items,
            }
            for name, items in sorted(discovered.items())
            if predicate(items)
        }

    def has(level: str):
        return lambda items: any(item["level"] == level for item in items)

    effective = group(has(LEVEL_EFFECTIVE))
    user = group(has(LEVEL_USER))
    cached = group(
        lambda items: all(item["level"] != LEVEL_EFFECTIVE for item in items)
        and any(item["level"] in (LEVEL_PLUGIN, LEVEL_MARKET, LEVEL_BACKUP, LEVEL_CANDIDATE, LEVEL_OTHER)
                for item in items)
    )

    report = {
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "knownRegistryServers": sorted(known),
        "discoveredServers": {
            name: {"registered": registered_as(name) is not None, "matchesRegistryAs": registered_as(name), "configs": items}
            for name, items in sorted(discovered.items())
        },
        "effectiveServers": effective,
        "userServers": user,
        "cachedServers": cached,
        "configFiles": found,
    }

    def host_only(name: str) -> bool:
        entries = [item for item in discovered[name] if item["level"] == LEVEL_EFFECTIVE]
        return bool(entries) and all(item.get("host") for item in entries)

    unknown = sorted(
        name for name in effective
        if registered_as(name) is None and not host_only(name)
    )
    report["unknownEffective"] = unknown
    report["unknownUser"] = sorted(name for name in user if registered_as(name) is None)
    report["unknownCached"] = sorted(name for name in cached if registered_as(name) is None)
    report["hostServers"] = sorted(name for name in discovered if host_only(name))
    return report, unknown
