from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mcpmanager import cli, health, paths, profiles, sync
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


class InstallCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "mcp.json"
        self.profile = Profile(id="demo", name="Demo", config=str(self.config))
        self.saved: list[dict] = []
        self.patches = [
            mock.patch.object(cli, "_load_catalog",
                              lambda: {"demo-mcp": {"command": "node", "args": ["x.js"]}}),
            mock.patch.object(cli, "_resolve_targets", lambda _agents: [self.profile]),
            mock.patch.object(cli.registry, "load", return_value={"servers": {}}),
            mock.patch.object(cli.registry, "save", side_effect=self.saved.append),
            mock.patch.object(profiles, "load_profiles", lambda reload=False: {"demo": self.profile}),
            mock.patch.object(sync.paths, "GENERATED_DIR", self.tmp / "generated"),
            mock.patch.object(sync.paths, "BACKUP_DIR", self.tmp / "backups"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()

    def args(self, apply: bool, yes: bool) -> argparse.Namespace:
        return argparse.Namespace(name="demo-mcp", agent=None, apply=apply, yes=yes)

    def test_apply_declined_leaves_registry_and_config_untouched(self) -> None:
        with mock.patch.object(cli, "_confirm", return_value=False), redirect_stdout(io.StringIO()):
            code = cli._cmd_install(self.args(apply=True, yes=False))
        self.assertEqual(code, 0)
        self.assertEqual(self.saved, [])
        self.assertFalse(self.config.exists())

    def test_apply_confirmed_saves_registry_and_writes_config(self) -> None:
        with mock.patch.object(cli, "_confirm", return_value=True), redirect_stdout(io.StringIO()) as out:
            code = cli._cmd_install(self.args(apply=True, yes=False))
        self.assertEqual(code, 0)
        self.assertIn("demo-mcp", self.saved[0]["servers"])
        self.assertIn("将写入的差异", out.getvalue())
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["mcpServers"]["demo-mcp"]["command"], "node")

    def test_apply_yes_skips_prompt_and_diff(self) -> None:
        with mock.patch.object(cli, "_confirm") as confirm, redirect_stdout(io.StringIO()) as out:
            code = cli._cmd_install(self.args(apply=True, yes=True))
        self.assertEqual(code, 0)
        confirm.assert_not_called()
        self.assertNotIn("将写入的差异", out.getvalue())
        self.assertIn("demo-mcp", self.saved[0]["servers"])
        self.assertTrue(self.config.exists())

    def test_without_apply_registers_and_only_builds_candidate(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            code = cli._cmd_install(self.args(apply=False, yes=False))
        self.assertEqual(code, 0)
        self.assertIn("demo-mcp", self.saved[0]["servers"])
        self.assertFalse(self.config.exists())
        self.assertIn("候选", out.getvalue())


if __name__ == "__main__":
    unittest.main()
