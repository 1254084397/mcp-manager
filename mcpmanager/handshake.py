"""Real MCP handshake: start the server and speak JSON-RPC to it.

``doctor`` only proves that a command exists on disk. This module goes one step
further: it launches the stdio server (or POSTs to a remote endpoint), sends
``initialize``, then ``tools/list``, and reports which stage failed. It never
calls a tool that could mutate data.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import adapters
from .model import Profile
from .versioning import CREATE_NO_WINDOW

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "mcp-manager", "version": "1.0"}

STAGE_CONFIG = "config"
STAGE_START = "start"
STAGE_INITIALIZE = "initialize"
STAGE_TOOLS = "tools"
STAGE_READY = "ready"


class ProbeError(Exception):
    """A handshake stage failed; ``stage``/``kind`` drive the user-facing text."""

    def __init__(self, stage: str, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.kind = kind
        self.detail = detail


def _result(ok: bool, stage: str, detail: str, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ok": ok,
        "stage": stage,
        "detail": detail,
        "tools": [],
        "serverInfo": {},
    }
    data.update(extra)
    return data


def classify_failure(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in (
        "unauthorized", "forbidden", "401", "403", "auth", "token", "api key", "apikey", "credential",
    )):
        return "auth"
    return "protocol"


def _environment(spec: dict[str, Any], profile: Profile) -> dict[str, str]:
    env = os.environ.copy()
    extra = spec.get(profile.env) or spec.get("env") or spec.get("environment")
    if isinstance(extra, dict):
        for key, value in extra.items():
            env[str(key)] = str(value)
    return env


def _argv(command: str, args: list[str]) -> list[str]:
    if command and (Path(command).is_absolute() or any(sep in command for sep in ("/", "\\"))):
        exe = command
    else:
        exe = shutil.which(command) or command
    argv = [exe, *args]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    return argv


def _request(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    }


def probe(profile: Profile, name: str, spec: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    """Probe one MCP and return ``{ok, stage, detail, tools, serverInfo}``."""
    if not isinstance(spec, dict):
        return _result(False, STAGE_CONFIG, "配置不是对象")
    if adapters.level_of(spec, profile) == "host":
        return _result(
            False, STAGE_CONFIG, "宿主/插件级 MCP，由 Agent 运行时启动，无法独立握手", skipped=True
        )
    command = adapters.command_of(spec)
    url = adapters.url_of(spec, profile)
    if not command and not url:
        return _result(False, STAGE_CONFIG, "配置缺少 command/url")
    try:
        if url:
            return _probe_http(url, spec, timeout)
        return _probe_stdio(command or "", adapters.args_of(spec), spec, profile, timeout)
    except ProbeError as exc:
        return _result(False, exc.stage, exc.detail, kind=exc.kind, tool=name)


# --------------------------------------------------------------------------- #
# stdio
# --------------------------------------------------------------------------- #
def _drain(stream, sink: list[str]) -> None:
    try:
        for line in stream:
            sink.append(line)
            if len(sink) > 200:
                del sink[:100]
    except Exception:
        pass


def _probe_stdio(
    command: str,
    args: list[str],
    spec: dict[str, Any],
    profile: Profile,
    timeout: float,
) -> dict[str, Any]:
    argv = _argv(command, args)
    cwd = spec.get("cwd")
    options: dict[str, Any] = {}
    if os.name != "nt":
        # Own process group so timeout cleanup can reach grandchildren too.
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(cwd) if isinstance(cwd, str) and cwd else None,
            env=_environment(spec, profile),
            creationflags=CREATE_NO_WINDOW,
            **options,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a start failure
        return _result(False, STAGE_START, f"启动失败：{exc}", kind="start")

    lines: queue.Queue[str | None] = queue.Queue()
    stderr: list[str] = []

    def reader() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    if process.stderr is not None:
        threading.Thread(target=_drain, args=(process.stderr, stderr), daemon=True).start()

    def send(payload: dict[str, Any]) -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except Exception as exc:  # noqa: BLE001 - the process died early
            raise ProbeError(STAGE_INITIALIZE, "start", f"写入失败：{exc}") from exc

    def wait_for(request_id: int, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.4)
            except queue.Empty:
                if process.poll() is not None and lines.empty():
                    detail = "MCP 进程提前退出" + _stderr_hint(stderr)
                    raise ProbeError(STAGE_INITIALIZE, classify_failure(detail), detail)
                continue
            if line is None:
                detail = "MCP 进程已关闭输出" + _stderr_hint(stderr)
                raise ProbeError(STAGE_INITIALIZE, classify_failure(detail), detail)
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue  # servers may print logs on stdout before the greeting
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
        raise ProbeError(STAGE_INITIALIZE, "timeout", "等待初始化响应超时")

    try:
        send(_request(1, "initialize", _initialize_params()))
        response = wait_for(1, time.monotonic() + timeout)
        result = response.get("result")
        if not isinstance(result, dict):
            detail = _error_text(response) or "initialize 未返回 result"
            raise ProbeError(STAGE_INITIALIZE, classify_failure(detail), f"初始化失败：{detail}")
        server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        protocol = str(result.get("protocolVersion") or "")

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(_request(2, "tools/list"))
        try:
            tools_response = wait_for(2, time.monotonic() + timeout)
        except ProbeError:
            return _result(
                True,
                STAGE_TOOLS,
                "已初始化，但 tools/list 无响应（部分服务可能不声明工具）",
                serverInfo=server_info,
                protocolVersion=protocol,
            )
        if "error" in tools_response:
            return _result(
                True,
                STAGE_TOOLS,
                "已初始化，但 tools/list 返回错误：" + (_error_text(tools_response) or "未知错误"),
                serverInfo=server_info,
                protocolVersion=protocol,
            )
        tools = tools_response.get("result", {}).get("tools", []) if isinstance(tools_response.get("result"), dict) else []
        names = [str(item.get("name", "")) for item in tools if isinstance(item, dict)]
        return _result(
            True,
            STAGE_READY,
            f"握手成功，发现 {len(names)} 个工具",
            tools=names,
            serverInfo=server_info,
            protocolVersion=protocol,
        )
    except ProbeError as exc:
        return _result(False, exc.stage, exc.detail, kind=exc.kind)
    finally:
        _stop(process)


def _stderr_hint(stderr: list[str]) -> str:
    text = " ".join(line.strip() for line in stderr[-5:] if line.strip())
    return f"：{text[-300:]}" if text else ""


def _stop(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # MCP servers often spawn node/browser children; taskkill /T removes the
        # whole tree, while terminate() would only stop the parent. Do this
        # before closing stdin so the parent is still alive while /T walks it.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    elif process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=2)
        except Exception:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
    try:
        if process.stdin:
            process.stdin.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# remote HTTP
# --------------------------------------------------------------------------- #
def _headers(spec: dict[str, Any], session: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "mcp-manager",
    }
    extra = spec.get("headers")
    if isinstance(extra, dict):
        headers.update({str(key): str(value) for key, value in extra.items()})
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


def _parse_body(text: str, content_type: str) -> tuple[dict[str, Any] | None, str | None]:
    body = text.strip()
    if "text/event-stream" in content_type.lower() or body.startswith("data:"):
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                message = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                return message, None
        return None, "SSE 响应里没有 JSON 消息"
    try:
        message = json.loads(body)
    except json.JSONDecodeError:
        return None, "响应不是合法 JSON"
    return (message, None) if isinstance(message, dict) else (None, "响应不是 JSON 对象")


def _post(url: str, payload: dict[str, Any], spec: dict[str, Any],
          timeout: float, session: str | None) -> tuple[dict[str, Any] | None, str | None, str | None]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(spec, session),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
        return (*_parse_body(text, response.headers.get("Content-Type", "")),
                response.headers.get("Mcp-Session-Id"))


def _probe_http(url: str, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        message, parse_error, session = _post(url, _request(1, "initialize", _initialize_params()),
                                              spec, timeout, None)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(False, STAGE_INITIALIZE, f"鉴权失败：HTTP {exc.code}", kind="auth")
        return _result(False, STAGE_START, f"HTTP {exc.code}", kind="start")
    except Exception as exc:  # noqa: BLE001 - network failure
        return _result(False, STAGE_START, f"连接失败：{type(exc).__name__}: {exc}", kind="start")
    if parse_error:
        return _result(False, STAGE_INITIALIZE, f"协议错误：{parse_error}", kind="protocol")
    if message is None:
        return _result(False, STAGE_INITIALIZE, "协议错误：没有响应", kind="protocol")
    if "error" in message:
        detail = _error_text(message) or "initialize 返回错误"
        return _result(False, STAGE_INITIALIZE, f"初始化失败：{detail}", kind=classify_failure(detail))
    if not isinstance(message.get("result"), dict):
        return _result(False, STAGE_INITIALIZE, "协议错误：initialize 未返回 result", kind="protocol")
    server_info = message["result"].get("serverInfo") if isinstance(message["result"].get("serverInfo"), dict) else {}
    protocol = str(message["result"].get("protocolVersion") or "")

    try:
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
              spec, timeout, session)
        tools_message, parse_error, _ = _post(url, _request(2, "tools/list"), spec, timeout, session)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _result(False, STAGE_TOOLS, f"鉴权失败：HTTP {exc.code}", kind="auth")
        return _result(True, STAGE_TOOLS, f"已初始化，但 tools/list 返回 HTTP {exc.code}",
                       serverInfo=server_info, protocolVersion=protocol)
    except Exception as exc:  # noqa: BLE001 - initialized but the follow-up failed
        return _result(True, STAGE_TOOLS, f"已初始化，但 tools/list 失败：{type(exc).__name__}",
                       serverInfo=server_info, protocolVersion=protocol)
    if parse_error or tools_message is None:
        return _result(True, STAGE_TOOLS, "已初始化，但 tools/list 无有效响应",
                       serverInfo=server_info, protocolVersion=protocol)
    if "error" in tools_message:
        return _result(True, STAGE_TOOLS, "已初始化，但 tools/list 返回错误：" + (_error_text(tools_message) or "未知"),
                       serverInfo=server_info, protocolVersion=protocol)
    result = tools_message.get("result") if isinstance(tools_message.get("result"), dict) else {}
    tools = result.get("tools", []) if isinstance(result, dict) else []
    names = [str(item.get("name", "")) for item in tools if isinstance(item, dict)]
    return _result(True, STAGE_READY, f"握手成功，发现 {len(names)} 个工具",
                   tools=names, serverInfo=server_info, protocolVersion=protocol)


def _error_text(message: dict[str, Any]) -> str:
    error = message.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    if isinstance(error, str):
        return error
    return ""
