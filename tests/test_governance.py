from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcpmanager import health, paths, profiles, registry, sync, versioning


class VersionComparisonTests(unittest.TestCase):
    def test_status_matrix(self) -> None:
        cases = [
            ("2.0.0", "1.9.0", versioning.STATUS_CURRENT_HIGHER),
            ("1.0.0", "1.0.0", versioning.STATUS_LATEST),
            ("1.0", "1.0.0", versioning.STATUS_LATEST),
            ("1.0.0", "1.0.0rc1", versioning.STATUS_CURRENT_HIGHER),
            ("1.0.0rc1", "1.0.0", versioning.STATUS_NEWER),
            ("v1.2.3", "1.2.4", versioning.STATUS_NEWER),
            ("1.0.0", "1.0.0.post1", versioning.STATUS_NEWER),
            ("2024.1.1", "2024.1.2", versioning.STATUS_NEWER),
            ("latest", "1.0.0", versioning.STATUS_UNCOMPARABLE),
            ("", "1.0.0", versioning.STATUS_MISSING),
        ]
        for current, latest, expected in cases:
            with self.subTest(current=current, latest=latest):
                self.assertEqual(versioning.compare_versions(current, latest)[0], expected)

    def test_is_newer_does_not_treat_downgrade_as_update(self) -> None:
        self.assertFalse(versioning.is_newer("1.9.0", "2.0.0"))
        self.assertTrue(versioning.is_newer("2.0.0", "1.9.0"))


class ReadOnlyTests(unittest.TestCase):
    def test_registry_save_is_blocked(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_MANAGER_READ_ONLY": "1"}):
            with self.assertRaises(paths.ReadOnlyError):
                registry.save({"servers": {}})

    def test_apply_is_blocked(self) -> None:
        profile = profiles.Profile(id="x", name="X", config=str(Path(tempfile.mkdtemp()) / "x.json"))
        with mock.patch.dict(os.environ, {"MCP_MANAGER_READ_ONLY": "1"}):
            with self.assertRaises(paths.ReadOnlyError):
                sync.apply_section(profile, {})

    def test_custom_update_is_blocked_without_raising(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_MANAGER_READ_ONLY": "1"}):
            ok, detail = versioning.run_custom_update(["python", "-c", "print(1)"])
        self.assertFalse(ok)
        self.assertIn("只读模式", detail)


class ProfileValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, text: str) -> Path:
        path = self.tmp / "agent.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_minimal_profile(self) -> None:
        profile = profiles._profile_from_file(self.write(
            'id = "demo"\nconfig = "{home}/demo.json"\n'
        ))
        self.assertEqual(profile.name, "demo")
        self.assertEqual(profile.config_priority, "primary-first")
        self.assertTrue(profile.auxiliary_optional)

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知字段"):
            profiles._profile_from_file(self.write(
                'id = "demo"\nconfig = "{home}/demo.json"\ntypo = true\n'
            ))

    def test_wrong_bool_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "auxiliary_optional"):
            profiles._profile_from_file(self.write(
                'id = "demo"\nconfig = "{home}/demo.json"\nauxiliary_optional = "yes"\n'
            ))

    def test_unknown_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "未知路径占位符"):
            profiles._profile_from_file(self.write(
                'id = "demo"\nconfig = "{nope}/demo.json"\n'
            ))


class CandidateDiffTests(unittest.TestCase):
    def test_unified_diff_marks_added_server(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        target = tmp / "config.json"
        target.write_text(json.dumps({"mcpServers": {"a": {"command": "node"}}}), encoding="utf-8")
        candidate = tmp / "candidate.json"
        candidate.write_text(json.dumps({
            "agent": "x",
            "targetPath": str(target),
            "format": "json",
            "schema": "mcpServers",
            "servers": {"a": {"command": "node"}, "b": {"command": "uvx"}},
        }), encoding="utf-8")
        diff = sync.candidate_diff(candidate)
        self.assertIn('+  "b": {', diff)
        self.assertNotIn('-  "a"', diff)


class HealthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(health, "HEALTH_PATH", self.tmp / "health.json")
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def test_record_and_read(self) -> None:
        health.record("demo", "server", {"ok": True, "stage": "ready", "detail": "ok", "tools": ["a", "b"]})
        cached = health.get("demo", "server")
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["toolCount"], 2)
        self.assertIn("server", health.summary("demo"))

    def test_read_only_does_not_write(self) -> None:
        with mock.patch.dict(os.environ, {"MCP_MANAGER_READ_ONLY": "1"}):
            health.record("demo", "server", {"ok": True, "stage": "ready", "detail": "", "tools": []})
        self.assertIsNone(health.get("demo", "server"))


if __name__ == "__main__":
    unittest.main()
