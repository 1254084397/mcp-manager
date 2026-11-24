"""Load Agent profiles from declarative TOML files."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from . import adapters, formats, paths
from .model import Profile

_CACHE: dict[str, Profile] | None = None

_BOOL_FIELDS = {"enabled", "tools", "readonly", "auxiliary_optional"}
_STRING_FIELDS = {"id", "name", "config", "format", "schema", "command", "env",
                  "transport", "stdio_type", "http_type", "url_key", "notes",
                  "config_priority"}


def _check_strings(value, field: str, source: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{source}: {field} 必须是字符串或字符串数组")
    return value


def _profile_from_file(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: profile 顶层必须是表")
    known = {field.name for field in Profile.__dataclass_fields__.values()}
    unknown = sorted(set(data) - known - {"source"})
    if unknown:
        raise ValueError(f"{path.name}: 未知字段 {', '.join(unknown)}")
    if not isinstance(data.get("id"), str) or not data["id"].strip():
        raise ValueError(f"{path.name}: id 必须是非空字符串")
    if not isinstance(data.get("config"), str) or not data["config"].strip():
        raise ValueError(f"{path.name}: config 必须是非空字符串")
    for key in _STRING_FIELDS:
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"{path.name}: {key} 必须是字符串")
    for key in _BOOL_FIELDS:
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{path.name}: {key} 必须是 true/false")
    for key in ("configs", "aliases"):
        if key in data:
            data[key] = _check_strings(data[key], key, path.name)
    for template in [data["config"], *data.get("configs", [])]:
        for placeholder in re.findall(r"\{(\w+)\}", template):
            if placeholder not in paths.PLACEHOLDERS:
                raise ValueError(f"{path.name}: 未知路径占位符 {{{placeholder}}}")
    values = {key: value for key, value in data.items() if key in known}
    values.setdefault("name", values["id"])
    profile = Profile(**values)
    profile.source = str(path)
    return profile


def load_profiles(reload: bool = False) -> dict[str, Profile]:
    """Bundled profiles first, then user overrides from ``local/agents``."""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE
    profiles: dict[str, Profile] = {}
    for folder in (paths.PROFILE_DIR, paths.USER_PROFILE_DIR):
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.toml")):
            try:
                profile = _profile_from_file(file)
            except Exception as exc:  # a broken profile must not hide the rest
                print(f"[mcpmanager] 跳过 {file}: {exc}")
                continue
            profiles[profile.id] = profile
    _CACHE = profiles
    return profiles


def resolve_profile(profiles: dict[str, Profile], key: str) -> Profile:
    for profile in profiles.values():
        if profile.matches(key):
            return profile
    options = ", ".join(sorted(profiles))
    raise KeyError(f"未知 Agent：{key}（可用：{options}）")


def writable_profiles(profiles: dict[str, Profile]) -> list[Profile]:
    return [profile for profile in profiles.values() if not profile.readonly]


def detected_profiles(profiles: dict[str, Profile]) -> dict[str, Profile]:
    """Return profiles with at least one real configuration file on this host.

    A bundled profile describes a possible Agent; it is not evidence that the
    Agent is installed.  The GUI should only present profiles whose primary or
    auxiliary configuration exists, while CLI commands may still resolve every
    bundled profile explicitly.
    """
    return {
        profile_id: profile
        for profile_id, profile in profiles.items()
        if any(path.is_file() for path in profile.paths)
    }


def profile_source_kind(profile: Profile) -> str:
    """Return ``override`` for ``local/agents/*.toml`` or ``bundled`` otherwise."""
    if not profile.source:
        return "bundled"
    try:
        if Path(profile.source).resolve().parent == paths.USER_PROFILE_DIR.resolve():
            return "override"
    except OSError:
        pass
    return "bundled"


def _is_redirected(path: Path) -> tuple[bool, Path]:
    """Return ``(redirected, resolved_path)`` for symlinks or Windows junctions."""
    abs_path = Path(os.path.abspath(str(path)))
    real_path = Path(os.path.realpath(str(path)))
    has_link = any(
        candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction())
        for candidate in (abs_path, *abs_path.parents)
    )
    if not has_link:
        return False, real_path
    return os.path.normcase(str(abs_path)) != os.path.normcase(str(real_path)), real_path


def _recognition_tokens(profile: Profile, source_kind: str) -> list[str]:
    tokens = ["local_override" if source_kind == "override" else "bundled_profile"]
    if "{codex_home}" in profile.config:
        tokens.append("CODEX_HOME" if os.environ.get("CODEX_HOME") else "default_codex_home")
    elif "{appdata}" in profile.config:
        tokens.append("APPDATA")
    elif "{localappdata}" in profile.config:
        tokens.append("LOCALAPPDATA")
    elif "{home}" in profile.config:
        tokens.append("HOME")
    else:
        tokens.append("explicit_path")
    if profile.path.is_file():
        tokens.append("config_file")
    elif any(extra.is_file() for extra in profile.paths[1:]):
        tokens.append("auxiliary_config_file")
    return tokens


def detect_agent(profile: Profile) -> dict[str, object]:
    """Return structured detection & config status for one Agent profile."""
    info = describe_profile_paths(profile)
    primary = profile.path
    redirected, resolved = _is_redirected(primary)
    existing_paths = [candidate for candidate in profile.paths if candidate.is_file()]
    configured = bool(existing_paths)
    config_state = "missing"
    config_error: str | None = None
    mcp_count: int | None = 0
    host_count = 0
    source_counts: dict[str, int] = {}
    server_names: list[str] = []
    server_sources: dict[str, str] = {}
    path_duplicates: list[dict[str, object]] = []

    if configured:
        try:
            section, sources = formats.read_merged_section(profile.paths, profile.format, profile.schema)
            real_entries = {
                name: spec
                for name, spec in section.items()
                if isinstance(spec, dict) and not adapters.is_container(spec)
            }
            server_names = list(real_entries)
            mcp_count = len(real_entries)
            server_sources = {name: str(sources[name]) for name in server_names if name in sources}
            per_server_files: dict[str, list[Path]] = {}
            for candidate_path in existing_paths:
                try:
                    part = formats.read_section(candidate_path, profile.format, profile.schema)
                except Exception:
                    continue
                for srv_name, srv_spec in part.items():
                    if isinstance(srv_spec, dict) and not adapters.is_container(srv_spec):
                        per_server_files.setdefault(srv_name, []).append(candidate_path)
            for srv_name, file_list in per_server_files.items():
                if len(file_list) > 1 and srv_name in sources:
                    winner = sources[srv_name]
                    shadowed = [str(p) for p in file_list if p != winner]
                    if shadowed:
                        path_duplicates.append({
                            "name": srv_name,
                            "effectiveSource": str(winner),
                            "shadowedSources": shadowed,
                        })
            for spec in real_entries.values():
                if adapters.level_of(spec, profile) == "host":
                    host_count += 1
                category = adapters.source_category(spec, profile)
                source_counts[category] = source_counts.get(category, 0) + 1
            if mcp_count > 0 and host_count == mcp_count:
                config_state = "plugin_only"
            else:
                config_state = "ok"
        except PermissionError as exc:
            mcp_count = None
            config_state = "permission_error"
            config_error = f"无读取权限：{exc}"
        except (formats.ConfigError, ValueError) as exc:
            mcp_count = None
            config_state = "parse_error"
            config_error = f"配置格式错误：{exc}"
        except OSError as exc:
            mcp_count = None
            config_state = "read_error"
            config_error = f"读取失败：{exc}"

    detected = configured
    evidence = []
    for candidate in info["candidates"]:
        c_path = Path(str(candidate["path"]))
        c_redir, c_real = _is_redirected(c_path)
        evidence.append({
            "path": str(c_path),
            "kind": "effective" if candidate["role"] == "primary" else "auxiliary",
            "exists": bool(candidate["exists"]),
            "redirected": c_redir,
            "resolvedPath": str(c_real),
        })

    return {
        "agent": profile.id,
        "name": profile.name,
        "detected": detected,
        "configured": configured,
        "configState": config_state,
        "configError": config_error,
        "displayPath": str(primary),
        "resolvedPath": str(resolved),
        "redirected": redirected,
        "sourceKind": info["sourceKind"],
        "recognitionBasis": info["recognitionBasis"],
        "recognitionTokens": _recognition_tokens(profile, str(info["sourceKind"])),
        "evidence": evidence,
        "hasMultipleSources": bool(info["hasMultipleExisting"]),
        "priority": info["priority"],
        "priorityNote": info["priorityNote"],
        "mcpCount": mcp_count,
        "hostMcpCount": host_count,
        "sourceCounts": source_counts,
        "serverNames": server_names,
        "serverSources": server_sources,
        "pathDuplicates": path_duplicates,
    }


def describe_profile_paths(profile: Profile) -> dict[str, object]:
    """Summarize primary/auxiliary config paths and how they were resolved."""
    source_kind = profile_source_kind(profile)
    basis_parts = ["本机覆盖 (local/agents)" if source_kind == "override" else "内置 profile"]
    if "{codex_home}" in profile.config:
        if os.environ.get("CODEX_HOME"):
            basis_parts.append("CODEX_HOME 环境变量")
        else:
            basis_parts.append("默认 ~/.codex")
    elif "{appdata}" in profile.config:
        basis_parts.append("APPDATA 目录")
    elif "{localappdata}" in profile.config:
        basis_parts.append("LOCALAPPDATA 目录")
    elif "{home}" in profile.config:
        basis_parts.append("用户主目录")
    else:
        basis_parts.append("显式路径")

    candidates = []
    for index, candidate in enumerate(profile.paths):
        candidates.append({
            "path": str(candidate),
            "role": "primary" if index == 0 else "auxiliary",
            "exists": candidate.exists(),
            "readonly": index > 0 or profile.readonly,
            "optional": False if index == 0 else profile.auxiliary_optional,
        })
    existing_count = sum(1 for item in candidates if item["exists"])
    priority_note = (
        f"检测到 {existing_count} 个候选配置；当前按 {profile.config_priority} 合并，实际加载优先级以 {profile.name} 本身为准。"
        if existing_count > 1
        else ""
    )
    return {
        "sourceKind": source_kind,
        "sourceFile": profile.source,
        "recognitionBasis": " + ".join(basis_parts),
        "candidates": candidates,
        "hasMultipleExisting": existing_count > 1,
        "priority": profile.config_priority,
        "priorityNote": priority_note,
    }
