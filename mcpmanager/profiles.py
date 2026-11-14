"""Load Agent profiles from declarative TOML files."""

from __future__ import annotations

import tomllib
from pathlib import Path

from . import paths
from .model import Profile

_CACHE: dict[str, Profile] | None = None


def _profile_from_file(path: Path) -> Profile:
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    if "id" not in data or "config" not in data:
        raise ValueError(f"{path.name}: profile needs at least id and config")
    known = {field.name for field in Profile.__dataclass_fields__.values()}
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
