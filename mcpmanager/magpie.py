"""Strictly read-only Magpie adapter.

Never modifies Magpie's database, auth files, or library state. Only invokes
read-only ``magpie ls`` / ``magpie library`` when available and inspects local
Agent configs for ``magpie_managed`` entries and Windows/WSL path differences.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from . import adapters, formats, paths, targets

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def _run_magpie_readonly(*args: str, timeout: int = 8) -> tuple[bool, str]:
    exe = shutil.which("magpie.com") or shutil.which("magpie.exe") or shutil.which("magpie")
    if not exe:
        return False, "未找到 magpie CLI"
    try:
        result = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, adapters.redact_text(out)
    except Exception as exc:
        return False, str(exc)


def magpie_status() -> dict[str, Any]:
    """Read-only summary of Magpie installation and ``magpie_managed`` MCPs across targets."""
    cfg_dir = paths.HOME / ".config" / "magpie"
    lib_skills = cfg_dir / "library" / "skills"
    cli_ok, ls_output = _run_magpie_readonly("ls")
    managed_entries: list[dict[str, Any]] = []
    for target in targets.list_targets(include_wsl=True):
        if not target.online:
            continue
        for agent in targets.detect_target_agents(target):
            cfg_path = agent.get("uncConfigPath") or agent.get("configPath")
            if not cfg_path:
                continue
            try:
                from pathlib import Path

                section = formats.read_section(Path(cfg_path), agent["format"], agent["schema"])
            except Exception:
                continue
            for srv_name, spec in section.items():
                if not isinstance(spec, dict):
                    continue
                if adapters.is_magpie_managed(spec):
                    managed_entries.append({
                        "target": target.id,
                        "agent": agent["agent"],
                        "name": srv_name,
                        "source": "magpie_managed",
                        "gatewayUrl": spec.get("url") or spec.get("serverUrl") or "",
                    })
    return {
        "installed": cfg_dir.is_dir(),
        "configDir": str(cfg_dir),
        "librarySkillsDir": str(lib_skills),
        "librarySkillsExists": lib_skills.is_dir(),
        "cliAvailable": cli_ok,
        "lsPreview": ls_output.splitlines()[:25] if cli_ok and ls_output else [],
        "managedMcps": managed_entries,
    }


def magpie_agents() -> dict[str, Any]:
    ok, output = _run_magpie_readonly("ls")
    return {"cliAvailable": ok, "raw": output, "lines": output.splitlines() if output else []}


def magpie_library() -> dict[str, Any]:
    ok, output = _run_magpie_readonly("library")
    return {"cliAvailable": ok, "raw": output, "lines": output.splitlines() if output else []}


def magpie_diff() -> dict[str, Any]:
    """Compare Magpie-managed MCPs and Windows/WSL path differences (read-only)."""
    win_mcps: dict[str, set[str]] = {}
    wsl_mcps: dict[str, set[str]] = {}
    path_diffs: list[dict[str, str]] = []
    for target in targets.list_targets(include_wsl=True):
        if not target.online:
            continue
        for agent in targets.detect_target_agents(target):
            cfg_path = agent.get("uncConfigPath") or agent.get("configPath")
            if not cfg_path:
                continue
            try:
                from pathlib import Path

                section = formats.read_section(Path(cfg_path), agent["format"], agent["schema"])
            except Exception:
                continue
            for srv_name, spec in section.items():
                if not isinstance(spec, dict):
                    continue
                bucket = win_mcps if target.kind == "windows" else wsl_mcps
                bucket.setdefault(srv_name, set()).add(agent["qualifiedId"])
                if target.kind == "wsl" and adapters.has_windows_path(spec):
                    path_diffs.append({
                        "qualifiedId": agent["qualifiedId"],
                        "name": srv_name,
                        "issue": "WSL 配置中包含 Windows 路径",
                    })
    only_win = sorted(name for name in win_mcps if name not in wsl_mcps)
    only_wsl = sorted(name for name in wsl_mcps if name not in win_mcps)
    return {
        "onlyInWindows": only_win,
        "onlyInWsl": only_wsl,
        "pathViolations": path_diffs,
    }
