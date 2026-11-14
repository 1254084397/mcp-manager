"""Version lookup and package upgrades."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

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


STATUS_LATEST = "已是最新"
STATUS_NEWER = "有新版本"
STATUS_CURRENT_HIGHER = "当前版本更高"
STATUS_UNCOMPARABLE = "无法比较"
STATUS_MISSING = "未安装"
STATUS_FAILED = "查询失败"

_PRE_RANK = {
    "dev": 0,
    "a": 1,
    "alpha": 1,
    "b": 2,
    "beta": 2,
    "rc": 3,
    "c": 3,
    "pre": 3,
    "preview": 3,
    "post": 4,
}


def parse_version(value: str) -> tuple[tuple[int, ...], int, int, int] | None:
    """Parse numeric versions with optional prerelease/post suffixes.

    Returns ``(numeric_parts, stage, prerelease_rank, prerelease_number)`` where
    stage is 0 for prerelease, 1 for final, 2 for post release. ``None`` means
    the string is not a comparable version (``latest``, ``next``, a commit SHA).
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.startswith("v"):
        text = text[1:]
    text = text.split("+", 1)[0]
    match = re.match(
        r"^(\d+(?:\.\d+)*)(?:[-._]?(dev|a|alpha|b|beta|rc|c|pre|preview|post))?(\d*)$",
        text,
    )
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    label = match.group(2) or ""
    number = int(match.group(3) or 0)
    if not label:
        return tuple(numbers), 1, 0, 0
    rank = _PRE_RANK.get(label, 0)
    return tuple(numbers), 2 if label == "post" else 0, rank, number


def compare_versions(current: str, latest: str) -> tuple[str, int | None]:
    """Compare a registered version with a queried latest version."""
    if not str(current or "").strip():
        return STATUS_MISSING, None
    left = parse_version(current)
    right = parse_version(latest)
    if left is None or right is None:
        return STATUS_UNCOMPARABLE, None
    if left < right:
        return STATUS_NEWER, -1
    if left > right:
        return STATUS_CURRENT_HIGHER, 1
    return STATUS_LATEST, 0


def is_newer(latest: str, current: str) -> bool:
    return compare_versions(current, latest)[0] == STATUS_NEWER



def npm_install_global(package: str, version: str) -> tuple[bool, str]:
    from . import paths

    paths.require_writable("安装 npm 包")
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


SHELL_OPERATORS = {"&&", "||", "|", ";", ">", ">>", "<", "&"}
SHELL_META = set("&|;<>")
CMD_META = set("&|;<>^()")

SHELL_ENTRYPOINTS = {
    "cmd", "cmd.exe",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "bash", "bash.exe", "sh", "sh.exe", "zsh", "zsh.exe",
    "wsl", "wsl.exe", "cscript", "cscript.exe", "wscript", "wscript.exe",
    "mshta", "mshta.exe",
}

# .cmd/.bat shims that package managers install on Windows. Anything else must
# be invoked through its real .exe so the manager never hands arbitrary batch
# files to cmd.exe.
ALLOWED_CMD_SHIMS = {"npm.cmd", "npx.cmd", "pnpm.cmd", "yarn.cmd", "corepack.cmd"}


def _metacharacters(value: str, allowed: set[str]) -> list[str]:
    return sorted({char for char in value if char in allowed})


def format_command(command: str | list[str] | tuple[str, ...] | None) -> str:
    """Render a stored update command for display without executing it."""
    if isinstance(command, (list, tuple)):
        return subprocess.list2cmdline([str(item) for item in command])
    return str(command or "").strip()


def split_command(command: str | list[str] | tuple[str, ...]) -> list[str]:
    """Turn a stored command into an argv list.

    Strings are tokenised without invoking a shell. Windows quoting is kept
    intact so paths such as ``"D:\\AI\\my server"`` survive, while quotes around
    a whole token are removed for subprocess.
    """
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command if str(item)]
    text = str(command).strip()
    if not text:
        return []
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        return [
            item[1:-1]
            if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'"
            else item
            for item in parts
            if item
        ]
    return shlex.split(text)


def run_custom_update(
    command: str | list[str] | tuple[str, ...], cwd: str | Path | None = None
) -> tuple[bool, str]:
    """Run a stored upgrade command as argv.

    The manager never invokes a shell on its own. Shell interpreters and
    arbitrary ``.cmd``/``.bat`` files are rejected; only known package-manager
    shims are allowed through ``cmd /c``.
    """
    from . import paths

    if paths.read_only():
        return False, "只读模式：禁止执行更新命令"
    parts = split_command(command)
    if not parts:
        return False, "更新命令为空"
    operators = [part for part in parts if part in SHELL_OPERATORS]
    if operators:
        return False, (
            "更新命令不支持 shell 操作符（"
            + "、".join(operators)
            + "）。请拆成单条命令，或在注册表中改用参数数组。"
        )
    risky = sorted({char for part in parts[1:] for char in _metacharacters(part, SHELL_META)})
    if risky:
        return False, (
            "更新命令参数包含 shell 元字符（"
            + "、".join(risky)
            + "）。为避免被 cmd 解释，请去掉 &、|、;、<、> 等字符。"
        )
    executable = parts[0]
    if os.path.dirname(executable):
        resolved = executable if Path(executable).is_file() else None
    else:
        resolved = shutil.which(executable)
    if not resolved:
        return False, f"未找到可执行文件：{executable}"
    name = Path(resolved).name.lower()
    if name in SHELL_ENTRYPOINTS:
        return False, (
            "不允许把 cmd、PowerShell、bash 等 shell 解释器作为更新命令入口。"
            "请直接填写要运行的 .exe，并在注册表中改用参数数组。"
        )
    argv = [resolved, *parts[1:]]
    if os.name == "nt" and name.endswith((".cmd", ".bat")):
        if name not in ALLOWED_CMD_SHIMS:
            return False, (
                "不允许直接执行任意 .cmd/.bat 更新命令。"
                "请改用对应的 .exe，或只使用 npm/npx/pnpm/yarn/corepack 包装脚本。"
            )
        cmd_risky = sorted({char for part in parts[1:] for char in _metacharacters(part, CMD_META)})
        if cmd_risky:
            return False, (
                "该 .cmd 更新命令的参数包含 cmd 元字符（"
                + "、".join(cmd_risky)
                + "），会被 cmd /c 解释，已拒绝执行。"
            )
        argv = ["cmd", "/c", *argv]
    try:
        result = subprocess.run(
            argv,
            shell=False,
            cwd=str(cwd) if cwd else None,
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

