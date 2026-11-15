"""Generate, review and apply per-Agent MCP candidates."""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import adapters, formats, paths, profiles as profiles_mod, registry
from .model import Candidate, Profile


class SecretError(Exception):
    """A live entry embeds credentials and the registry has no safe replacement."""


def candidate_path(profile: Profile, index: int = 0) -> Path:
    name = profile.id if index == 0 else f"{profile.id}-{index + 1}"
    return paths.GENERATED_DIR / f"{name}-candidate.json"


def build_candidate(
    profile: Profile,
    registry_data: dict[str, Any],
    source: Path | None = None,
) -> Candidate:
    source = source or profile.path
    if not source.exists():
        raise FileNotFoundError(f"找不到 {profile.name} 配置：{source}")
    section = formats.read_section(source, profile.format, profile.schema)
    servers: dict[str, Any] = {}
    for name, spec in section.items():
        if not isinstance(spec, dict):
            servers[name] = spec
            continue
        if adapters.has_inline_secret(spec):
            definition = registry.entry(registry_data, name)
            if not definition or not (definition.get("command") or definition.get("url")):
                raise SecretError(
                    f"{name} 含有内嵌凭据，且主注册表没有可替代的无密配置，已停止生成候选。"
                )
            servers[name] = adapters.to_agent(definition, profile)
            continue
        servers[name] = registry.patch_pins(name, spec, registry_data)

    candidate = {
        "agent": profile.id,
        "agentName": profile.name,
        "targetPath": str(source),
        "format": profile.format,
        "schema": profile.schema,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "servers": servers,
    }
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = candidate_path(profile)
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return Candidate(
        profile=profile,
        target=source,
        servers=servers,
        source_sha256=candidate["sourceSha256"],
        generated_at=candidate["generatedAt"],
        path=path,
    )


def load_candidate(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def is_fresh(candidate: dict[str, Any]) -> bool:
    target = Path(candidate["targetPath"])
    if not target.exists():
        return candidate.get("sourceSha256") == ""
    return hashlib.sha256(target.read_bytes()).hexdigest() == candidate["sourceSha256"]


def candidate_diff(path: Path) -> str:
    """Unified diff between the live MCP section and a candidate."""
    candidate = load_candidate(path)
    target = Path(candidate["targetPath"])
    before: dict[str, Any] = {}
    if target.exists():
        before = formats.read_section(target, candidate["format"], candidate["schema"])
    after = candidate.get("servers") if isinstance(candidate.get("servers"), dict) else {}
    before_lines = json.dumps(before, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after_lines = json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"当前 {target}",
        tofile=f"候选 {path.name}",
        lineterm="",
    ))


def _backup(target: Path, label: str) -> Path:
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = paths.BACKUP_DIR / f"{label}-{stamp}{target.suffix or '.json'}"
    shutil.copy2(target, backup)
    return backup


def apply_candidate(path: Path) -> tuple[Path | None, str]:
    paths.require_writable("应用候选配置")
    candidate = load_candidate(path)
    if not is_fresh(candidate):
        raise formats.ConfigError("目标配置在生成候选后已变化，请重新生成候选并检查差异。")
    profile = profiles_mod.resolve_profile(profiles_mod.load_profiles(), candidate["agent"])
    target = Path(candidate["targetPath"])
    if target != profile.path:
        raise formats.ConfigError(f"候选目标与 Agent profile 不一致：{target} != {profile.path}")
    if profile.readonly:
        raise formats.ConfigError(f"{profile.name} 被标记为只读，不写入。")
    backup = _backup(target, f"{profile.id}-before-apply") if target.exists() else None
    mode = formats.write_section(target, profile.format, profile.schema, candidate["servers"])
    return backup, mode


def apply_section(profile: Profile, servers: dict[str, Any]) -> tuple[Path | None, str]:
    paths.require_writable("写入 Agent 配置")
    if profile.readonly:
        raise formats.ConfigError(f"{profile.name} 被标记为只读，不写入。")
    target = profile.path
    backup = _backup(target, f"{profile.id}-before-apply") if target.exists() else None
    mode = formats.write_section(target, profile.format, profile.schema, servers)
    return backup, mode


def install_candidate(profile: Profile, registry_data: dict[str, Any], name: str) -> Path:
    """Build a one-server candidate without touching the live config."""
    paths.require_writable("生成安装候选")
    definition = registry.entry(registry_data, name)
    if definition is None:
        raise KeyError(f"{name} 不在主注册表中")
    section: dict[str, Any] = {}
    if profile.path.exists():
        section = formats.read_section(profile.path, profile.format, profile.schema)
    pinned = registry.patch_pins(name, definition, registry_data)
    section[name] = adapters.to_agent(pinned, profile)
    candidate = {
        "agent": profile.id,
        "agentName": profile.name,
        "targetPath": str(profile.path),
        "format": profile.format,
        "schema": profile.schema,
        "sourceSha256": hashlib.sha256(profile.path.read_bytes()).hexdigest() if profile.path.exists() else "",
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "installOnly": True,
        "servers": section,
    }
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = quote(name, safe="-._")
    path = paths.GENERATED_DIR / f"{profile.id}-install-{safe_name}.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def uninstall_candidate(profile: Profile, name: str) -> Path:
    """Build a candidate that removes one MCP from the live config."""
    paths.require_writable("生成卸载候选")
    if not profile.path.exists():
        raise FileNotFoundError(f"找不到 {profile.name} 配置：{profile.path}")
    section = formats.read_section(profile.path, profile.format, profile.schema)
    key = name if name in section else next(
        (item for item in section if registry.canonical(item) == registry.canonical(name)), None
    )
    if key is None:
        raise KeyError(f"{profile.name} 配置里没有 {name}")
    section.pop(key)
    candidate = {
        "agent": profile.id,
        "agentName": profile.name,
        "targetPath": str(profile.path),
        "format": profile.format,
        "schema": profile.schema,
        "sourceSha256": hashlib.sha256(profile.path.read_bytes()).hexdigest(),
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "removeOnly": True,
        "servers": section,
    }
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = quote(name, safe="-._")
    path = paths.GENERATED_DIR / f"{profile.id}-uninstall-{safe_name}.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def attach_agent_and_install(
    profile: Profile, registry_data: dict[str, Any], name: str
) -> tuple[Path, bool]:
    """Attach the agent to an existing registry MCP's agents list and generate a candidate.

    The registry entry must already exist. The agent id is appended to its
    ``agents`` list only when absent. Returns ``(candidate_path, registry_modified)``
    so the caller can persist the registry exactly once after the candidate is
    successfully generated.
    """
    entry = registry.entry(registry_data, name)
    if entry is None:
        raise KeyError(f"{name} 不在注册表中")
    agents = entry.get("agents")
    if not isinstance(agents, list):
        agents = []
    agent_added = profile.id not in agents
    if agent_added:
        agents.append(profile.id)
        entry["agents"] = agents
    candidate = install_candidate(profile, registry_data, name)
    return candidate, agent_added
