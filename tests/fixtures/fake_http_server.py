"""Minimal HTTP MCP server used by handshake tests.

Run as a script; it prints ``PORT <number>`` on the first line. Modes:
- ``ok``: JSON responses with an ``Mcp-Session-Id`` header
- ``sse``: ``text/event-stream`` responses
- ``401``: reject initialize with HTTP 401
- ``no-tools``: initialize succeeds, tools/list is empty
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *_args) -> None:  # keep test output clean
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: dict, session: bool = False) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if session:
            self.send_header("Mcp-Session-Id", "test-session")
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, payload: dict) -> None:
        data = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        message = self._body()
        if self.mode in {"401", "403"}:
            self.send_response(401 if self.mode == "401" else 403)
            self.end_headers()
            return
        if message.get("id") is None:
            self.send_response(202)
            self.end_headers()
            return
        method = message.get("method")
        if method == "initialize" and self.mode == "protocol-error":
            self._send_json({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32600, "message": "invalid request"},
            })
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-http", "version": "1.0"},
            }
        elif method == "tools/list":
            tools = [] if self.mode == "no-tools" else [{"name": "ping"}, {"name": "echo"}]
            result = {"tools": tools}
        else:
            self._send_json({
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "unknown method"},
            })
            return
        payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if self.mode == "sse":
            self._send_sse(payload)
        else:
            self._send_json(payload, session=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    Handler.mode = args.mode
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PORT {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
