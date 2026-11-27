"""Multi-target runtime model: Windows local host and WSL Linux distros."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import adapters, formats, paths, profiles as profiles_mod

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

# Known WSL agent layouts (resolved relative to the detected WSL user's $HOME).
WSL_AGENT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "pi",
        "name": "Pi",
        "config": ".pi/agent/mcp.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".pi/agent/skills",
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "config": ".codex/config.toml",
        "format": "toml",
        "schema": "mcp_servers",
        "skills": ".codex/skills",
    },
    {
        "id": "antigravity",
        "name": "Antigravity",
        "config": ".gemini/config/mcp_config.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".gemini/skills",
    },
    {
        "id": "claude-code",
        "name": "Claude Code",
        "config": ".claude.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".claude/skills",
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "config": ".config/opencode/opencode.json",
        "format": "jsonc",
        "schema": "mcp",
        "skills": ".config/opencode/skills",
    },
)


@dataclass
class Target:
    """One execution environment (Windows host, WSL distro, or future SSH host)."""

    id: str
    name: str
    kind: str  # "windows" | "wsl" | "ssh"
    online: bool
    os_name: str
    user: str
    home: str
    distro: str = ""
    tools: dict[str, bool] = field(default_factory=dict)
    magpie_reachable: bool = False


def list_wsl_distros() -> list[str]:
    """Return installed WSL distro names safely, or ``[]`` when unavailable."""
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            timeout=4,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    raw = result.stdout
    text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode("utf-8", errors="ignore")
    return [line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()]


def run_wsl_bash(distro: str, script: str, timeout: int = 8) -> tuple[int, str, str]:
    """Run a bash command inside a WSL distro without assuming a fixed username."""
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "bash", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except Exception as exc:
        return 1, "", str(exc)


@lru_cache(maxsize=256)
def wsl_unc_path(distro: str, posix_path: str) -> Path:
    """Map a POSIX path inside ``distro`` to its Windows ``\\\\wsl.localhost`` UNC path."""
    rel = posix_path.lstrip("/").replace("/", "\\")
    return Path(f"\\\\wsl.localhost\\{distro}\\{rel}")


def _windows_target() -> Target:
    tools = {
        "node": bool(shutil.which("node.exe") or shutil.which("node")),
        "npm": bool(shutil.which("npm.cmd") or shutil.which("npm")),
        "uvx": bool(shutil.which("uvx.exe") or shutil.which("uvx")),
        "python": bool(shutil.which("python.exe") or shutil.which("python")),
        "git": bool(shutil.which("git.exe") or shutil.which("git")),
    }
    magpie_dir = paths.HOME / ".config" / "magpie"
    return Target(
        id="windows",
        name="Windows 本机",
        kind="windows",
        online=os.name == "nt",
        os_name=f"{platform.system()} {platform.release()}".strip(),
        user=os.environ.get("USERNAME") or paths.HOME.name,
        home=str(paths.HOME),
        tools=tools,
        magpie_reachable=magpie_dir.is_dir(),
    )


def _probe_wsl_target(distro: str) -> Target:
    probe_script = (
        'printf "USER=%s\\nHOME=%s\\nOS=%s\\n" '
        '"$(id -un 2>/dev/null || echo $USER)" '
        '"$HOME" '
        '"$(uname -sr 2>/dev/null || echo Linux)"; '
        'for t in node npm uvx python3 git; do '
        '  command -v "$t" >/dev/null 2>&1 && echo "TOOL_$t=1" || echo "TOOL_$t=0"; '
        'done'
    )
    code, out, _err = run_wsl_bash(distro, probe_script, timeout=6)
    kv: dict[str, str] = {}
    if code == 0 and out:
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()
    online = code == 0 and bool(kv.get("HOME"))
    tools = {
        "node": kv.get("TOOL_node") == "1",
        "npm": kv.get("TOOL_npm") == "1",
        "uvx": kv.get("TOOL_uvx") == "1",
        "python": kv.get("TOOL_python3") == "1",
        "git": kv.get("TOOL_git") == "1",
    }
    return Target(
        id=f"wsl:{distro}",
        name=f"WSL {distro}",
        kind="wsl",
        online=online,
        os_name=kv.get("OS", "Linux (WSL)"),
        user=kv.get("USER", ""),
        home=kv.get("HOME", ""),
        distro=distro,
        tools=tools,
        magpie_reachable=(paths.HOME / ".config" / "magpie").is_dir(),
    )


def list_targets(include_wsl: bool = True) -> list[Target]:
    """Return Windows local target and any detected WSL distros."""
    items = [_windows_target()]
    if include_wsl and os.name == "nt":
        for distro in list_wsl_distros():
            items.append(_probe_wsl_target(distro))
    return items


def resolve_target(target_id: str | None, include_wsl: bool = True) -> Target:
    wanted = (target_id or "windows").strip()
    for item in list_targets(include_wsl=include_wsl):
        if item.id.lower() == wanted.lower() or (item.distro and item.distro.lower() == wanted.lower()):
            return item
    raise KeyError(f"未找到运行目标：{wanted}")


def _count_posix_skills(skill_dir: Path) -> tuple[int, bool, bool]:
    """Return ``(count, exists, is_symlink)`` for a skill directory (supports WSL UNC)."""
    try:
        is_link = skill_dir.is_symlink()
        exists = skill_dir.is_dir() or is_link
        if not exists:
            return 0, False, is_link
        count = sum(1 for item in skill_dir.iterdir() if not item.name.startswith("."))
        return count, True, is_link
    except OSError:
        return 0, False, False


def detect_target_agents(target: Target) -> list[dict[str, Any]]:
    """Detect all Agents inside ``target`` without mixing Windows and WSL paths."""
    if target.kind == "windows":
        pool = profiles_mod.load_profiles()
        rows = []
        for profile in profiles_mod.detected_profiles(pool).values():
            det = profiles_mod.detect_agent(profile)
            from . import context_audit

            sk_dir = context_audit._skill_dir_for(profile)
            sk_count, sk_exists, sk_link = _count_posix_skills(sk_dir) if sk_dir else (0, False, False)
            rows.append({
                "target": target.id,
                "agent": profile.id,
                "qualifiedId": f"{target.id}/{profile.id}",
                "name": profile.name,
                "configPath": str(profile.path),
                "mcpPath": str(profile.path),
                "skillsPath": str(sk_dir) if sk_dir else "",
                "skillsExists": sk_exists,
                "skillsCount": sk_count,
                "skillsSymlink": sk_link,
                "transport": "local",
                "format": profile.format,
                "schema": profile.schema,
                "detected": det["detected"],
                "configState": det["configState"],
                "configError": det["configError"],
                "mcpCount": det["mcpCount"],
                "redirected": det["redirected"],
                "resolvedPath": det["resolvedPath"],
            })
        return rows

    if target.kind == "wsl":
        if not target.online or not target.home:
            return []
        rows = []
        for spec in WSL_AGENT_SPECS:
            posix_cfg = f"{target.home.rstrip('/')}/{spec['config']}"
            posix_skills = f"{target.home.rstrip('/')}/{spec['skills']}"
            unc_cfg = wsl_unc_path(target.distro, posix_cfg)
            unc_skills = wsl_unc_path(target.distro, posix_skills)
            try:
                cfg_exists = unc_cfg.is_file()
            except OSError:
                cfg_exists = False
            sk_count, sk_exists, sk_link = _count_posix_skills(unc_skills)
            if not cfg_exists and not sk_exists:
                continue
            config_state = "missing"
            config_error = None
            mcp_count: int | None = 0
            if cfg_exists:
                try:
                    section = formats.read_section(unc_cfg, spec["format"], spec["schema"])
                    real = {
                        k: v for k, v in section.items()
                        if isinstance(v, dict) and not adapters.is_container(v)
                    }
                    mcp_count = len(real)
                    config_state = "ok"
                except Exception as exc:
                    mcp_count = None
                    config_state = "parse_error"
                    config_error = str(exc)
            rows.append({
                "target": target.id,
                "agent": spec["id"],
                "qualifiedId": f"{target.id}/{spec['id']}",
                "name": spec["name"],
                "configPath": posix_cfg,
                "mcpPath": posix_cfg,
                "uncConfigPath": str(unc_cfg),
                "skillsPath": posix_skills,
                "uncSkillsPath": str(unc_skills),
                "skillsExists": sk_exists,
                "skillsCount": sk_count,
                "skillsSymlink": sk_link,
                "transport": "wsl",
                "format": spec["format"],
                "schema": spec["schema"],
                "detected": True,
                "configState": config_state,
                "configError": config_error,
                "mcpCount": mcp_count,
                "redirected": False,
                "resolvedPath": posix_cfg,
            })
        return rows

    return []


def collect_targets_report(target_id: str | None = None) -> dict[str, Any]:
    """Structured report for ``mcpmanager targets`` and ``mcpmanager detect``."""
    selected = [resolve_target(target_id)] if target_id else list_targets(include_wsl=True)
    items = []
    for t in selected:
        agents = detect_target_agents(t)
        items.append({
            "id": t.id,
            "name": t.name,
            "kind": t.kind,
            "online": t.online,
            "os": t.os_name,
            "user": t.user,
            "home": t.home,
            "distro": t.distro,
            "tools": t.tools,
            "magpieReachable": t.magpie_reachable,
            "agents": agents,
        })
    return {"schemaVersion": 1, "targets": items}
