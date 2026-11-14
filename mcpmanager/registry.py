"""The MCP registry: desired servers, versions and pins."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import paths

SERVER_ALIASES = {"laya-decision": "laya"}


def canonical(name: str) -> str:
    key = name.strip().lower()
    return SERVER_ALIASES.get(key, key)


def load(path: Path | None = None) -> dict[str, Any]:
    target = path or paths.REGISTRY_PATH
    paths.ensure_layout()
    if not target.exists() and paths.read_only():
        return copy.deepcopy(paths.EMPTY_REGISTRY)
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"注册表不存在：{target}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"注册表不是合法 JSON：{target}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"注册表顶层必须是对象：{target}")
    data.setdefault("servers", {})
    return data


def save(data: dict[str, Any], path: Path | None = None) -> None:
    paths.require_writable("写入注册表")
    target = path or paths.REGISTRY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def servers(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("servers")
    return value if isinstance(value, dict) else {}


def entry(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    pool = servers(data)
    if name in pool:
        return pool[name]
    wanted = canonical(name)
    for candidate, spec in pool.items():
        if canonical(candidate) == wanted:
            return spec
    return None


def version_field(spec: dict[str, Any]) -> str | None:
    if spec.get("versionPolicy") == "pin-before-sync" and "version" in spec:
        return "version"
    if "installedVersion" in spec:
        return "installedVersion"
    if "version" in spec:
        return "version"
    return None


def normalize_version(text: Any) -> str:
    return str(text or "").strip().lstrip("vV")


def _pin_token(text: str, package: str, version: str, separator: str) -> str:
    if text == package:
        return f"{package}{separator}{version}"
    for prefix in (package + "==", package + "@"):
        if text.startswith(prefix):
            return f"{package}{separator}{version}"
    return text


def patch_pins(name: str, spec: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Pin package versions declared with ``versionPolicy = pin-before-sync``."""
    result = copy.deepcopy(spec)
    definition = entry(data, name)
    if not definition or definition.get("versionPolicy") != "pin-before-sync":
        return result
    package = definition.get("package")
    version = definition.get("version")
    if not package or not version:
        return result
    kind = str(definition.get("kind", ""))
    separator = "==" if any(tag in kind for tag in ("uv", "python", "pip")) else "@"

    def rewrite(items: list[Any]) -> list[Any]:
        output: list[Any] = []
        pin_next = False
        for item in items:
            text = str(item)
            if pin_next:
                text = _pin_token(text, package, version, separator)
                pin_next = False
            elif text == "--from":
                pin_next = True
            elif text.startswith(package + "==") or text.startswith(package + "@"):
                text = _pin_token(text, package, version, separator)
            elif separator == "@" and text == package:
                text = _pin_token(text, package, version, separator)
            output.append(text)
        return output

    if isinstance(result.get("args"), list):
        result["args"] = rewrite(result["args"])
    if isinstance(result.get("command"), list):
        result["command"] = rewrite(result["command"])
    elif isinstance(result.get("command"), str):
        result["command"] = _pin_token(result["command"], package, version, separator)
    return result


def set_version(data: dict[str, Any], name: str, version: str) -> None:
    spec = entry(data, name)
    if spec is None:
        raise KeyError(f"{name} 不在主注册表中")
    field = version_field(spec)
    if field is None:
        raise KeyError(f"{name} 没有可编辑的版本字段")
    spec[field] = version


def add_server(data: dict[str, Any], name: str, spec: dict[str, Any]) -> None:
    pool = servers(data)
    pool[name] = spec
    data["servers"] = pool


def remove_server(data: dict[str, Any], name: str) -> bool:
    pool = servers(data)
    if name in pool:
        del pool[name]
        return True
    wanted = canonical(name)
    for key in list(pool):
        if canonical(key) == wanted:
            del pool[key]
            return True
    return False
