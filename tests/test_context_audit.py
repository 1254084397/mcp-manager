from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcpmanager import context_audit
from mcpmanager.model import Profile


class ContextAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.profile = Profile(id="codex", name="Codex CLI", config=str(self.tmp / "config.toml"))

    def test_classify_mcp_routing(self):
        routed = context_audit.classify_mcp_routing(
            {"magpie": {"url": "http://127.0.0.1:3425/mcp/magpie/hub/codex"}}, self.profile
        )
        self.assertEqual(routed["mode"], "routed")
        self.assertTrue(routed["contextSaved"])

        hybrid = context_audit.classify_mcp_routing(
            {
                "magpie": {"url": "http://127.0.0.1:3425/mcp/magpie/hub/codex"},
                "local": {"command": "node", "args": ["x.js"]},
            },
            self.profile,
        )
        self.assertEqual(hybrid["mode"], "hybrid")
        self.assertFalse(hybrid["contextSaved"])

        direct = context_audit.classify_mcp_routing(
            {"a": {"command": "node"}, "b": {"url": "https://example.com"}}, self.profile
        )
        self.assertEqual(direct["mode"], "direct")
        self.assertEqual(direct["directCount"], 2)

    def test_skill_classification_detects_routed_expanded_and_shared_leak(self):
        routed_dir = self.tmp / "claude-skills"
        (routed_dir / "magpie-skills").mkdir(parents=True)
        (routed_dir / "local-one").mkdir(parents=True)
        shared_empty = {"sharedSkillCount": 0}
        res_routed = context_audit._classify_skills("claude-code", routed_dir, shared_empty)
        self.assertEqual(res_routed["mode"], "routed")
        self.assertTrue(res_routed["contextSaved"])

        expanded_dir = self.tmp / "trae-skills"
        for i in range(20):
            (expanded_dir / f"skill-{i}").mkdir(parents=True)
        res_expanded = context_audit._classify_skills("trae", expanded_dir, shared_empty)
        self.assertEqual(res_expanded["mode"], "expanded")
        self.assertFalse(res_expanded["contextSaved"])

        shared_full = {"sharedSkillCount": 103}
        res_leaked = context_audit._classify_skills("codex", routed_dir, shared_full)
        self.assertEqual(res_leaked["mode"], "leaked_shared")
        self.assertFalse(res_leaked["contextSaved"])

    def test_find_duplicate_plugins(self):
        cfg = self.tmp / "opencode.json"
        target = self.tmp / "npm" / "node_modules" / "context-mode"
        target.mkdir(parents=True)
        cfg.write_text(
            json.dumps({"plugin": [str(target), str(target)]}),
            encoding="utf-8",
        )
        dups = context_audit.find_duplicate_plugins(cfg, "json")
        self.assertEqual(len(dups), 1)
        self.assertEqual(len(dups[0]["entries"]), 2)

    def test_collect_context_audit_structure(self):
        cfg = self.tmp / "mcp.json"
        cfg.write_text(
            json.dumps({"mcpServers": {"magpie": {"url": "http://127.0.0.1:3425/mcp/magpie/hub/agy"}}}),
            encoding="utf-8",
        )
        prof = Profile(id="antigravity", name="Antigravity", config=str(cfg))
        with mock.patch.object(context_audit.profiles_mod, "load_profiles", return_value={"antigravity": prof}), \
             mock.patch.object(context_audit.profiles_mod, "detected_profiles", return_value={"antigravity": prof}), \
             mock.patch.object(context_audit, "inspect_shared_skills", return_value={
                 "sharedAgentsDir": str(self.tmp / ".agents" / "skills"),
                 "sharedExists": False,
                 "sharedSkillCount": 0,
                 "sharedHasRouter": False,
                 "magpieLibraryDir": str(self.tmp / "magpie"),
                 "magpieLibraryExists": False,
                 "magpieLibraryCount": 0,
             }):
            report = context_audit.collect_context_audit(include_wsl=False)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(len(report["agents"]), 1)
        self.assertEqual(report["agents"][0]["mcp"]["mode"], "routed")


if __name__ == "__main__":
    unittest.main()
