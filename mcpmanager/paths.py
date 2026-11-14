"""Filesystem layout and path placeholders.

All personal data (registry, backups, candidates, reports) lives in ``local/`` so
the repository itself stays publishable. ``MCP_MANAGER_HOME`` relocates the
whole tree and ``MCP_MANAGER_DATA`` only relocates the writable data folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

APP_NAME = "mcp-manager"

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")


def _project_root() -> Path:
    override = os.environ.get("MCP_MANAGER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for candidate in (exe_dir, exe_dir.parent):
            if (candidate / "local").is_dir():
                return candidate
        return exe_dir
    return Path(__file__).resolve().parent.parent


def _resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
RESOURCE_ROOT = _resource_root()
DATA_DIR = Path(os.environ.get("MCP_MANAGER_DATA") or (PROJECT_ROOT / "local")).expanduser()

REGISTRY_PATH = DATA_DIR / "registry.json"
BACKUP_DIR = DATA_DIR / "backups"
GENERATED_DIR = DATA_DIR / "generated"
REPORT_DIR = DATA_DIR / "reports"
DISCOVERY_ROOTS_FILE = DATA_DIR / "discovery-roots.txt"
USER_PROFILE_DIR = DATA_DIR / "agents"

PROFILE_DIR = Path(os.environ.get("MCP_MANAGER_PROFILES") or (RESOURCE_ROOT / "mcpmanager" / "agents"))
CATALOG_PATH = RESOURCE_ROOT / "mcpmanager" / "catalog" / "servers.json"
ICON_PATH = RESOURCE_ROOT / "assets" / "icon.ico"

PLACEHOLDERS = {
    "home": HOME,
    "appdata": APPDATA,
    "localappdata": LOCALAPPDATA,
    "project": PROJECT_ROOT,
    "data": DATA_DIR,
}


class ReadOnlyError(Exception):
    """A mutation was attempted while ``MCP_MANAGER_READ_ONLY`` is active."""


def read_only() -> bool:
    value = os.environ.get("MCP_MANAGER_READ_ONLY", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def require_writable(action: str) -> None:
    if read_only():
        raise ReadOnlyError(f"只读模式：禁止{action}")


def expand_path(template: str) -> Path:
    """Resolve ``{home}``/``{appdata}`` placeholders in a profile path."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = PLACEHOLDERS.get(key)
        return str(value) if value is not None else match.group(0)

    return Path(re.sub(r"\{(\w+)\}", replace, template)).expanduser()


def ensure_dirs() -> None:
    for path in (DATA_DIR, BACKUP_DIR, GENERATED_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


EMPTY_REGISTRY = {"schemaVersion": 1, "servers": {}}


def ensure_layout() -> None:
    """Create writable folders and seed a registry on first run."""
    ensure_dirs()
    if read_only() or REGISTRY_PATH.exists():
        return
    legacy = PROJECT_ROOT / "registry" / "mcp-registry.json"
    if legacy.exists():
        shutil.copy2(legacy, REGISTRY_PATH)
        return
    REGISTRY_PATH.write_text(
        json.dumps(EMPTY_REGISTRY, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
