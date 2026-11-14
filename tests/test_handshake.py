from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcpmanager import handshake, versioning
from mcpmanager.model import Profile

FIXTURES = Path(__file__).parent / "fixtures"
STDIO_SERVER = FIXTURES / "fake_stdio_server.py"
HTTP_SERVER = FIXTURES / "fake_http_server.py"


def kill_pid(pid: int) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class StdioHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = Profile(id="fake", name="Fake", config=str(self.tmp / "missing.json"))

    def spec(self, mode: str, **env: str) -> dict:
        return {
            "command": sys.executable,
            "args": [str(STDIO_SERVER)],
            "env": {"FAKE_MODE": mode, **env},
        }

    def test_initialize_and_tools(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("ok"), timeout=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["stage"], handshake.STAGE_READY)
        self.assertEqual(result["tools"], ["ping", "echo"])
        self.assertEqual(result["serverInfo"]["name"], "fake-stdio")

    def test_no_tools(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("no-tools"), timeout=10)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tools"], [])

    def test_server_exits_early(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("exit-early"), timeout=5)
        self.assertFalse(result["ok"])
        self.assertIn(result["stage"], {handshake.STAGE_START, handshake.STAGE_INITIALIZE})
        self.assertTrue("退出" in result["detail"] or "关闭" in result["detail"], result["detail"])

    def test_initialize_timeout(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("timeout"), timeout=0.5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], handshake.STAGE_INITIALIZE)
        self.assertIn("超时", result["detail"])

    def test_protocol_error(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("protocol-error"), timeout=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], handshake.STAGE_INITIALIZE)
        self.assertIn("初始化失败", result["detail"])
        self.assertEqual(result["kind"], "protocol")

    def test_stderr_auth_is_classified(self) -> None:
        result = handshake.probe(self.profile, "fake", self.spec("stderr-auth"), timeout=5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "auth")
        self.assertIn("401", result["detail"])

    def test_process_tree_is_cleaned_up(self) -> None:
        heartbeat = self.tmp / "heartbeat.txt"
        pid_file = self.tmp / "child.pid"
        result = handshake.probe(
            self.profile,
            "fake",
            self.spec("spawn-child", FAKE_HEARTBEAT=str(heartbeat), FAKE_CHILD_PID=str(pid_file)),
            timeout=10,
        )
        self.assertTrue(result["ok"], result)
        time.sleep(0.5)
        first = heartbeat.read_text(encoding="utf-8") if heartbeat.exists() else ""
        time.sleep(1.0)
        second = heartbeat.read_text(encoding="utf-8") if heartbeat.exists() else ""
        try:
            self.assertEqual(first, second, "子进程仍在运行，进程树未被清理")
        finally:
            if pid_file.exists():
                text = pid_file.read_text(encoding="utf-8").strip()
                if text.isdigit():
                    kill_pid(int(text))


class HttpHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Profile(id="remote", name="Remote", config=str(Path(tempfile.mkdtemp()) / "x.json"))

    def probe(self, mode: str) -> dict:
        process = subprocess.Popen(
            [sys.executable, str(HTTP_SERVER), "--mode", mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            line = process.stdout.readline().strip() if process.stdout else ""
            self.assertTrue(line.startswith("PORT "), line)
            port = int(line.split()[1])
            return handshake.probe(
                self.profile,
                "remote",
                {"url": f"http://127.0.0.1:{port}/mcp"},
                timeout=5,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

    def test_http_initialize_and_tools(self) -> None:
        result = self.probe("ok")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tools"], ["ping", "echo"])
        self.assertEqual(result["serverInfo"]["name"], "fake-http")

    def test_http_sse_response(self) -> None:
        result = self.probe("sse")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tools"], ["ping", "echo"])

    def test_http_401_is_auth_failure(self) -> None:
        result = self.probe("401")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], handshake.STAGE_INITIALIZE)
        self.assertIn("鉴权", result["detail"])

    def test_http_403_is_auth_failure(self) -> None:
        result = self.probe("403")
        self.assertFalse(result["ok"])
        self.assertIn("鉴权", result["detail"])

    def test_http_protocol_error(self) -> None:
        result = self.probe("protocol-error")
        self.assertFalse(result["ok"])
        self.assertIn("初始化失败", result["detail"])
        self.assertEqual(result["kind"], "protocol")

    def test_http_no_tools(self) -> None:
        result = self.probe("no-tools")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tools"], [])


class VersioningCommandTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows 引号与反斜杠规则")
    def test_shell_split_keeps_windows_paths(self) -> None:
        parts = versioning.split_command('git -C "D:\\AI\\my server" pull')
        self.assertEqual(parts[0], "git")
        self.assertEqual(parts[2], "D:\\AI\\my server")

    def test_shell_operators_are_rejected(self) -> None:
        ok, detail = versioning.run_custom_update("echo a && echo b")
        self.assertFalse(ok)
        self.assertIn("shell 操作符", detail)

    def test_array_command_is_supported(self) -> None:
        ok, _detail = versioning.run_custom_update([sys.executable, "-c", "print('ok')"])
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
