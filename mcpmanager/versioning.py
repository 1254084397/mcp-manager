"""Version lookup and package upgrades."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from typing import Any

from .registry import normalize_version

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def find_tool(*names: str) -> str | None:
    """Resolve Windows shims such as npm.cmd, which CreateProcess cannot find."""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def npm_latest(package: str) -> str:
    npm = find_tool("npm.cmd", "npm")
    if not npm:
        raise RuntimeError("未找到 npm，请确认 Node.js 已安装并加入 PATH")
    result = subprocess.run(
        [npm, "view", package, "version", "--fetch-timeout=15000", "--fetch-retries=1"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        creationflags=CREATE_NO_WINDOW,
    )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        detail = ((result.stderr or "") + (result.stdout or "")).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"npm 返回 {result.returncode}")
    return lines[-1]


def _pypi_bases() -> list[str]:
    """Official PyPI first, then pip/uv mirrors from the environment."""
    bases: list[str] = []
    for key in ("PIP_INDEX_URL", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_INDEX"):
        value = os.environ.get(key, "").strip()
        if not value.startswith("http"):
            continue
        base = value.split()[0].rstrip("/")
        if base.endswith("/simple"):
            base = base[: -len("/simple")]
        bases.append(base)
    bases.extend(("https://pypi.org", "https://pypi.tuna.tsinghua.edu.cn"))
    unique: list[str] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def pypi_latest(package: str) -> str:
    last_error: Exception | None = None
    for base in _pypi_bases():
        url = f"{base}/pypi/{package}/json"
        for attempt in range(2):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "mcp-manager"})
                with urllib.request.urlopen(request, timeout=12) as response:
                    data = json.loads(response.read().decode("utf-8"))
                info = data.get("info") if isinstance(data, dict) else None
                version = info.get("version") if isinstance(info, dict) else None
                if version:
                    return version
                raise RuntimeError(f"PyPI 返回格式异常，未找到 {package} 的 version 字段")
            except Exception as exc:  # noqa: BLE001 - try the next mirror before failing
                last_error = exc
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(
        f"无法查询 {package} 的版本（{last_error}）。"
        "如网络受限，可设置 PIP_INDEX_URL 指向可用镜像后重试"
    )


def latest_version(spec: dict[str, Any]) -> str:
    package = spec.get("package")
    if not package:
        raise RuntimeError("注册表没有登记 package")
    kind = str(spec.get("kind", ""))
    if any(tag in kind for tag in ("uv", "python", "pip")):
        return pypi_latest(package)
    return npm_latest(package)


def is_newer(latest: str, current: str) -> bool:
    return normalize_version(latest) != normalize_version(current)


def npm_install_global(package: str, version: str) -> tuple[bool, str]:
    npm = find_tool("npm.cmd", "npm")
    if not npm:
        return False, "未找到 npm，请确认 Node.js 已安装并加入 PATH"
    try:
        result = subprocess.run(
            [npm, "install", "-g", f"{package}@{version}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return False, output[-1500:] or f"npm install 返回 {result.returncode}"
    return True, output[-1500:]


def run_custom_update(command: str) -> tuple[bool, str]:
    """Run a user supplied upgrade command (git pull, pip install -U, ...)."""
    command = command.strip()
    if not command:
        return False, "更新命令为空"
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return False, str(exc)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, output[-2000:] or f"命令返回 {result.returncode}"
    return True, output[-2000:]
