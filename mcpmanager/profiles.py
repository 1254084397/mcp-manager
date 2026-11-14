"""Load Agent profiles from declarative TOML files."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from . import paths
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
