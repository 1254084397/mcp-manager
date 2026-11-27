"""Multi-target runtime model: Windows local host and WSL Linux distros."""

from __future__ import annotations

import hashlib
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
IDENTITY_STATE_PATH = paths.DATA_DIR / "agent-identity.json"

# Known WSL agent layouts (resolved relative to the detected WSL user's $HOME).
WSL_AGENT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "pi",
        "name": "Pi",
        "config": ".pi/agent/mcp.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".pi/agent/skills",
        "shared_skills_reader": True,
        "executables": ("pi",),
        "runtime_markers": (".pi/agent/settings.json", ".pi/agent/models.json"),
    },
    {
        "id": "codex",
        "name": "Codex CLI",
        "config": ".codex/config.toml",
        "format": "toml",
        "schema": "mcp_servers",
        "skills": ".codex/skills",
        "shared_skills_reader": True,
        "executables": ("codex",),
        "runtime_markers": (".codex/auth.json", ".codex/sessions"),
    },
    {
        "id": "antigravity",
        "name": "Antigravity",
        "config": ".gemini/config/mcp_config.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".gemini/skills",
        "shared_skills_reader": True,
        "executables": ("antigravity",),
        "runtime_markers": (".gemini/antigravity", ".gemini/settings.json", ".antigravity-server"),
    },
    {
        "id": "claude-code",
        "name": "Claude Code",
        "config": ".claude.json",
        "format": "json",
        "schema": "mcpServers",
        "skills": ".claude/skills",
        "shared_skills_reader": False,
        "executables": ("claude",),
        "runtime_markers": (".claude/settings.json", ".claude/projects"),
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "config": ".config/opencode/opencode.json",
        "format": "jsonc",
        "schema": "mcp",
        "skills": ".config/opencode/skills",
        "shared_skills_reader": True,
        "executables": ("opencode",),
        "runtime_markers": (".config/opencode/package.json", ".config/opencode/opencode-storage"),
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
    agent_tools: dict[str, bool] = field(default_factory=dict)
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
        'done; '
        'for a in pi codex opencode claude copilot antigravity; do '
        '  command -v "$a" >/dev/null 2>&1 && echo "AG_$a=1" || echo "AG_$a=0"; '
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
    agent_tools = {
        a: kv.get(f"AG_{a}") == "1"
        for a in ("pi", "codex", "opencode", "claude", "copilot", "antigravity")
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
        agent_tools=agent_tools,
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


def load_identity_overrides(path: Path | None = None) -> dict[str, Any]:
    target = path or IDENTITY_STATE_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_identity_overrides(data: dict[str, Any], path: Path | None = None) -> None:
    if paths.read_only():
        return
    target = path or IDENTITY_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_agent_identity_override(qualified_id: str, state: str, path: Path | None = None) -> None:
    """Persist user override for an agent: ``confirmed``, ``residual``, ``ignored``, or ``auto``."""
    data = load_identity_overrides(path)
    for bucket_key in ("confirmed", "residual", "ignored"):
        items = [x for x in data.get(bucket_key, []) if x != qualified_id]
        if state == bucket_key:
            items.append(qualified_id)
        data[bucket_key] = items
    save_identity_overrides(data, path)


def _list_skill_names(skill_dir: Path) -> tuple[set[str], bool, bool]:
    try:
        is_link = skill_dir.is_symlink() or (hasattr(skill_dir, "is_junction") and skill_dir.is_junction())
        exists = skill_dir.is_dir() or is_link
        if not exists:
            return set(), False, is_link
        names = {item.name for item in skill_dir.iterdir() if not item.name.startswith(".")}
        return names, True, is_link
    except OSError:
        return set(), False, False


def _load_magpie_applied() -> dict[str, Any]:
    try:
        data = json.loads((paths.HOME / ".config" / "magpie" / "applied.json").read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _magpie_mcp_applied(record: Any) -> bool:
    """Return whether an applied.json record proves MCP/Hub application.

    Magpie also records model/provider application in the same file.  A model
    record alone must not be presented as proof that the agent's MCP config was
    written.
    """
    if not isinstance(record, dict):
        return False
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    if not isinstance(fields, dict):
        return False
    for key in fields:
        token = str(key).lower()
        if any(word in token for word in ("mcp", "hub", "server")):
            return True
    return False


def _routing_status(section: dict[str, Any]) -> tuple[str, bool]:
    if not section:
        return "empty", False
    real = {k: v for k, v in section.items() if isinstance(v, dict) and not adapters.is_container(v)}
    if not real:
        return "empty", False
    has_magpie = any(k.lower() == "magpie" or adapters.is_magpie_managed(v) for k, v in real.items())
    others = sum(1 for k, v in real.items() if k.lower() != "magpie" and not adapters.is_magpie_managed(v))
    if has_magpie and others == 0:
        return "routed", True
    if has_magpie:
        return "hybrid", True
    return "direct", False


def _windows_executable_found(agent_id: str, executables: list[str]) -> bool:
    for exe in executables:
        if shutil.which(exe) or shutil.which(f"{exe}.cmd") or shutil.which(f"{exe}.exe"):
            return True
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata = Path(os.environ.get("APPDATA", ""))
    app_candidates = {
        "antigravity": [local / "Programs" / "Antigravity" / "Antigravity.exe"],
        "vscode": [local / "Programs" / "Microsoft VS Code" / "Code.exe"],
        "trae": [local / "Programs" / "Trae CN" / "Trae CN.exe", appdata / "Trae CN" / "Trae CN.exe"],
        "trae-solo": [local / "Programs" / "TRAE SOLO CN" / "TRAE SOLO CN.exe"],
    }.get(agent_id, [])
    return any(p.is_file() for p in app_candidates if str(p))


def _windows_runtime_dir_found(agent_id: str, profile: Any) -> bool:
    home = paths.HOME
    appdata = Path(os.environ.get("APPDATA", ""))
    markers = {
        "pi": [home / ".pi" / "agent" / "settings.json", home / ".pi" / "agent" / "models.json"],
        "codex": [profile.path.parent / "auth.json", profile.path.parent / "sessions"],
        "opencode": [home / ".config" / "opencode" / "package.json", home / ".config" / "opencode" / "opencode-storage"],
        "antigravity": [home / ".gemini" / "antigravity", home / ".gemini" / "settings.json"],
        "trae": [appdata / "Trae CN" / "User" / "globalStorage", home / ".trae-cn" / "installed-plugins.json"],
        "trae-solo": [appdata / "TRAE SOLO CN" / "User" / "globalStorage"],
        "vscode": [appdata / "Code" / "User" / "globalStorage"],
        "claude-code": [home / ".claude" / "settings.json", home / ".claude" / "projects"],
        "copilot-cli": [home / ".copilot" / "config.json"],
    }.get(agent_id, [])
    return any(p.exists() for p in markers if str(p))


def _classify_agent_identity(
    *,
    qualified_id: str,
    has_exe: bool,
    has_runtime: bool,
    has_config: bool,
    has_skills: bool,
    has_magpie_applied: bool,
    mcp_count: int | None,
    discovered: bool = False,
    overrides: dict[str, Any] | None = None,
) -> tuple[str, float, list[str], bool]:
    ev: list[str] = []
    if has_exe:
        ev.append("executable_or_runtime")
    if has_runtime:
        ev.append("runtime_dir")
    if has_config:
        ev.append("config_file")
    if has_skills:
        ev.append("skills_directory")
    if has_magpie_applied:
        ev.append("magpie_applied")

    ov = overrides if overrides is not None else load_identity_overrides()
    if qualified_id in ov.get("ignored", []):
        return "ignored", 0.0, ev, True
    if qualified_id in ov.get("confirmed", []):
        return "confirmed", 1.0, [*ev, "user_confirmed"], False
    if qualified_id in ov.get("residual", []):
        return "residual", 0.2, [*ev, "user_residual"], True
    if discovered:
        return "unknown_readonly", 0.45, ev, True

    score = min(
        0.99,
        (0.45 if has_exe else 0.0)
        + (0.25 if has_runtime else 0.0)
        + (0.20 if has_config else 0.0)
        + (0.15 if has_magpie_applied else 0.0)
        + (0.10 if has_skills else 0.0),
    )
    if has_exe or (has_runtime and (has_config or has_magpie_applied)):
        return "confirmed", round(score, 2), ev, False
    if has_config and (has_skills or has_magpie_applied or (mcp_count or 0) > 0):
        return "configured", round(score, 2), ev, False
    return "residual", round(score, 2), ev, True


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


_IGNORED_DISCOVERY_DIRS = {
    "node_modules", ".git", ".cache", ".npm", ".pnpm-store", ".cargo", ".rustup",
    ".local", ".venv", "venv", "__pycache__", "backups", "backup", "cache",
    "caches", "marketplaces", "marketplace", "plugins", "agentplugins", "tmp",
    "temp", ".trash", ".antigravity-server", ".vscode-server", ".agents",
    ".ssh", ".gnupg", "microsoft", "packages", "programs", "google", "mozilla",
    "nvidia", "intel", "amd", "adobe", "jetbrains", "pip", "npm", "npm-cache",
    ".trae-cn", ".trae-aicc", "trae cn", "trae solo cn",
}
_DISCOVERY_CONFIG_NAMES = ("mcp.json", "mcp_config.json", "mcp.jsonc", "config.toml")
_DISCOVERY_SCHEMAS = ("mcpServers", "servers", "mcp", "mcp_servers")


def _infer_mcp_config(cfg_path: Path) -> tuple[str, str, int] | None:
    """Inspect a candidate config file and infer ``(format, schema, mcp_count)``."""
    name_low = cfg_path.name.lower()
    if ".bak" in name_low or "backup" in name_low or "cache" in name_low:
        return None
    suffix = cfg_path.suffix.lower()
    fmt = {".toml": "toml", ".jsonc": "jsonc", ".json": "json"}.get(suffix)
    if not fmt:
        return None
    try:
        raw = cfg_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for schema in _DISCOVERY_SCHEMAS:
        if schema not in raw:
            continue
        try:
            section = formats.read_section(cfg_path, fmt, schema)
        except Exception:
            continue
        if isinstance(section, dict) and (section or fmt != "toml"):
            real = {
                k: v for k, v in section.items()
                if isinstance(v, dict) and not adapters.is_container(v)
            }
            return fmt, schema, len(real)
    return None


def _discover_unknown_wsl_agents(
    target: Target,
    known_cfg_posix: set[str],
    known_skills_posix: set[str],
) -> list[dict[str, Any]]:
    """Layer-2 WSL discovery: find unknown Agent configs or skill dirs under ``$HOME``."""
    home_posix = target.home.rstrip("/")
    home_unc = wsl_unc_path(target.distro, home_posix)
    try:
        if not home_unc.is_dir():
            return []
        top_entries = sorted(home_unc.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []

    candidate_dirs: list[tuple[str, Path]] = []
    for entry in top_entries:
        if not entry.name.startswith(".") or entry.name.lower() in _IGNORED_DISCOVERY_DIRS:
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if entry.name == ".config":
            try:
                for sub in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if sub.name.lower() in _IGNORED_DISCOVERY_DIRS:
                        continue
                    if sub.is_dir():
                        candidate_dirs.append((f".config/{sub.name}", sub))
            except OSError:
                pass
        else:
            candidate_dirs.append((entry.name, entry))

    discovered: list[dict[str, Any]] = []
    for rel_dir, dir_unc in candidate_dirs:
        cfg_file: Path | None = None
        cfg_posix = ""
        inferred: tuple[str, str, int] | None = None
        for sub_dir, sub_rel in ((dir_unc, rel_dir), (dir_unc / "agent", f"{rel_dir}/agent")):
            for fname in _DISCOVERY_CONFIG_NAMES:
                cand_file = sub_dir / fname
                cand_posix = f"{home_posix}/{sub_rel}/{fname}"
                if cand_posix in known_cfg_posix:
                    continue
                try:
                    if not cand_file.is_file():
                        continue
                except OSError:
                    continue
                inferred = _infer_mcp_config(cand_file)
                if inferred is not None:
                    cfg_file = cand_file
                    cfg_posix = cand_posix
                    break
            if inferred is not None:
                break

        skills_posix = f"{home_posix}/{rel_dir}/skills"
        skills_unc = wsl_unc_path(target.distro, skills_posix)
        if skills_posix in known_skills_posix:
            sk_count, sk_exists, sk_link = 0, False, False
        else:
            sk_count, sk_exists, sk_link = _count_posix_skills(skills_unc)

        if inferred is None and not sk_exists:
            continue

        seed = cfg_posix or skills_posix
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]
        agent_id = f"discovered-{digest}"
        dir_label = rel_dir.split("/")[-1].lstrip(".") or rel_dir
        if inferred is not None:
            fmt, schema, mcp_count = inferred
            state = "unknown_readonly"
            label = f"未知 Agent ({dir_label})"
        else:
            fmt, schema, mcp_count = "json", "mcpServers", 0
            state = "skills_only"
            label = f"未知 Agent ({dir_label}·仅Skills)"

        discovered.append({
            "target": target.id,
            "agent": agent_id,
            "qualifiedId": f"{target.id}/{agent_id}",
            "name": label,
            "configPath": cfg_posix,
            "mcpPath": cfg_posix,
            "uncConfigPath": str(cfg_file) if cfg_file else "",
            "skillsPath": skills_posix if sk_exists else "",
            "uncSkillsPath": str(skills_unc) if sk_exists else "",
            "skillsExists": sk_exists,
            "skillsCount": sk_count,
            "skillsSymlink": sk_link,
            "transport": "wsl",
            "format": fmt,
            "schema": schema,
            "detected": True,
            "readonly": True,
            "discovered": True,
            "configState": state,
            "configError": None,
            "mcpCount": mcp_count,
            "redirected": False,
            "resolvedPath": cfg_posix or skills_posix,
        })
    return discovered


def _norm_local_path(p: Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _discover_unknown_windows_agents(
    target: Target,
    known_cfg_norm: set[str],
    known_skills_norm: set[str],
) -> list[dict[str, Any]]:
    """Layer-2 Windows discovery: find unknown Agent configs or skill dirs."""
    search_roots: list[tuple[Path, bool]] = [(Path(target.home), True)]
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        val = os.environ.get(env_key)
        if val:
            search_roots.append((Path(val), False))

    candidate_dirs: list[tuple[str, Path]] = []
    seen_dirs: set[str] = set()
    for root, dot_only in search_roots:
        try:
            if not root.is_dir():
                continue
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if dot_only and not entry.name.startswith("."):
                continue
            if entry.name.lower() in _IGNORED_DISCOVERY_DIRS:
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            norm_d = _norm_local_path(entry)
            if norm_d not in seen_dirs:
                seen_dirs.add(norm_d)
                candidate_dirs.append((entry.name, entry))

    discovered: list[dict[str, Any]] = []
    for dir_name, dir_path in candidate_dirs:
        cfg_file: Path | None = None
        inferred: tuple[str, str, int] | None = None
        for sub_dir in (dir_path, dir_path / "agent"):
            for fname in _DISCOVERY_CONFIG_NAMES:
                cand_file = sub_dir / fname
                try:
                    if not cand_file.is_file():
                        continue
                except OSError:
                    continue
                if _norm_local_path(cand_file) in known_cfg_norm:
                    continue
                inferred = _infer_mcp_config(cand_file)
                if inferred is not None:
                    cfg_file = cand_file
                    break
            if inferred is not None:
                break

        skills_dir = dir_path / "skills"
        if _norm_local_path(skills_dir) in known_skills_norm:
            sk_count, sk_exists, sk_link = 0, False, False
        else:
            sk_count, sk_exists, sk_link = _count_posix_skills(skills_dir)

        if inferred is None and not sk_exists:
            continue

        seed = str(cfg_file or skills_dir)
        digest = hashlib.sha1(_norm_local_path(Path(seed)).encode("utf-8")).hexdigest()[:6]
        agent_id = f"discovered-{digest}"
        dir_label = dir_name.lstrip(".") or dir_name
        if inferred is not None:
            fmt, schema, mcp_count = inferred
            state = "unknown_readonly"
            label = f"未知 Agent ({dir_label})"
        else:
            fmt, schema, mcp_count = "json", "mcpServers", 0
            state = "skills_only"
            label = f"未知 Agent ({dir_label}·仅Skills)"

        discovered.append({
            "target": target.id,
            "agent": agent_id,
            "qualifiedId": f"{target.id}/{agent_id}",
            "name": label,
            "configPath": str(cfg_file) if cfg_file else "",
            "mcpPath": str(cfg_file) if cfg_file else "",
            "skillsPath": str(skills_dir) if sk_exists else "",
            "skillsExists": sk_exists,
            "skillsCount": sk_count,
            "skillsSymlink": sk_link,
            "transport": "local",
            "format": fmt,
            "schema": schema,
            "detected": True,
            "readonly": True,
            "discovered": True,
            "configState": state,
            "configError": None,
            "mcpCount": mcp_count,
            "redirected": False,
            "resolvedPath": str(cfg_file or skills_dir),
        })
    return discovered


def detect_target_agents(target: Target) -> list[dict[str, Any]]:
    """Detect all Agents inside ``target`` without mixing Windows and WSL paths."""
    if target.kind == "windows":
        pool = profiles_mod.load_profiles()
        rows = []
        known_cfg_norm: set[str] = set()
        known_skills_norm: set[str] = set()
        overrides = load_identity_overrides()
        applied_map = _load_magpie_applied()
        shared_win_names, _, _ = _list_skill_names(paths.HOME / ".agents" / "skills")
        magpie_lib_names, _, _ = _list_skill_names(paths.HOME / ".config" / "magpie" / "library")
        from . import context_audit

        for profile in pool.values():
            for p in profile.paths:
                known_cfg_norm.add(_norm_local_path(p))
            sk_all = Path(paths.expand_path(profile.skills)) if profile.skills else context_audit._skill_dir_for(profile)
            if sk_all:
                known_skills_norm.add(_norm_local_path(sk_all))

        for profile in profiles_mod.detected_profiles(pool).values():
            det = profiles_mod.detect_agent(profile)
            sk_dir = Path(paths.expand_path(profile.skills)) if profile.skills else context_audit._skill_dir_for(profile)
            local_names, sk_exists, sk_link = _list_skill_names(sk_dir) if sk_dir else (set(), False, False)
            shared_names = shared_win_names if profile.shared_skills_reader else set()
            effective_names = local_names | shared_names

            section: dict[str, Any] = {}
            if det["configState"] in {"ok", "plugin_only"}:
                try:
                    section, _ = formats.read_merged_section(profile.paths, profile.format, profile.schema)
                except Exception:
                    section = {}
            routing_mode, magpie_hub = _routing_status(section)
            qid = f"{target.id}/{profile.id}"
            has_exe = _windows_executable_found(profile.id, profile.executables or [profile.id])
            has_rt = _windows_runtime_dir_found(profile.id, profile)
            applied_record = applied_map.get(profile.id)
            if applied_record is None:
                applied_record = next((applied_map.get(a) for a in profile.aliases if a in applied_map), None)
            has_applied = _magpie_mcp_applied(applied_record)
            model_applied = applied_record is not None
            id_state, conf, ev, is_res = _classify_agent_identity(
                qualified_id=qid,
                has_exe=has_exe,
                has_runtime=has_rt,
                has_config=det["configState"] in {"ok", "plugin_only"},
                has_skills=bool(local_names),
                has_magpie_applied=has_applied,
                mcp_count=det["mcpCount"],
                overrides=overrides,
            )
            if id_state == "ignored":
                continue

            rows.append({
                "target": target.id,
                "agent": profile.id,
                "qualifiedId": qid,
                "name": profile.name,
                "configPath": str(profile.path),
                "mcpPath": str(profile.path),
                "skillsPath": str(sk_dir) if sk_dir else "",
                "skillsExists": sk_exists,
                "skillsCount": len(effective_names),
                "localSkillsCount": len(local_names),
                "sharedSkillsCount": len(shared_names),
                "effectiveSkillsCount": len(effective_names),
                "magpieLibraryCount": len(magpie_lib_names),
                "skillsSymlink": sk_link,
                "transport": "local",
                "format": profile.format,
                "schema": profile.schema,
                "detected": det["detected"],
                "configState": det["configState"],
                "configError": det["configError"],
                "mcpCount": det["mcpCount"],
                "mcpRoutingMode": routing_mode,
                "magpieHubConfigured": magpie_hub,
                "magpieModelApplied": model_applied,
                "identityState": id_state,
                "confidence": conf,
                "evidence": ev,
                "configResidual": is_res,
                "readonly": profile.readonly,
                "redirected": det["redirected"],
                "resolvedPath": det["resolvedPath"],
            })
        for unk in _discover_unknown_windows_agents(target, known_cfg_norm, known_skills_norm):
            if unk["qualifiedId"] in overrides.get("ignored", []):
                continue
            unk["identityState"] = "unknown_readonly"
            unk["confidence"] = 0.45
            unk["evidence"] = ["config_file"] if unk["configPath"] else ["skills_directory"]
            unk["configResidual"] = True
            unk["localSkillsCount"] = unk["skillsCount"]
            unk["sharedSkillsCount"] = 0
            unk["effectiveSkillsCount"] = unk["skillsCount"]
            unk["mcpRoutingMode"] = "direct" if (unk["mcpCount"] or 0) > 0 else "empty"
            unk["magpieHubConfigured"] = False
            rows.append(unk)
        return rows

    if target.kind == "wsl":
        if not target.online or not target.home:
            return []
        rows = []
        known_cfg_posix: set[str] = set()
        known_skills_posix: set[str] = set()
        overrides = load_identity_overrides()
        applied_map = _load_magpie_applied()
        shared_wsl_names, _, _ = _list_skill_names(wsl_unc_path(target.distro, f"{target.home.rstrip('/')}/.agents/skills"))
        for spec in WSL_AGENT_SPECS:
            posix_cfg = f"{target.home.rstrip('/')}/{spec['config']}"
            posix_skills = f"{target.home.rstrip('/')}/{spec['skills']}"
            known_cfg_posix.add(posix_cfg)
            known_skills_posix.add(posix_skills)
            unc_cfg = wsl_unc_path(target.distro, posix_cfg)
            unc_skills = wsl_unc_path(target.distro, posix_skills)
            try:
                cfg_exists = unc_cfg.is_file()
            except OSError:
                cfg_exists = False
            local_names, sk_exists, sk_link = _list_skill_names(unc_skills)
            if not cfg_exists and not sk_exists:
                continue
            shared_names = shared_wsl_names if spec.get("shared_skills_reader") else set()
            effective_names = local_names | shared_names
            config_state = "missing"
            config_error = None
            mcp_count: int | None = 0
            section = {}
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

            routing_mode, magpie_hub = _routing_status(section)
            qid = f"{target.id}/{spec['id']}"
            has_exe = any(target.agent_tools.get(e, False) for e in spec.get("executables", ()))
            has_rt = any(
                wsl_unc_path(target.distro, f"{target.home.rstrip('/')}/{m}").exists()
                for m in spec.get("runtime_markers", ())
            )
            applied_record = applied_map.get(f"{spec['id']}@{target.id}")
            has_applied = _magpie_mcp_applied(applied_record)
            model_applied = applied_record is not None
            id_state, conf, ev, is_res = _classify_agent_identity(
                qualified_id=qid,
                has_exe=has_exe,
                has_runtime=has_rt,
                has_config=config_state == "ok",
                has_skills=bool(local_names),
                has_magpie_applied=has_applied,
                mcp_count=mcp_count,
                overrides=overrides,
            )
            if id_state == "ignored":
                continue

            rows.append({
                "target": target.id,
                "agent": spec["id"],
                "qualifiedId": qid,
                "name": spec["name"],
                "configPath": posix_cfg,
                "mcpPath": posix_cfg,
                "uncConfigPath": str(unc_cfg),
                "skillsPath": posix_skills,
                "uncSkillsPath": str(unc_skills),
                "skillsExists": sk_exists,
                "skillsCount": len(effective_names),
                "localSkillsCount": len(local_names),
                "sharedSkillsCount": len(shared_names),
                "effectiveSkillsCount": len(effective_names),
                "skillsSymlink": sk_link,
                "transport": "wsl",
                "format": spec["format"],
                "schema": spec["schema"],
                "detected": True,
                "configState": config_state,
                "configError": config_error,
                "mcpCount": mcp_count,
                "mcpRoutingMode": routing_mode,
                "magpieHubConfigured": magpie_hub,
                "magpieModelApplied": model_applied,
                "identityState": id_state,
                "confidence": conf,
                "evidence": ev,
                "configResidual": is_res,
                "redirected": False,
                "resolvedPath": posix_cfg,
            })
        for unk in _discover_unknown_wsl_agents(target, known_cfg_posix, known_skills_posix):
            if unk["qualifiedId"] in overrides.get("ignored", []):
                continue
            unk["identityState"] = "unknown_readonly"
            unk["confidence"] = 0.45
            unk["evidence"] = ["config_file"] if unk["configPath"] else ["skills_directory"]
            unk["configResidual"] = True
            unk["localSkillsCount"] = unk["skillsCount"]
            unk["sharedSkillsCount"] = 0
            unk["effectiveSkillsCount"] = unk["skillsCount"]
            unk["mcpRoutingMode"] = "direct" if (unk["mcpCount"] or 0) > 0 else "empty"
            unk["magpieHubConfigured"] = False
            rows.append(unk)
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
