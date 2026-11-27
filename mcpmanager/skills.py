"""Skills discovery, status and 3-mode sync with per-entry ownership & conflict safety."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import paths, provenance, targets

SYNC_MODES = ("shared-directory", "symlink-per-entry", "copy")
SKILLS_STATE_PATH = paths.DATA_DIR / "skills-sync.json"

RECOMMENDED_UBUNTU_SKILLS = (
    "context-mode",
    "jupyter-notebook",
    "nature-figure",
    "scientific-writing",
    "statistical-analysis",
    "research-lookup",
    "nature-academic-search",
    "nature-data",
    "nature-polishing",
    "biopython",
    "scanpy",
    "bulk-rnaseq",
    "nextflow",
    "plant-protection-research",
)


def _dir_hash(folder: Path) -> str:
    h = hashlib.sha256()
    if not folder.is_dir():
        return ""
    for item in sorted(folder.rglob("*")):
        if item.is_file():
            rel = item.relative_to(folder).as_posix()
            h.update(rel.encode("utf-8"))
            try:
                h.update(item.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _snapshot_hash(skill_dir: Path) -> str:
    """Fast structural + content hash of a skills directory for stale-plan detection."""
    if not skill_dir.exists() and not skill_dir.is_symlink():
        return "missing"
    if skill_dir.is_symlink():
        return f"symlink:{os.path.realpath(str(skill_dir))}"
    h = hashlib.sha256()
    try:
        for child in sorted(skill_dir.iterdir(), key=lambda p: p.name.lower()):
            h.update(child.name.encode("utf-8"))
            if child.is_symlink():
                h.update(b":link")
            elif child.is_dir():
                h.update(_dir_hash(child).encode("utf-8"))
            elif child.is_file():
                h.update(child.read_bytes())
    except OSError:
        return "unreadable"
    return h.hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _create_dir_link(src: Path, dst: Path) -> None:
    try:
        os.symlink(src, dst, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=targets.CREATE_NO_WINDOW,
            )
            if res.returncode != 0:
                raise RuntimeError(f"创建目录联接失败：{res.stderr or res.stdout}") from exc
            return
        raise


def _remove_dir_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif hasattr(link, "is_junction") and link.is_junction():
        os.rmdir(str(link))


def _same_physical_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _load_skills_state() -> dict[str, Any]:
    try:
        data = json.loads(SKILLS_STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_skills_state(data: dict[str, Any]) -> None:
    if paths.read_only():
        return
    SKILLS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILLS_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _owned_entries(target_id: str, agent_id: str) -> dict[str, dict[str, Any]]:
    state = _load_skills_state()
    bucket = state.get(target_id, {}).get(agent_id, {})
    entries = bucket.get("entries") if isinstance(bucket, dict) else None
    return entries if isinstance(entries, dict) else {}


def _inspect_skill_dir(skill_dir: Path) -> dict[str, Any]:
    if not skill_dir.exists() and not skill_dir.is_symlink():
        return {"exists": False, "isSymlink": False, "target": None, "entries": [], "systemEntries": [], "mode": "missing"}
    is_dir_link = skill_dir.is_symlink() or (hasattr(skill_dir, "is_junction") and skill_dir.is_junction())
    entries: list[str] = []
    system_entries: list[str] = []
    symlink_entries = 0
    is_wsl_unc = str(skill_dir).startswith("\\\\wsl")
    try:
        for child in sorted(skill_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                system_entries.append(child.name)
                continue
            if child.is_dir() or child.is_symlink() or is_wsl_unc:
                entries.append(child.name)
                if child.is_symlink():
                    symlink_entries += 1
    except OSError:
        pass
    if is_dir_link:
        mode = "shared-directory"
    elif symlink_entries > 0 and symlink_entries == len(entries):
        mode = "symlink-per-entry"
    elif symlink_entries > 0:
        mode = "mixed"
    elif entries:
        mode = "copy"
    else:
        mode = "empty"
    return {
        "exists": True,
        "isSymlink": is_dir_link,
        "target": os.path.realpath(str(skill_dir)) if is_dir_link else None,
        "entries": entries,
        "systemEntries": system_entries,
        "symlinkEntries": symlink_entries,
        "mode": mode,
    }


def list_target_skills(target_id: str | None = None) -> dict[str, Any]:
    target = targets.resolve_target(target_id)
    agents = targets.detect_target_agents(target)
    items = []
    for agent in agents:
        raw_path = agent.get("uncSkillsPath") or agent.get("skillsPath")
        if not raw_path:
            continue
        info = _inspect_skill_dir(Path(raw_path))
        items.append({
            "target": target.id,
            "agent": agent["agent"],
            "name": agent["name"],
            "skillsPath": agent["skillsPath"],
            "mode": info["mode"],
            "autoSyncsOnEdit": info["mode"] == "shared-directory",
            "count": len(info["entries"]),
            "systemEntries": info["systemEntries"],
            "entries": info["entries"],
        })
    return {"target": target.id, "online": target.online, "agents": items}


def skills_status(target_id: str | None = None) -> dict[str, Any]:
    data = list_target_skills(target_id)
    for item in data["agents"]:
        mode = item["mode"]
        if mode == "shared-directory":
            item["syncNote"] = "共享目录模式：源目录增删改会立即可见"
        elif mode == "symlink-per-entry":
            item["syncNote"] = "逐项软链接模式：修改已有 Skill 实时生效，新增/删除需重新执行 skills sync"
        elif mode == "copy":
            item["syncNote"] = "独立副本模式：源目录变更不会自动同步，需重新执行 skills sync"
        else:
            item["syncNote"] = "未配置或空目录"
    return data


def plan_skills_sync(
    target_id: str,
    source_agent: str,
    dest_agent: str,
    mode: str = "symlink-per-entry",
    include: list[str] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    if mode not in SYNC_MODES:
        raise ValueError(f"不支持的同步模式：{mode}（可选：{', '.join(SYNC_MODES)}）")
    target = targets.resolve_target(target_id)
    if not target.online:
        raise RuntimeError(f"目标 {target.id} 当前离线，仅支持只读查看。")
    agents = {item["agent"]: item for item in targets.detect_target_agents(target)}
    if source_agent not in agents:
        raise KeyError(f"目标 {target.id} 中未检测到源 Agent：{source_agent}")
    if dest_agent not in agents:
        raise KeyError(f"目标 {target.id} 中未检测到目标 Agent：{dest_agent}")

    src_info = agents[source_agent]
    dst_info = agents[dest_agent]
    src_fs = Path(src_info.get("uncSkillsPath") or src_info["skillsPath"])
    dst_fs = Path(dst_info.get("uncSkillsPath") or dst_info["skillsPath"])
    if _same_physical_path(src_fs, dst_fs) or src_info["skillsPath"].rstrip("/") == dst_info["skillsPath"].rstrip("/"):
        return {
            "target": target.id,
            "distro": target.distro,
            "kind": target.kind,
            "mode": mode,
            "alreadyShared": True,
            "alreadySynced": True,
            "autoSyncsOnEdit": True,
            "sourceAgent": source_agent,
            "destAgent": dest_agent,
            "sourcePath": src_info["skillsPath"],
            "destPath": dst_info["skillsPath"],
            "sourceFsPath": str(src_fs),
            "destFsPath": str(dst_fs),
            "destSnapshotHash": _snapshot_hash(dst_fs),
            "added": [],
            "updated": [],
            "kept": [],
            "conflicts": [],
            "preserved": [],
            "skipped": [],
        }

    src_scan = _inspect_skill_dir(src_fs)
    dst_scan = _inspect_skill_dir(dst_fs)
    if not src_scan["exists"]:
        raise FileNotFoundError(f"源 Skills 目录不存在：{src_info['skillsPath']}")

    if mode == "shared-directory" and dst_scan["exists"] and not dst_scan["isSymlink"]:
        non_empty = bool(dst_scan["entries"] or dst_scan["systemEntries"])
        if non_empty and not replace:
            raise ValueError(
                f"目标 Skills 目录非空（含 {len(dst_scan['entries'])} 个 Skill），"
                "为保护已有用户数据，shared-directory 不会删除非空目录；请使用 symlink-per-entry 或显式传入 --replace。"
            )

    wanted = set(include) if include else set(src_scan["entries"])
    available = [name for name in src_scan["entries"] if name in wanted]
    missing = sorted(name for name in wanted if name not in src_scan["entries"])
    owned = _owned_entries(target.id, dest_agent)

    existing = set(dst_scan["entries"])
    added: list[str] = []
    updated: list[str] = []
    kept: list[str] = []
    conflicts: list[dict[str, str]] = []
    skipped = [{"name": name, "reason": "源目录中不存在该 Skill"} for name in missing]

    for name in available:
        if name not in existing:
            added.append(name)
            continue
        owner_record = owned.get(name)
        is_owned = isinstance(owner_record, dict) and owner_record.get("owner") == "mcp-manager"
        if not is_owned:
            conflicts.append({"name": name, "reason": "目标目录已存在非 mcp-manager 管理的同名 Skill，已保留"})
            skipped.append({"name": name, "reason": "非 mcp-manager 管理的同名 Skill，禁止静默覆盖"})
            continue
        if mode == "copy":
            item_dst = dst_fs / name
            if _is_link_or_junction(item_dst):
                updated.append(name)
                continue
            src_hash = _dir_hash(src_fs / name)
            dst_hash = _dir_hash(item_dst)
            rec_dst_hash = str(owner_record.get("destinationHash") or "")
            if rec_dst_hash and dst_hash != rec_dst_hash:
                conflicts.append({"name": name, "reason": "目标副本已被用户手动修改，禁止静默覆盖"})
                skipped.append({"name": name, "reason": "目标副本已被手动修改，存在冲突"})
            elif src_hash != dst_hash:
                updated.append(name)
            else:
                kept.append(name)
        else:
            kept.append(name)

    needs_replace_link = mode == "shared-directory" and (not dst_scan["exists"] or (not dst_scan["isSymlink"] and replace))
    return {
        "target": target.id,
        "distro": target.distro,
        "kind": target.kind,
        "mode": mode,
        "replace": replace,
        "alreadyShared": False,
        "alreadySynced": not (added or updated or needs_replace_link),
        "autoSyncsOnEdit": mode == "shared-directory",
        "sourceAgent": source_agent,
        "destAgent": dest_agent,
        "sourcePath": src_info["skillsPath"],
        "destPath": dst_info["skillsPath"],
        "sourceFsPath": str(src_fs),
        "destFsPath": str(dst_fs),
        "destSnapshotHash": _snapshot_hash(dst_fs),
        "added": added,
        "updated": updated,
        "kept": kept,
        "conflicts": conflicts,
        "preserved": dst_scan["systemEntries"],
        "skipped": skipped,
    }


def _record_managed_skills(plan: dict[str, Any]) -> None:
    if paths.read_only():
        return
    state = _load_skills_state()
    target_bucket = state.setdefault(plan["target"], {})
    agent_bucket = target_bucket.setdefault(plan["destAgent"], {})
    entries = agent_bucket.setdefault("entries", {})
    src_fs = Path(plan["sourceFsPath"])
    dst_fs = Path(plan["destFsPath"])
    now = dt.datetime.now().isoformat(timespec="seconds")

    for name in list(plan.get("added", [])) + list(plan.get("updated", [])):
        s_hash = _dir_hash(src_fs / name)
        d_hash = _dir_hash(dst_fs / name) if plan["mode"] == "copy" else s_hash
        entries[name] = {
            "target": plan["target"],
            "agent": plan["destAgent"],
            "skill": name,
            "sourcePath": f"{plan['sourcePath'].rstrip('/')}/{name}",
            "destinationPath": f"{plan['destPath'].rstrip('/')}/{name}",
            "mode": "symlink" if plan["mode"] != "copy" else "copy",
            "owner": "mcp-manager",
            "createdAt": now,
            "sourceHash": s_hash,
            "destinationHash": d_hash,
        }
    agent_bucket["mode"] = plan["mode"]
    agent_bucket["source"] = plan["sourceAgent"]
    agent_bucket["syncedAt"] = now
    _save_skills_state(state)


def _backup_skills_dir(plan: dict[str, Any]) -> Path | None:
    dst_fs = Path(plan["destFsPath"])
    if not dst_fs.exists() and not dst_fs.is_symlink():
        return None
    if dst_fs.is_symlink():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if plan["kind"] == "wsl":
        dst_posix = plan["destPath"].rstrip("/")
        bak_posix = f"{dst_posix}.bak.{stamp}"
        cmd = f"cp -a {shlex.quote(dst_posix)} {shlex.quote(bak_posix)}"
        code, _out, err = targets.run_wsl_bash(plan["distro"], cmd, timeout=30)
        if code != 0:
            raise RuntimeError(f"WSL Skills 备份失败：{err}")
        return targets.wsl_unc_path(plan["distro"], bak_posix)
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_target = plan["target"].replace(":", "-")
    backup_dir = paths.BACKUP_DIR / f"skills-{safe_target}-{plan['destAgent']}-{stamp}"
    shutil.copytree(dst_fs, backup_dir, symlinks=True)
    return backup_dir


def apply_skills_sync(plan: dict[str, Any]) -> dict[str, Any]:
    paths.require_writable("同步 Skills")
    if plan.get("alreadyShared") or plan.get("alreadySynced"):
        return {
            "applied": False,
            "alreadySynced": True,
            "mode": plan["mode"],
            "added": [],
            "updated": [],
            "preserved": plan.get("preserved", []),
            "backup": None,
        }

    mode = plan["mode"]
    kind = plan["kind"]
    src_fs = Path(plan["sourceFsPath"])
    dst_fs = Path(plan["destFsPath"])

    if _same_physical_path(src_fs, dst_fs) or plan["sourcePath"].rstrip("/") == plan["destPath"].rstrip("/"):
        return {
            "applied": False,
            "alreadySynced": True,
            "mode": mode,
            "added": [],
            "updated": [],
            "preserved": plan.get("preserved", []),
            "backup": None,
        }

    expected_hash = plan.get("destSnapshotHash")
    if expected_hash is not None and _snapshot_hash(dst_fs) != expected_hash:
        raise RuntimeError("目标 Skills 目录在生成计划后发生变化，请重新生成同步计划。")

    dst_scan = _inspect_skill_dir(dst_fs)
    if mode == "shared-directory" and dst_scan["exists"] and not dst_scan["isSymlink"]:
        non_empty = bool(dst_scan["entries"] or dst_scan["systemEntries"])
        if non_empty and not plan.get("replace"):
            raise ValueError("目标 Skills 目录非空，shared-directory 禁止直接覆盖（请显式传入 --replace）。")

    backup_path = _backup_skills_dir(plan)
    to_sync = list(plan.get("added", [])) + list(plan.get("updated", []))

    try:
        if kind == "wsl":
            src_posix = plan["sourcePath"].rstrip("/")
            dst_posix = plan["destPath"].rstrip("/")
            distro = plan["distro"]
            if mode == "shared-directory":
                q_dst = shlex.quote(dst_posix)
                q_src = shlex.quote(src_posix)
                cmd = (
                    f"mkdir -p {shlex.quote(os.path.dirname(dst_posix))} && "
                    f"if [ -L {q_dst} ]; then rm -f {q_dst}; elif [ -e {q_dst} ]; then rm -rf {q_dst}; fi && "
                    f"ln -s {q_src} {q_dst}"
                )
            elif mode == "symlink-per-entry":
                links = " && ".join(
                    f"ln -sfn {shlex.quote(src_posix + '/' + name)} {shlex.quote(dst_posix + '/' + name)}"
                    for name in to_sync
                )
                cmd = f"mkdir -p {shlex.quote(dst_posix)}" + (f" && {links}" if links else "")
            else:
                copies = " && ".join(
                    (
                        f"if [ -L {shlex.quote(dst_posix + '/' + name)} ]; then rm -f {shlex.quote(dst_posix + '/' + name)}; "
                        f"elif [ -e {shlex.quote(dst_posix + '/' + name)} ]; then rm -rf {shlex.quote(dst_posix + '/' + name)}; fi && "
                        f"cp -r {shlex.quote(src_posix + '/' + name)} {shlex.quote(dst_posix + '/' + name)}"
                    )
                    for name in to_sync
                )
                cmd = f"mkdir -p {shlex.quote(dst_posix)}" + (f" && {copies}" if copies else "")
            code, _out, err = targets.run_wsl_bash(distro, cmd, timeout=30)
            if code != 0:
                raise RuntimeError(f"WSL Skills 同步失败：{err}")
        else:
            if mode == "shared-directory":
                dst_fs.parent.mkdir(parents=True, exist_ok=True)
                if _is_link_or_junction(dst_fs):
                    _remove_dir_link(dst_fs)
                elif dst_fs.exists():
                    shutil.rmtree(dst_fs)
                _create_dir_link(src_fs, dst_fs)
            elif mode == "symlink-per-entry":
                dst_fs.mkdir(parents=True, exist_ok=True)
                for name in to_sync:
                    target_link = dst_fs / name
                    if not target_link.exists() and not _is_link_or_junction(target_link):
                        _create_dir_link(src_fs / name, target_link)
            else:
                dst_fs.mkdir(parents=True, exist_ok=True)
                for name in to_sync:
                    dest_item = dst_fs / name
                    if _is_link_or_junction(dest_item):
                        _remove_dir_link(dest_item)
                    elif dest_item.exists():
                        shutil.rmtree(dest_item)
                    shutil.copytree(src_fs / name, dest_item)
    except Exception:
        if backup_path is not None and backup_path.exists():
            if _is_link_or_junction(dst_fs):
                _remove_dir_link(dst_fs)
            elif dst_fs.exists():
                shutil.rmtree(dst_fs)
            shutil.copytree(backup_path, dst_fs, symlinks=True)
        raise

    _record_managed_skills(plan)
    skill_changes = [*(f"+{n}" for n in plan.get("added", [])), *(f"~{n}" for n in plan.get("updated", []))]
    provenance.record_audit(
        agent=plan["destAgent"],
        target_path=Path(plan["destPath"]),
        mode=f"skills:{mode}",
        backup_path=backup_path,
        summary={
            "added": list(plan.get("added", [])),
            "updated": list(plan.get("updated", [])),
            "kept": list(plan.get("kept", [])),
            "removed": [],
            "skipped": list(plan.get("skipped", [])),
            "conflicts": list(plan.get("conflicts", [])),
        },
        target_env=plan["target"],
        source=plan["sourceAgent"],
        skill_changes=skill_changes,
        result="ok",
    )
    return {
        "applied": True,
        "alreadySynced": False,
        "mode": mode,
        "added": plan.get("added", []),
        "updated": plan.get("updated", []),
        "preserved": plan.get("preserved", []),
        "backup": str(backup_path) if backup_path else None,
    }
