"""Managed-entry markers and auditable sync history in ``local/``.

Managed markers live in ``local/managed.json`` rather than inside Agent config
files so strict Agent schemas (VS Code, Codex TOML, OpenCode) are never
polluted with unknown keys.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import paths

MANAGED_PATH = paths.DATA_DIR / "managed.json"
AUDIT_PATH = paths.DATA_DIR / "audit.jsonl"


def spec_hash(spec: Any) -> str:
    """Deterministic SHA-256 of a canonicalized server spec."""
    payload = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_managed(path: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    target = path or MANAGED_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_managed(data: dict[str, dict[str, dict[str, Any]]], path: Path | None = None) -> None:
    if paths.read_only():
        return
    target = path or MANAGED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_managed(agent: str, name: str, path: Path | None = None) -> dict[str, Any] | None:
    bucket = load_managed(path).get(agent, {})
    entry = bucket.get(name) if isinstance(bucket, dict) else None
    return entry if isinstance(entry, dict) else None


def mark_managed(
    agent: str,
    name: str,
    spec: Any,
    written_version: str = "",
    source_registry: str | None = None,
    path: Path | None = None,
) -> None:
    if paths.read_only():
        return
    data = load_managed(path)
    bucket = data.setdefault(agent, {})
    bucket[name] = {
        "managedBy": "mcp-manager",
        "agent": agent,
        "name": name,
        "writtenVersion": str(written_version or ""),
        "sourceRegistry": str(source_registry or paths.REGISTRY_PATH),
        "configHash": spec_hash(spec),
        "updatedAt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    save_managed(data, path)


def unmark_managed(agent: str, name: str, path: Path | None = None) -> None:
    if paths.read_only():
        return
    data = load_managed(path)
    bucket = data.get(agent)
    if isinstance(bucket, dict) and name in bucket:
        bucket.pop(name)
        save_managed(data, path)


def record_audit(
    *,
    agent: str,
    target_path: Path,
    mode: str,
    backup_path: Path | None,
    summary: dict[str, Any],
    result: str = "ok",
    error: str | None = None,
    target_env: str = "windows",
    source: str = "mcp-manager",
    skill_changes: list[Any] | None = None,
    path: Path | None = None,
) -> None:
    if paths.read_only():
        return
    target = path or AUDIT_PATH
    now = dt.datetime.now().isoformat(timespec="seconds")
    mcp_changes = [
        *(f"+{name}" for name in summary.get("added", [])),
        *(f"~{name}" for name in summary.get("updated", [])),
        *(f"-{name}" for name in summary.get("removed", [])),
    ]
    entry = {
        "createdAt": now,
        "timestamp": now,
        "targetEnv": target_env,
        "agent": agent,
        "target": str(target_path),
        "targetPath": str(target_path),
        "mode": mode,
        "backup": str(backup_path) if backup_path else None,
        "backupPath": str(backup_path) if backup_path else None,
        "added": list(summary.get("added", [])),
        "updated": list(summary.get("updated", [])),
        "removed": list(summary.get("removed", [])),
        "preserved": list(summary.get("kept", [])),
        "skipped": list(summary.get("skipped", [])),
        "conflicts": list(summary.get("conflicts", [])),
        "mcpChanges": mcp_changes,
        "skillChanges": list(skill_changes or []),
        "source": source,
        "summary": summary,
        "result": result,
        "error": error,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_audit(
    agent: str | None = None,
    target_env: str | None = None,
    limit: int = 20,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    target = path or AUDIT_PATH
    if not target.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        if agent and item.get("agent") != agent:
            continue
        if target_env and item.get("targetEnv", "windows") != target_env:
            continue
        entries.append(item)
    return entries[-limit:] if limit > 0 else entries
