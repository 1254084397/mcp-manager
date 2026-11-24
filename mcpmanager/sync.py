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

from . import adapters, formats, paths, profiles as profiles_mod, provenance, registry
from .model import Candidate, Profile


class SecretError(Exception):
    """A live entry embeds credentials and the registry has no safe replacement."""


def candidate_path(profile: Profile, index: int = 0) -> Path:
    name = profile.id if index == 0 else f"{profile.id}-{index + 1}"
    return paths.GENERATED_DIR / f"{name}-candidate.json"


def _is_host_managed(name: str, spec: dict[str, Any], profile: Profile, registry_data: dict[str, Any]) -> bool:
    if adapters.level_of(spec, profile) == "host":
        return True
    definition = registry.entry(registry_data, name)
    return bool(definition and definition.get("kind") == "copilot-plugin-host")


def _plan_summary(
    before: dict[str, Any],
    after: dict[str, Any],
    skipped: list[dict[str, str]] | None = None,
    conflicts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    added = sorted(name for name in after if name not in before)
    removed = sorted(name for name in before if name not in after)
    updated = sorted(name for name in after if name in before and after[name] != before[name])
    kept = sorted(name for name in after if name in before and after[name] == before[name])
    return {
        "added": added,
        "updated": updated,
        "kept": kept,
        "removed": removed,
        "skipped": list(skipped or []),
        "conflicts": list(conflicts or []),
    }


def _detect_conflict(
    name: str,
    spec: dict[str, Any],
    profile: Profile,
    registry_data: dict[str, Any],
    managed_entry: dict[str, Any] | None,
) -> str | None:
    """Return a human-readable conflict reason if overwriting would clobber another source."""
    definition = registry.entry(registry_data, name)
    if adapters.is_magpie_managed(spec):
        if definition is not None:
            return "Magpie 与 mcp-manager 注册表同时存在同名 MCP，禁止静默覆盖"
        return None
    if managed_entry is not None:
        recorded_hash = str(managed_entry.get("configHash") or "")
        if recorded_hash and recorded_hash != provenance.spec_hash(spec):
            return "用户已手动修改该受管 MCP，与当前注册表存在冲突，禁止静默覆盖"
        return None
    if definition and (definition.get("command") or definition.get("url")):
        expected = adapters.to_agent(registry.patch_pins(name, definition, registry_data), profile)
        if expected != spec and not adapters.has_inline_secret(spec):
            return "用户手动配置与 mcp-manager 注册表同时存在同名 MCP，禁止静默覆盖"
    return None


def build_candidate(
    profile: Profile,
    registry_data: dict[str, Any],
    source: Path | None = None,
) -> Candidate:
    source = source or profile.path
    if not source.exists():
        raise FileNotFoundError(f"找不到 {profile.name} 配置：{source}")
    section = formats.read_section(source, profile.format, profile.schema)
    managed_map = provenance.load_managed().get(profile.id, {})
    servers: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    for name, spec in section.items():
        if not isinstance(spec, dict):
            servers[name] = spec
            skipped.append({"name": name, "reason": "非对象配置条目，已原样保留"})
            continue
        if _is_host_managed(name, spec, profile, registry_data):
            servers[name] = spec
            skipped.append({"name": name, "reason": "由 Agent 宿主/插件管理，同步时原样保留"})
            continue
        managed_entry = managed_map.get(name) if isinstance(managed_map, dict) else None
        conflict_reason = _detect_conflict(name, spec, profile, registry_data, managed_entry)
        if conflict_reason:
            servers[name] = spec
            conflicts.append({"name": name, "reason": conflict_reason})
            skipped.append({"name": name, "reason": conflict_reason})
            continue
        if adapters.is_magpie_managed(spec):
            servers[name] = spec
            skipped.append({"name": name, "reason": "Magpie 管理的 MCP，同步时原样保留"})
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

    for reg_name, reg_spec in registry.servers(registry_data).items():
        if not isinstance(reg_spec, dict):
            continue
        if reg_spec.get("kind") == "copilot-plugin-host":
            agents = [str(item).lower() for item in reg_spec.get("agents", [])]
            if "*" in agents or any(profile.matches(agent) for agent in agents):
                if not any(registry.canonical(existing) == registry.canonical(reg_name) for existing in servers):
                    skipped.append({"name": reg_name, "reason": "宿主/插件级 MCP，由宿主管理，不写入普通配置"})

    summary = _plan_summary(section, servers, skipped, conflicts)
    candidate = {
        "agent": profile.id,
        "agentName": profile.name,
        "targetPath": str(source),
        "format": profile.format,
        "schema": profile.schema,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
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


def summarize_candidate(path: Path) -> dict[str, Any]:
    """Return ``added``/``updated``/``kept``/``removed``/``skipped`` for a candidate."""
    candidate = load_candidate(path)
    if isinstance(candidate.get("summary"), dict):
        return candidate["summary"]
    target = Path(candidate["targetPath"])
    before: dict[str, Any] = {}
    if target.exists():
        before = formats.read_section(target, candidate["format"], candidate["schema"])
    after = candidate.get("servers") if isinstance(candidate.get("servers"), dict) else {}
    return _plan_summary(before, after, [])


def candidate_diff(path: Path) -> str:
    """Unified diff between the live MCP section and a candidate (secrets masked)."""
    candidate = load_candidate(path)
    target = Path(candidate["targetPath"])
    before: dict[str, Any] = {}
    if target.exists():
        before = formats.read_section(target, candidate["format"], candidate["schema"])
    after = candidate.get("servers") if isinstance(candidate.get("servers"), dict) else {}
    safe_before = adapters.redact_secrets(before)
    safe_after = adapters.redact_secrets(after)
    before_lines = json.dumps(safe_before, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after_lines = json.dumps(safe_after, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
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
    before: dict[str, Any] = {}
    if target.exists():
        before = formats.read_section(target, profile.format, profile.schema)
    after = candidate.get("servers") if isinstance(candidate.get("servers"), dict) else {}
    summary = candidate.get("summary") if isinstance(candidate.get("summary"), dict) else _plan_summary(before, after)
    backup = _backup(target, f"{profile.id}-before-apply") if target.exists() else None
    mode = formats.write_section(target, profile.format, profile.schema, after)
    reg_data = registry.load()
    for name in summary.get("added", []) + summary.get("updated", []):
        if name in after and isinstance(after[name], dict):
            spec_def = registry.entry(reg_data, name) or {}
            field = registry.version_field(spec_def)
            ver = str(spec_def.get(field, "")) if field else ""
            provenance.mark_managed(profile.id, name, after[name], written_version=ver)
    for name in summary.get("removed", []):
        provenance.unmark_managed(profile.id, name)
    provenance.record_audit(
        agent=profile.id,
        target_path=target,
        mode=mode,
        backup_path=backup,
        summary=summary,
        result="ok",
    )
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
    if definition.get("kind") == "copilot-plugin-host" or adapters.level_of(definition, profile) == "host":
        raise ValueError(f"{name} 由 Agent 宿主/插件管理，不能通过普通配置重复安装。")
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
