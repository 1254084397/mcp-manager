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
            resolved_def = adapters.resolve_target_spec(definition, "windows")
            violation = adapters.target_path_violation(resolved_def, "windows")
            if violation:
                servers[name] = spec
                skipped.append({"name": name, "reason": violation})
                continue
            servers[name] = adapters.to_agent(resolved_def, profile)
            continue
        patched = registry.patch_pins(name, spec, registry_data)
        violation = adapters.target_path_violation(patched, "windows")
        if violation:
            servers[name] = spec
            skipped.append({"name": name, "reason": violation})
            continue
        servers[name] = patched

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
    resolved_def = adapters.resolve_target_spec(pinned, "windows")
    violation = adapters.target_path_violation(resolved_def, "windows")
    if violation:
        raise ValueError(f"{name} 无法安装到 Windows：{violation}")
    section[name] = adapters.to_agent(resolved_def, profile)
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


def build_target_candidate(
    target_id: str,
    agent_id: str,
    registry_data: dict[str, Any],
) -> Path:
    """Build a target-isolated MCP candidate for Windows or WSL targets."""
    from . import targets

    target = targets.resolve_target(target_id)
    if not target.online:
        raise RuntimeError(f"目标 {target.id} 当前离线，仅支持只读查看。")
    if target.kind == "windows":
        profile = profiles_mod.resolve_profile(profiles_mod.load_profiles(), agent_id)
        return build_candidate(profile, registry_data).path

    agents = {item["agent"]: item for item in targets.detect_target_agents(target)}
    if agent_id not in agents:
        raise KeyError(f"目标 {target.id} 中未检测到 Agent：{agent_id}")
    info = agents[agent_id]
    if info.get("readonly"):
        raise ValueError(f"Agent {target.id}/{agent_id} 为只读或未知 Agent，禁止生成写入候选。")
    unc_cfg = Path(info["uncConfigPath"])
    if not unc_cfg.exists():
        raise FileNotFoundError(f"找不到 {target.id}/{agent_id} 配置：{info['configPath']}")

    fake_profile = Profile(
        id=agent_id,
        name=info["name"],
        config=info["configPath"],
        format=info["format"],
        schema=info["schema"],
        command="array" if agent_id == "opencode" else "string",
        env="environment" if agent_id == "opencode" else "env",
        transport="type" if agent_id == "opencode" else "",
        stdio_type="local" if agent_id == "opencode" else "stdio",
        http_type="remote" if agent_id == "opencode" else "http",
    )
    section = formats.read_section(unc_cfg, info["format"], info["schema"])
    servers: dict[str, Any] = {}
    skipped: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    managed_map = provenance.load_managed().get(f"{target.id}/{agent_id}", {})

    for name, spec in section.items():
        if not isinstance(spec, dict):
            servers[name] = spec
            continue
        if adapters.is_magpie_managed(spec):
            servers[name] = spec
            skipped.append({"name": name, "reason": "Magpie 管理的 MCP，原样保留"})
            continue
        if adapters.level_of(spec, fake_profile) == "host":
            servers[name] = spec
            skipped.append({"name": name, "reason": "宿主/插件级 MCP，原样保留"})
            continue
        if adapters.has_inline_secret(spec):
            definition = registry.entry(registry_data, name)
            if not definition:
                raise SecretError(f"{name} 含有内嵌凭据，且无安全替代定义，已停止生成候选。")
            resolved_def = adapters.resolve_target_spec(definition, target.kind)
            violation = adapters.target_path_violation(resolved_def, target.kind)
            if violation:
                servers[name] = spec
                skipped.append({"name": name, "reason": violation})
                continue
            servers[name] = adapters.to_agent(resolved_def, fake_profile)
            continue

        definition = registry.entry(registry_data, name)
        managed_entry = managed_map.get(name) if isinstance(managed_map, dict) else None
        if definition:
            resolved_def = adapters.resolve_target_spec(definition, target.kind)
            violation = adapters.target_path_violation(resolved_def, target.kind)
            if violation:
                servers[name] = spec
                skipped.append({"name": name, "reason": violation})
                continue
            if managed_entry is None:
                conflicts.append({"name": name, "reason": "用户手动配置与注册表同时存在，禁止静默覆盖"})
                skipped.append({"name": name, "reason": "用户手动配置与注册表冲突，已保留"})
                servers[name] = spec
                continue
        patched = registry.patch_pins(name, spec, registry_data)
        violation = adapters.target_path_violation(patched, target.kind)
        if violation:
            servers[name] = spec
            skipped.append({"name": name, "reason": violation})
            continue
        servers[name] = patched

    summary = _plan_summary(section, servers, skipped, conflicts)
    candidate = {
        "targetEnv": target.id,
        "targetKind": target.kind,
        "agent": agent_id,
        "agentName": info["name"],
        "targetPath": info["configPath"],
        "uncTargetPath": str(unc_cfg),
        "format": info["format"],
        "schema": info["schema"],
        "sourceSha256": hashlib.sha256(unc_cfg.read_bytes()).hexdigest(),
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "servers": servers,
    }
    paths.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    safe_target = quote(target.id, safe="-._")
    path = paths.GENERATED_DIR / f"{safe_target}-{agent_id}-candidate.json"
    path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def apply_target_candidate(path: Path) -> tuple[Path | None, str]:
    """Apply a target-isolated candidate with pre-write backup and audit logging."""
    paths.require_writable("应用目标候选配置")
    candidate = load_candidate(path)
    target_env = candidate.get("targetEnv", "windows")
    if target_env == "windows":
        return apply_candidate(path)

    from . import targets

    target = targets.resolve_target(target_env)
    if not target.online:
        raise RuntimeError(f"目标 {target.id} 当前离线，禁止写入。")
    unc_cfg = Path(candidate["uncTargetPath"])
    if not unc_cfg.exists() or hashlib.sha256(unc_cfg.read_bytes()).hexdigest() != candidate["sourceSha256"]:
        raise formats.ConfigError("目标配置在生成候选后已变化，请重新生成候选。")
    after = candidate.get("servers") if isinstance(candidate.get("servers"), dict) else {}
    summary = candidate.get("summary") or {}
    changed_names = set(summary.get("added", [])) | set(summary.get("updated", []))
    for srv_name, srv_spec in after.items():
        if srv_name in changed_names and isinstance(srv_spec, dict):
            violation = adapters.target_path_violation(srv_spec, target.kind)
            if violation:
                raise ValueError(f"{srv_name} 违反跨平台路径隔离：{violation}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = unc_cfg.with_name(f"{unc_cfg.name}.bak.{stamp}")
    shutil.copy2(unc_cfg, backup)
    mode = formats.write_section(unc_cfg, candidate["format"], candidate["schema"], after)
    provenance.record_audit(
        agent=candidate["agent"],
        target_path=Path(candidate["targetPath"]),
        mode=mode,
        backup_path=backup,
        summary=summary,
        target_env=target_env,
        result="ok",
    )
    return backup, mode


def rollback_from_audit(target_env: str, agent_id: str) -> tuple[Path, Path | None]:
    """Restore the most recent backup recorded in ``local/audit.jsonl`` (MCP or Skills)."""
    paths.require_writable("回滚配置")
    entries = provenance.load_audit(agent=agent_id, target_env=target_env, limit=50)
    chosen = next(
        (item for item in reversed(entries) if item.get("backupPath") and Path(item["backupPath"]).exists()),
        None,
    )
    if chosen is None:
        raise FileNotFoundError(f"未找到 {target_env}/{agent_id} 可用的回滚备份记录。")
    backup_src = Path(chosen["backupPath"])
    is_skills = str(chosen.get("mode", "")).startswith("skills:")

    if is_skills:
        if target_env == "windows":
            live_path = Path(chosen["targetPath"])
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safety = paths.BACKUP_DIR / f"skills-before-rollback-{agent_id}-{stamp}"
            if live_path.exists() or live_path.is_symlink():
                if live_path.is_symlink():
                    live_path.unlink()
                else:
                    shutil.copytree(live_path, safety, symlinks=True)
                    shutil.rmtree(live_path)
            else:
                safety = None
            shutil.copytree(backup_src, live_path, symlinks=True)
        else:
            from . import targets

            target = targets.resolve_target(target_env)
            if not target.online:
                raise RuntimeError(f"目标 {target.id} 当前离线，禁止回滚写入。")
            live_path = targets.wsl_unc_path(target.distro, chosen["targetPath"])
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            safety = live_path.with_name(f"{live_path.name}.bak.before-rollback-{stamp}")
            if live_path.exists() or live_path.is_symlink():
                if live_path.is_symlink():
                    live_path.unlink()
                    safety = None
                else:
                    shutil.copytree(live_path, safety, symlinks=True)
                    shutil.rmtree(live_path)
            else:
                safety = None
            shutil.copytree(backup_src, live_path, symlinks=True)
        provenance.record_audit(
            agent=agent_id,
            target_path=Path(chosen["targetPath"]),
            mode="skills:rollback",
            backup_path=safety,
            summary={"added": [], "updated": [], "kept": [], "removed": [], "skipped": [], "conflicts": []},
            target_env=target_env,
            result="ok",
        )
        return backup_src, safety

    if target_env == "windows":
        profile = profiles_mod.resolve_profile(profiles_mod.load_profiles(), agent_id)
        live_path = profile.path
        safety = _backup(live_path, f"{agent_id}-before-rollback") if live_path.exists() else None
    else:
        from . import targets

        target = targets.resolve_target(target_env)
        if not target.online:
            raise RuntimeError(f"目标 {target.id} 当前离线，禁止回滚写入。")
        live_path = targets.wsl_unc_path(target.distro, chosen["targetPath"])
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safety = live_path.with_name(f"{live_path.name}.bak.before-rollback-{stamp}") if live_path.exists() else None
        if safety is not None:
            shutil.copy2(live_path, safety)
    live_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_src, live_path)
    provenance.record_audit(
        agent=agent_id,
        target_path=Path(chosen["targetPath"]),
        mode="rollback",
        backup_path=safety,
        summary={"added": [], "updated": [], "kept": [], "removed": [], "skipped": [], "conflicts": []},
        target_env=target_env,
        result="ok",
    )
    return backup_src, safety
