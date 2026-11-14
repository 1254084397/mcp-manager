from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mcpmanager import cli, health, paths, sync
from mcpmanager.model import Profile


class HealthCacheEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(health, "HEALTH_PATH", self.tmp / "health.json")
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def test_malformed_cache_is_ignored(self) -> None:
        (self.tmp / "health.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(health.load(), {})
        self.assertEqual(health.summary("demo"), {})

    def test_unknown_agent_returns_empty(self) -> None:
        self.assertIsNone(health.get("missing", "server"))
        self.assertEqual(health.summary("missing"), {})

    def test_record_overwrites_previous_result(self) -> None:
        health.record("demo", "server", {"ok": False, "stage": "initialize", "detail": "timeout", "tools": []})
        health.record("demo", "server", {"ok": True, "stage": "ready", "detail": "ok", "tools": ["ping"]})
        cached = health.get("demo", "server")
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["toolCount"], 1)


class HealthCommandTests(unittest.TestCase):
    def test_json_output(self) -> None:
        data = {"demo": {"server": {"ok": True, "toolCount": 2, "checkedAt": "2026-11-14T22:00:00"}}}
        args = argparse.Namespace(agent=None, json=True)
        with mock.patch.object(cli.health, "load", return_value=data), redirect_stdout(io.StringIO()) as out:
            code = cli._cmd_health(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), data)

    def test_agent_filter(self) -> None:
        data = {"trae": {"a": {}}, "antigravity": {"b": {}}}
        args = argparse.Namespace(agent="antigravity", json=True)
        with mock.patch.object(cli.health, "load", return_value=data), redirect_stdout(io.StringIO()) as out:
            cli._cmd_health(args)
        self.assertEqual(json.loads(out.getvalue()), {"antigravity": {"b": {}}})

    def test_empty_cache_message(self) -> None:
        args = argparse.Namespace(agent=None, json=False)
        with mock.patch.object(cli.health, "load", return_value={}), redirect_stdout(io.StringIO()) as out:
            cli._cmd_health(args)
        self.assertIn("还没有握手缓存", out.getvalue())


class DoctorReportTests(unittest.TestCase):
    def test_json_report_is_written(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        profile = Profile(id="demo", name="Demo", config=str(tmp / "config.json"))
        rows = [{"name": "alpha", "available": True, "detail": "ok", "problems": []}]
        environment = [{"name": "node", "available": True, "path": "C:/node.exe"}]
        args = argparse.Namespace(agent="demo", network=False, env=True, json=True)
        with mock.patch.object(cli, "_profile", return_value=profile), \
                mock.patch.object(cli.doctor, "diagnose", return_value=rows), \
                mock.patch.object(cli.doctor, "environment_report", return_value=environment), \
                mock.patch.object(paths, "REPORT_DIR", tmp), \
                redirect_stdout(io.StringIO()) as out:
            code = cli._cmd_doctor(args)
        self.assertEqual(code, 0)
        target = tmp / "doctor-demo.json"
        self.assertTrue(target.exists())
        report = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(report["agent"], "demo")
        self.assertEqual(report["servers"], rows)
        self.assertEqual(report["environment"], environment)
        self.assertEqual(json.loads(out.getvalue())["agent"], "demo")


class CandidateDiffEdgeTests(unittest.TestCase):
    def write_candidate(self, tmp: Path, target: Path, servers: dict) -> Path:
        path = tmp / "candidate.json"
        path.write_text(json.dumps({
            "agent": "demo",
            "targetPath": str(target),
            "format": "json",
            "schema": "mcpServers",
            "servers": servers,
        }), encoding="utf-8")
        return path

    def test_missing_target_treats_all_servers_as_added(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        candidate = self.write_candidate(tmp, tmp / "config.json", {"alpha": {"command": "node"}})
        diff = sync.candidate_diff(candidate)
        self.assertIn('+  "alpha"', diff)
        self.assertNotIn('-  "alpha"', diff)

    def test_equal_sections_produce_no_diff(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        target = tmp / "config.json"
        target.write_text(json.dumps({"mcpServers": {"alpha": {"command": "node"}}}), encoding="utf-8")
        candidate = self.write_candidate(tmp, target, {"alpha": {"command": "node"}})
        self.assertEqual(sync.candidate_diff(candidate), "")


if __name__ == "__main__":
    unittest.main()
