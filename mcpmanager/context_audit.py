"""Context-saving, Skill-routing, duplicate-plugin and WSL read-only audit.

Answers the practical questions users hit when managing many Agents:
- Which Agents actually folded MCPs into a single Magpie hub vs loading N direct MCPs?
- Which Agents actually folded Skills into ``magpie-skills`` vs expanding 100+ copies?
- Is ``~/.agents/skills`` leaking 100+ skills into Codex/OpenCode/Pi/OmO?
- Do plugin lists or paths point to the same physical directory via a Windows junction?
- What is configured inside WSL distros (read-only, without mutating WSL)?
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path
from typing import Any

from . import adapters, formats, paths, profiles as profiles_mod

# Agents known to natively scan ~/.agents/skills even when Skill routing is on.
SHARED_AGENTS_DIR_READERS = {"codex", "opencode", "omo", "pi", "cindy", "alma"}

# Conventional Skill directories per Agent profile id.
SKILL_DIR_TEMPLATES: dict[str, str] = {
    "antigravity": "{home}/.gemini/config/skills",
    "claude-code": "{home}/.claude/skills",
    "codex": "{codex_home}/skills",
    "copilot-cli": "{home}/.copilot/skills",
    "opencode": "{home}/.config/opencode/skills",
    "trae": "{home}/.trae-cn/skills",
    "trae-solo": "{home}/.trae-aicc/skills",
    "cursor": "{home}/.cursor/skills",
    "windsurf": "{home}/.codeium/windsurf/skills",
    "cline": "{home}/.cline/skills",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def _is_junction_or_symlink(path: Path) -> tuple[bool, Path]:
    abs_path = Path(os.path.abspath(str(path)))
    real_path = Path(os.path.realpath(str(path)))
    has_link = any(
        candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction())
        for candidate in (abs_path, *abs_path.parents)
    )
    if not has_link:
        return False, real_path
    return os.path.normcase(str(abs_path)) != os.path.normcase(str(real_path)), real_path


def _count_skill_entries(folder: Path) -> tuple[int, bool, int]:
    """Return ``(total_skills, has_magpie_router, symlink_count)`` in a skills directory."""
    if not folder.is_dir():
        return 0, False, 0
    total = 0
    has_router = False
    symlinks = 0
    try:
        for child in folder.iterdir():
            if child.name.startswith("."):
                continue
            if not (child.is_dir() or child.is_symlink()):
                continue
            total += 1
            if child.name == "magpie-skills":
                has_router = True
            if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
                symlinks += 1
    except OSError:
        return 0, False, 0
    return total, has_router, symlinks


def classify_mcp_routing(section: dict[str, Any], profile: Any) -> dict[str, Any]:
    """Classify an Agent's MCP section into ``empty`` / ``routed`` / ``hybrid`` / ``direct``."""
    real = {
        name: spec
        for name, spec in section.items()
        if isinstance(spec, dict) and not adapters.is_container(spec)
    }
    if not real:
        return {"mode": "empty", "mcpCount": 0, "magpieCount": 0, "directCount": 0, "contextSaved": True}

    magpie_names = [name for name, spec in real.items() if adapters.is_magpie_managed(spec)]
    direct_count = len(real) - len(magpie_names)
    if magpie_names and direct_count == 0:
        mode = "routed"
    elif magpie_names:
        mode = "hybrid"
    else:
        mode = "direct"
    return {
        "mode": mode,
        "mcpCount": len(real),
        "magpieCount": len(magpie_names),
        "directCount": direct_count,
        "contextSaved": mode in {"empty", "routed"},
    }


def find_duplicate_plugins(path: Path, fmt: str) -> list[dict[str, Any]]:
    """Detect duplicate plugin entries (e.g. OpenCode ``plugin`` list pointing to same realpath)."""
    if fmt not in {"json", "jsonc"} or not path.is_file():
        return []
    try:
        data = formats.load_config(path, fmt)
    except Exception:
        return []
    plugins = data.get("plugin")
    if not isinstance(plugins, list) or len(plugins) < 2:
        return []
    by_real: dict[str, list[str]] = {}
    for item in plugins:
        text = str(item).strip()
        if not text:
            continue
        real = os.path.normcase(os.path.realpath(text)) if (":" in text or "/" in text or "\\" in text) else text
        by_real.setdefault(real, []).append(text)
    return [
        {"resolvedTarget": target, "entries": entries}
        for target, entries in by_real.items()
        if len(entries) > 1
    ]


def inspect_shared_skills(home: Path | None = None) -> dict[str, Any]:
    """Inspect ``~/.agents/skills`` and ``~/.config/magpie/library/skills``."""
    base_home = home or paths.HOME
    shared_dir = base_home / ".agents" / "skills"
    magpie_lib = base_home / ".config" / "magpie" / "library" / "skills"
    shared_count, shared_router, _ = _count_skill_entries(shared_dir)
    lib_count, _, _ = _count_skill_entries(magpie_lib)
    return {
        "sharedAgentsDir": str(shared_dir),
        "sharedExists": shared_dir.is_dir(),
        "sharedSkillCount": shared_count,
        "sharedHasRouter": shared_router,
        "magpieLibraryDir": str(magpie_lib),
        "magpieLibraryExists": magpie_lib.is_dir(),
        "magpieLibraryCount": lib_count,
    }


def _skill_dir_for(profile: Any) -> Path | None:
    template = SKILL_DIR_TEMPLATES.get(profile.id)
    if not template:
        return None
    expanded = paths.expand_path(template)
    # If the profile itself was redirected (e.g. ~/.copilot -> E:\.copilot),
    # check alongside the resolved config parent as well if the default does not exist.
    if not expanded.exists():
        resolved_cfg = Path(os.path.realpath(str(profile.path)))
        sibling = resolved_cfg.parent / "skills"
        if sibling.is_dir():
            return sibling
    return expanded


def _classify_skills(
    profile_id: str,
    skill_dir: Path | None,
    shared_info: dict[str, Any],
) -> dict[str, Any]:
    if skill_dir is None:
        return {
            "path": None,
            "exists": False,
            "count": 0,
            "hasRouter": False,
            "redirected": False,
            "resolvedPath": None,
            "mode": "unknown",
            "contextSaved": False,
            "note": "未定义 Skill 目录",
        }
    redirected, resolved = _is_junction_or_symlink(skill_dir)
    exists = skill_dir.is_dir()
    count, has_router, symlinks = _count_skill_entries(skill_dir)
    shared_count = int(shared_info.get("sharedSkillCount", 0))
    reads_shared = profile_id in SHARED_AGENTS_DIR_READERS

    if reads_shared and shared_count > 10:
        mode = "leaked_shared"
        saved = False
        note = f"原生读取 ~/.agents/skills（内有 {shared_count} 个未折叠 Skill），Skill 路由暂无法省上下文"
    elif redirected:
        mode = "symlink_shared"
        saved = False
        note = f"目录软链接/Junction 至 {resolved}（共 {count} 个），切勿直接覆盖写入"
    elif has_router and count <= 15:
        mode = "routed"
        saved = True
        note = f"已折叠为 magpie-skills 路由（目录仅 {count} 项），有效节省上下文"
    elif exists and count > 0:
        mode = "expanded"
        saved = False
        note = f"全量展开 {count} 个实体 Skill" + ("（含路由入口）" if has_router else "")
    else:
        mode = "empty"
        saved = True
        note = "目录为空或未发现额外 Skill"

    return {
        "path": str(skill_dir),
        "exists": exists,
        "count": count,
        "hasRouter": has_router,
        "symlinkEntries": symlinks,
        "redirected": redirected,
        "resolvedPath": str(resolved),
        "mode": mode,
        "contextSaved": saved,
        "note": note,
    }


def list_wsl_distros() -> list[str]:
    """Return installed WSL distro names safely, or ``[]`` on non-Windows / error."""
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
    # wsl.exe -l -q outputs UTF-16LE on Windows.
    raw = result.stdout
    text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode("utf-8", errors="ignore")
    return [line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()]


def inspect_wsl_agents(distros: list[str] | None = None) -> list[dict[str, Any]]:
    """Read-only inspection of Pi, Codex, and Antigravity inside WSL distros."""
    if os.name != "nt":
        return []
    targets = distros if distros is not None else list_wsl_distros()
    results: list[dict[str, Any]] = []
    for distro in targets:
        home_root = Path(f"\\\\wsl.localhost\\{distro}\\home")
        try:
            if not home_root.is_dir():
                continue
            users = [p for p in home_root.iterdir() if p.is_dir() and p.name not in {".", ".."}]
        except OSError:
            continue

        for user_home in users:
            specs = [
                ("pi", "Pi (WSL)", user_home / ".pi" / "agent" / "mcp.json", "json", "mcpServers", user_home / ".pi" / "agent" / "skills"),
                ("codex", "Codex (WSL)", user_home / ".codex" / "config.toml", "toml", "mcp_servers", user_home / ".codex" / "skills"),
                ("antigravity", "Antigravity (WSL)", user_home / ".gemini" / "config" / "mcp_config.json", "json", "mcpServers", user_home / ".gemini" / "config" / "skills"),
            ]
            for agent_id, label, cfg_path, fmt, schema, skill_dir in specs:
                try:
                    exists = cfg_path.is_file()
                except OSError:
                    exists = False
                if not exists:
                    continue
                mcp_info: dict[str, Any] = {"mode": "unknown", "mcpCount": 0, "magpieCount": 0, "directCount": 0}
                try:
                    section = formats.read_section(cfg_path, fmt, schema)
                    mcp_info = classify_mcp_routing(section, profiles_mod.Profile(id=agent_id, name=label, config=str(cfg_path), format=fmt, schema=schema))
                except Exception:
                    mcp_info["mode"] = "parse_error"

                skill_count, has_router, symlink_entries = _count_skill_entries(skill_dir)
                is_link = skill_dir.is_symlink()
                results.append({
                    "distro": distro,
                    "user": user_home.name,
                    "agent": f"{agent_id}@wsl:{distro}",
                    "name": f"{label} · {distro}",
                    "configPath": str(cfg_path),
                    "mcp": mcp_info,
                    "skills": {
                        "path": str(skill_dir),
                        "exists": skill_dir.is_dir(),
                        "count": skill_count,
                        "hasRouter": has_router,
                        "symlinkEntries": symlink_entries,
                        "isSymlink": is_link,
                    },
                    "readonly": True,
                })
    return results


def collect_context_audit(include_wsl: bool = True) -> dict[str, Any]:
    """Build a complete MCP-routing, Skill-folding, duplicate-plugin and WSL audit."""
    pool = profiles_mod.load_profiles()
    detected = profiles_mod.detected_profiles(pool)
    shared = inspect_shared_skills()
    agents_out: list[dict[str, Any]] = []
    recommendations: list[str] = []

    for profile in detected.values():
        mcp_info: dict[str, Any] = {"mode": "error", "mcpCount": 0, "magpieCount": 0, "directCount": 0, "contextSaved": False}
        try:
            section, _ = formats.read_merged_section(profile.paths, profile.format, profile.schema)
            mcp_info = classify_mcp_routing(section, profile)
        except Exception:
            pass
        skill_dir = _skill_dir_for(profile)
        skill_info = _classify_skills(profile.id, skill_dir, shared)
        dup_plugins = find_duplicate_plugins(profile.path, profile.format)

        agents_out.append({
            "agent": profile.id,
            "name": profile.name,
            "configPath": str(profile.path),
            "mcp": mcp_info,
            "skills": skill_info,
            "duplicatePlugins": dup_plugins,
        })
        if dup_plugins:
            recommendations.append(f"{profile.name} 存在重复插件条目（指向同一物理目录），建议清理重复项。")

    if shared["sharedSkillCount"] > 10:
        recommendations.append(
            f"公共目录 {shared['sharedAgentsDir']} 仍有 {shared['sharedSkillCount']} 个 Skill，"
            "Codex / OpenCode / Pi / OmO 会原生全量扫描；如需通过 Skill 路由省上下文，需在 Magpie 关闭只读该目录的端并移出公共目录。"
        )
    unfolded_mcp = [a["name"] for a in agents_out if a["mcp"]["mode"] in {"direct", "hybrid"} and a["mcp"]["mcpCount"] > 3]
    if unfolded_mcp:
        recommendations.append("以下 Agent 仍有多个直连 MCP 未完全折叠为单网关：" + "、".join(unfolded_mcp))

    wsl_agents = inspect_wsl_agents() if include_wsl else []
    return {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "sharedSkills": shared,
        "agents": agents_out,
        "wslAgents": wsl_agents,
        "recommendations": recommendations,
    }
