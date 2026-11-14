"""Health checks for MCP commands, URLs and local runtimes."""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import adapters, formats, paths, versioning
from .model import Profile


def check_command(command: str | None) -> tuple[bool, str]:
    if not command:
        return False, "未配置 command"
    candidate = Path(command)
    if candidate.is_absolute() or any(sep in command for sep in ("/", "\\")):
        return candidate.exists(), str(candidate)
    found = shutil.which(command)
    return bool(found), found or "PATH 中未找到"


def check_url(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "mcp-manager"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # Any HTTP answer proves the endpoint exists; 4xx often means auth needed.
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return False, type(exc).__name__


def diagnose_server(profile: Profile, name: str, spec: dict[str, Any], network: bool = False) -> dict[str, Any]:
    command = adapters.command_of(spec)
    url = adapters.url_of(spec, profile)
    row: dict[str, Any] = {
        "name": name,
        "transport": "remote" if url else "stdio",
        "command": command or "",
        "args": adapters.args_of(spec),
        "available": None,
        "detail": "",
        "problems": [],
    }
    if url:
        if network:
            ok, detail = check_url(url)
            row["available"] = ok
            row["detail"] = f"{url}（{detail}）"
            if not ok:
                row["problems"].append("URL 不可达")
        else:
            row["detail"] = url
        return row
    ok, detail = check_command(command)
    row["available"] = ok
    row["detail"] = detail
    if not ok:
        row["problems"].append("command 不可用")
    missing = [arg for arg in row["args"] if _looks_like_missing_path(arg)]
    if missing:
        row["problems"].append("参数路径不存在：" + ", ".join(missing))
    return row


def _looks_like_missing_path(arg: str) -> bool:
    if not arg or arg.startswith("-") or "://" in arg:
        return False
    text = arg.split("==", 1)[0]
    candidate = Path(text)
    looks_like_path = candidate.is_absolute() and (candidate.suffix or "\\" in text or "/" in text)
    return bool(looks_like_path and not candidate.exists())


def diagnose(profile: Profile, network: bool = False) -> list[dict[str, Any]]:
    if not profile.path.exists():
        raise FileNotFoundError(f"{profile.name} 配置不存在：{profile.path}")
    section = formats.read_section(profile.path, profile.format, profile.schema)
    return [diagnose_server(profile, name, spec, network) for name, spec in section.items() if isinstance(spec, dict)]


def environment_report() -> list[dict[str, Any]]:
    rows = []
    for label, names in (
        ("Node.js", ("node.exe", "node")),
        ("npm", ("npm.cmd", "npm")),
        ("uvx", ("uvx.exe", "uvx")),
        ("Git", ("git.exe", "git")),
        ("Git Bash", (r"C:\Program Files\Git\bin\bash.exe",)),
    ):
        found = versioning.find_tool(*names) if not names[0].startswith("C:") else (
            names[0] if Path(names[0]).exists() else None
        )
        rows.append({"name": label, "available": bool(found), "path": found or ""})
    if os.name != "nt" and rows[-1]["name"] == "Git Bash":
        rows.pop()
    return rows


def project_paths() -> dict[str, str]:
    return {
        "project": str(paths.PROJECT_ROOT),
        "data": str(paths.DATA_DIR),
        "registry": str(paths.REGISTRY_PATH),
        "backups": str(paths.BACKUP_DIR),
        "generated": str(paths.GENERATED_DIR),
    }
