"""Minimal stdio MCP server used by handshake tests.

Modes are selected with ``FAKE_MODE``:
- ``ok`` (default): initialize + two tools
- ``no-tools``: initialize + empty tools/list
- ``exit-early``: exit before answering
- ``timeout``: read requests but never answer
- ``spawn-child``: start a heartbeat child, then behave like ``ok``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def initialize_result() -> dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "serverInfo": {"name": "fake-stdio", "version": "1.0"},
    }


def spawn_child() -> None:
    heartbeat = os.environ.get("FAKE_HEARTBEAT", "")
    pid_file = os.environ.get("FAKE_CHILD_PID", "")
    code = (
        "import os, time\n"
        "path = os.environ.get('FAKE_HEARTBEAT', '')\n"
        "while path:\n"
        "    with open(path, 'a', encoding='utf-8') as handle:\n"
        "        handle.write(str(time.time()) + '\\n')\n"
        "        handle.flush()\n"
        "    time.sleep(0.2)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], env=os.environ.copy())
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))


def main() -> int:
    mode = os.environ.get("FAKE_MODE", "ok")
    if mode == "exit-early":
        return 1
    if mode == "stderr-auth":
        print("401 Unauthorized: missing token", file=sys.stderr, flush=True)
        return 1
    if mode == "spawn-child":
        spawn_child()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") is None:
            continue
        method = message.get("method")
        if mode == "timeout":
            continue
        if method == "initialize":
            if mode == "protocol-error":
                send({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32600, "message": "invalid request"},
                })
                continue
            send({"jsonrpc": "2.0", "id": message["id"], "result": initialize_result()})
        elif method == "tools/list":
            tools = [] if mode == "no-tools" else [{"name": "ping"}, {"name": "echo"}]
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
