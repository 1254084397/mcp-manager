"""Last known handshake results, cached in ``local/health.json``.

The cache is advisory: it records the last probe run, not whether the Agent has
loaded the server or whether its business tools work.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import paths

HEALTH_PATH = paths.DATA_DIR / "health.json"


def load() -> dict[str, dict[str, dict[str, Any]]]:
    try:
        data = json.loads(HEALTH_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record(agent: str, name: str, result: dict[str, Any]) -> None:
    if paths.read_only():
        return
    data = load()
    bucket = data.setdefault(agent, {})
    bucket[name] = {
        "ok": bool(result.get("ok")),
        "stage": str(result.get("stage", "")),
        "detail": str(result.get("detail", "")),
        "toolCount": len(result.get("tools") or []),
        "checkedAt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def get(agent: str, name: str) -> dict[str, Any] | None:
    value = load().get(agent, {}).get(name)
    return value if isinstance(value, dict) else None


def summary(agent: str) -> dict[str, dict[str, Any]]:
    value = load().get(agent, {})
    return value if isinstance(value, dict) else {}
