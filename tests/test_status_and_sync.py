from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcpmanager import adapters, cli, discovery, formats, health, paths, profiles, provenance, sync
from mcpmanager.model import Profile


class CodexAndDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_codex_home_override_is_resolved_dynamically(self):
        custom_codex = self.tmp / "custom-codex"
        custom_codex.mkdir(parents=True)
        cfg = custom_codex / "config.toml"
        cfg.write_text('[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8")
        empty_overrides = self.tmp / "no-overrides"

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(custom_codex)}), mock.patch.object(
            paths, "USER_PROFILE_DIR", empty_overrides
        ):
            loaded = profiles.load_profiles(reload=True)
            self.assertEqual(loaded["codex"].path, cfg)
            info = profiles.describe_profile_paths(loaded["codex"])
            self.assertIn("CODEX_HOME", info["recognitionBasis"])

            with mock.patch.object(discovery, "DEFAULT_ROOTS", []), mock.patch.object(
                discovery, "extra_roots", return_value=[]
            ):
                found = discovery.discover_configs([cfg])
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["sourceType"], discovery.LEVEL_EFFECTIVE)
                self.assertEqual(found[0]["servers"], ["demo"])

    def test_discovery_classifies_backup_plugin_market_and_effective(self):
        effective = self.tmp / "live" / "mcp.json"
        backup1 = self.tmp / "backups" / "trae-20261125.json"
        backup2 = self.tmp / "trae-before-restore-20261125.json"
        plugin = self.tmp / "Code" / "agentPlugins" / "foo" / "mcp.json"
        market = self.tmp / "copilot" / "marketplaces" / "bar" / "mcp.json"
        candidate = self.tmp / "generated" / "trae-candidate.json"

        effective_set = {os.path.normcase(str(effective))}
        self.assertEqual(discovery.source_type(effective, effective_set), discovery.LEVEL_EFFECTIVE)
        self.assertEqual(discovery.source_type(backup1, effective_set), discovery.LEVEL_BACKUP)
        self.assertEqual(discovery.source_type(backup2, effective_set), discovery.LEVEL_BACKUP)
        self.assertEqual(discovery.source_type(plugin, effective_set), discovery.LEVEL_PLUGIN)
        self.assertEqual(discovery.source_type(market, effective_set), discovery.LEVEL_MARKET)
        self.assertEqual(discovery.source_type(candidate, effective_set), discovery.LEVEL_CANDIDATE)

    def test_describe_profile_paths_detects_override_and_multiple_candidates(self):
        override_dir = self.tmp / "local" / "agents"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "demo.toml"
        primary = self.tmp / "primary.json"
        aux = self.tmp / "aux.json"
        primary.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        aux.write_text('{"mcpServers": {}}\n', encoding="utf-8")

        profile = Profile(
            id="demo",
            name="Demo",
            config=str(primary),
            configs=[str(aux)],
            source=str(override_file),
        )
        with mock.patch.object(paths, "USER_PROFILE_DIR", override_dir):
            info = profiles.describe_profile_paths(profile)
        self.assertEqual(info["sourceKind"], "override")
        self.assertTrue(info["hasMultipleExisting"])
        self.assertIn("primary-first", info["priorityNote"])

    def test_codex_home_fallback_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            self.assertEqual(paths.codex_home(), paths.HOME / ".codex")
            self.assertEqual(paths.expand_path("{codex_home}/config.toml"), paths.HOME / ".codex" / "config.toml")

    def test_primary_and_auxiliary_detection_combinations(self):
        primary = self.tmp / "primary.json"
        aux = self.tmp / "aux.json"
        profile = Profile(id="multi", name="Multi", config=str(primary), configs=[str(aux)])

        self.assertFalse(profiles.detect_agent(profile)["detected"])

        aux.write_text('{"mcpServers": {"a": {"command": "node"}}}\n', encoding="utf-8")
        det_aux = profiles.detect_agent(profile)
        self.assertTrue(det_aux["detected"])
        self.assertFalse(det_aux["hasMultipleSources"])

        primary.write_text('{"mcpServers": {"b": {"command": "node"}}}\n', encoding="utf-8")
        det_both = profiles.detect_agent(profile)
        self.assertTrue(det_both["detected"])
        self.assertTrue(det_both["hasMultipleSources"])
        self.assertEqual(det_both["mcpCount"], 2)

    def test_plugin_only_still_marks_detected_true_and_reports_duplicate_sources(self):
        plugin_cfg = self.tmp / "plugin-only.json"
        plugin_cfg.write_text(
            json.dumps({"mcpServers": {"host-srv": {"command": "C:/Users/me/.vscode/agentPlugins/x/srv.exe"}}}),
            encoding="utf-8",
        )
        plugin_profile = Profile(id="plug", name="Plug", config=str(plugin_cfg))
        det_plug = profiles.detect_agent(plugin_profile)
        self.assertTrue(det_plug["detected"])
        self.assertTrue(det_plug["configured"])
        self.assertEqual(det_plug["configState"], "plugin_only")

        primary = self.tmp / "p.json"
        aux = self.tmp / "a.json"
        primary.write_text('{"mcpServers": {"shared": {"command": "node", "args": ["p.js"]}}}\n', encoding="utf-8")
        aux.write_text('{"mcpServers": {"shared": {"command": "node", "args": ["a.js"]}, "only_aux": {"command": "node"}}}\n', encoding="utf-8")
        multi = Profile(id="multi-dup", name="MultiDup", config=str(primary), configs=[str(aux)])
        det_multi = profiles.detect_agent(multi)
        self.assertEqual(det_multi["serverSources"]["shared"], str(primary))
        self.assertEqual(det_multi["serverSources"]["only_aux"], str(aux))
        self.assertEqual(len(det_multi["pathDuplicates"]), 1)
        self.assertEqual(det_multi["pathDuplicates"][0]["name"], "shared")
        self.assertEqual(det_multi["pathDuplicates"][0]["effectiveSource"], str(primary))
        self.assertEqual(det_multi["pathDuplicates"][0]["shadowedSources"], [str(aux)])

    def test_detected_profiles_excludes_missing_and_gui_empty_state_safe(self):
        missing = Profile(id="none", name="None", config=str(self.tmp / "missing.json"))
        existing = self.tmp / "ok.json"
        existing.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        ok_prof = Profile(id="ok", name="OK", config=str(existing))
        pool = {"none": missing, "ok": ok_prof}
        detected = profiles.detected_profiles(pool)
        self.assertEqual(list(detected), ["ok"])

        from mcpmanager import gui

        fake_app = gui.App.__new__(gui.App)
        fake_app.profiles = {"none": missing}
        fake_app.detected = {}
        fake_app.agent_id = None
        fake_app.rows = {}
        fake_app._sidebar_rows = {}
        fake_app._agent_meta = {}
        fake_app.tree = mock.MagicMock()
        fake_app.tree.get_children.return_value = []
        fake_app.hint = mock.MagicMock()
        fake_app.status_right = mock.MagicMock()
        fake_app.details = mock.MagicMock()
        fake_app.dark = False
        fake_app.refresh_servers()
        fake_app.status_right.configure.assert_called_with(text="未检测到 Agent")

    def test_theme_callback_skips_and_prunes_destroyed_widgets(self):
        from mcpmanager import gui

        fake_app = gui.App.__new__(gui.App)
        fake_app.dark = False
        fake_app._themed = []
        alive_widget = mock.MagicMock()
        alive_widget.winfo_exists.return_value = True
        dead_widget = mock.MagicMock()
        dead_widget.winfo_exists.return_value = True

        fake_app._theme(alive_widget, bg="sidebar")
        fake_app._theme(dead_widget, bg="sidebar")
        self.assertEqual(len(fake_app._themed), 2)

        dead_widget.winfo_exists.return_value = False
        dead_widget.configure.side_effect = AssertionError("destroyed widget should not be configured")
        fake_app._themed = [apply for apply in fake_app._themed if apply() is not False]
        self.assertEqual(len(fake_app._themed), 1)

    def test_detected_profiles_only_includes_existing_primary_or_auxiliary_configs(self):
        primary = self.tmp / "agent.json"
        auxiliary = self.tmp / "plugin.json"
        primary.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        profiles_to_check = {
            "primary": Profile(id="primary", name="Primary", config=str(primary)),
            "auxiliary": Profile(
                id="auxiliary", name="Auxiliary", config=str(self.tmp / "missing.json"),
                configs=[str(auxiliary)],
            ),
            "missing": Profile(id="missing", name="Missing", config=str(self.tmp / "absent.json")),
        }
        auxiliary.write_text('{"mcpServers": {}}\n', encoding="utf-8")

        self.assertEqual(
            set(profiles.detected_profiles(profiles_to_check)),
            {"primary", "auxiliary"},
        )


class StatusCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ok_cfg = self.tmp / "ok.json"
        self.ok_cfg.write_text(
            json.dumps({"mcpServers": {"a": {"command": "node"}, "b": {"url": "https://example.com"}}}),
            encoding="utf-8",
        )
        self.bad_cfg = self.tmp / "bad.json"
        self.bad_cfg.write_text("{not-valid-json", encoding="utf-8")
        self.perm_cfg = self.tmp / "perm.json"
        self.perm_cfg.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        self.missing_cfg = self.tmp / "missing.json"

        self.fake_profiles = {
            "ok-agent": Profile(id="ok-agent", name="OK Agent", config=str(self.ok_cfg)),
            "bad-agent": Profile(id="bad-agent", name="Bad Agent", config=str(self.bad_cfg)),
            "perm-agent": Profile(id="perm-agent", name="Perm Agent", config=str(self.perm_cfg)),
            "missing-agent": Profile(id="missing-agent", name="Missing Agent", config=str(self.missing_cfg)),
        }
        self.health_data = {
            "ok-agent": {
                "a": {"ok": True, "checkedAt": "2026-11-25T01:00:00"},
            }
        }

    def test_collect_status_handles_parse_permission_missing_and_health(self):
        orig_read = formats.read_merged_section

        def fake_read(path_list, fmt, schema):
            if path_list[0] == self.perm_cfg:
                raise PermissionError("Access is denied")
            return orig_read(path_list, fmt, schema)

        with mock.patch.object(cli, "_profiles", return_value=self.fake_profiles), mock.patch.object(
            health, "load", return_value=self.health_data
        ), mock.patch.object(formats, "read_merged_section", side_effect=fake_read):
            report = cli.collect_status()

        by_id = {row["agent"]: row for row in report["agents"]}
        self.assertEqual(report["schemaVersion"], 2)
        self.assertIn("discoveredConfigs", report)
        self.assertIn("conflicts", report)
        self.assertIn("notes", report)
        self.assertIsNone(by_id["ok-agent"]["agentLoaded"])
        self.assertEqual(by_id["ok-agent"]["configState"], "ok")
        self.assertEqual(by_id["ok-agent"]["mcpCount"], 2)
        self.assertEqual(by_id["ok-agent"]["lastHealth"]["ok"], 1)
        self.assertEqual(by_id["ok-agent"]["lastHealth"]["failed"], 0)
        self.assertEqual(by_id["ok-agent"]["lastHealth"]["unchecked"], 1)
        self.assertEqual(by_id["ok-agent"]["lastHealth"]["lastCheckedAt"], "2026-11-25T01:00:00")
        self.assertFalse(by_id["ok-agent"]["redirected"])
        self.assertTrue(by_id["ok-agent"]["resolvedPath"])

        self.assertEqual(by_id["bad-agent"]["configState"], "parse_error")
        self.assertIsNone(by_id["bad-agent"]["mcpCount"])
        self.assertTrue(by_id["bad-agent"]["configError"])

        self.assertEqual(by_id["perm-agent"]["configState"], "permission_error")
        self.assertIsNone(by_id["perm-agent"]["mcpCount"])

        self.assertEqual(by_id["missing-agent"]["configState"], "missing")
        self.assertFalse(by_id["missing-agent"]["configFileExists"])
        self.assertEqual(by_id["missing-agent"]["mcpCount"], 0)


class SyncProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "mcp.json"
        self.profile = Profile(id="fake", name="Fake", config=str(self.config))
        self.patches = [
            mock.patch.object(sync.paths, "GENERATED_DIR", self.tmp / "generated"),
            mock.patch.object(sync.paths, "BACKUP_DIR", self.tmp / "backups"),
            mock.patch.object(profiles, "load_profiles", lambda reload=False: {"fake": self.profile}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def test_candidate_diff_masks_secrets(self):
        self.config.write_text(
            json.dumps({
                "mcpServers": {
                    "svc": {
                        "command": "node",
                        "env": {
                            "API_KEY": "super-secret-123",
                            "XERJ_AUTH": "Bearer tok_abcdef123456",
                            "NOTE": "github_pat_1234567890abcdef",
                        },
                    },
                }
            }) + "\n",
            encoding="utf-8",
        )
        reg = {"servers": {"svc": {"command": "node", "args": ["new.js"]}}}
        candidate = sync.build_candidate(self.profile, reg)
        diff = sync.candidate_diff(candidate.path)
        self.assertNotIn("super-secret-123", diff)
        self.assertNotIn("tok_abcdef123456", diff)
        self.assertNotIn("github_pat_1234567890abcdef", diff)
        self.assertIn("***", diff)

    def test_host_mcp_is_preserved_in_sync_and_blocked_from_install(self):
        self.config.write_text(
            json.dumps({
                "mcpServers": {
                    "host-plugin": {"command": "C:/Users/me/.vscode/agentPlugins/foo/server.exe"},
                    "custom-local": {"command": "node", "args": ["my.js"]},
                }
            }) + "\n",
            encoding="utf-8",
        )
        reg = {
            "servers": {
                "copilot-only": {
                    "kind": "copilot-plugin-host",
                    "agents": ["fake"],
                }
            }
        }
        candidate = sync.build_candidate(self.profile, reg)
        summary = sync.summarize_candidate(candidate.path)
        # Stale/partial registry must not wipe existing live MCPs
        self.assertIn("custom-local", candidate.servers)
        self.assertIn("host-plugin", candidate.servers)
        skipped_names = {item["name"] for item in summary["skipped"]}
        self.assertIn("host-plugin", skipped_names)
        self.assertIn("copilot-only", skipped_names)

        with self.assertRaises(ValueError):
            sync.install_candidate(self.profile, reg, "copilot-only")


class ProvenanceAndConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "mcp.json"
        self.profile = Profile(id="fake", name="Fake", config=str(self.config))
        self.managed_path = self.tmp / "managed.json"
        self.audit_path = self.tmp / "audit.jsonl"
        self.patches = [
            mock.patch.object(sync.paths, "GENERATED_DIR", self.tmp / "generated"),
            mock.patch.object(sync.paths, "BACKUP_DIR", self.tmp / "backups"),
            mock.patch.object(provenance, "MANAGED_PATH", self.managed_path),
            mock.patch.object(provenance, "AUDIT_PATH", self.audit_path),
            mock.patch.object(profiles, "load_profiles", lambda reload=False: {"fake": self.profile}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def test_source_layer_classification(self):
        host_spec = {"command": "C:/Users/me/.vscode/agentPlugins/x/srv.exe"}
        magpie_spec = {"url": "http://127.0.0.1:8080/magpie/mcp"}
        user_spec = {"command": "node", "args": ["local.js"]}
        self.assertEqual(adapters.source_layer(host_spec, self.profile), adapters.LAYER_HOST)
        self.assertEqual(adapters.source_layer(magpie_spec, self.profile), adapters.LAYER_MAGPIE)
        self.assertEqual(
            adapters.source_layer(user_spec, self.profile, managed_entry={"managedBy": "mcp-manager"}),
            adapters.LAYER_MCP_MANAGER,
        )
        self.assertEqual(adapters.source_layer(user_spec, self.profile), adapters.LAYER_USER)
        self.assertEqual(
            adapters.source_layer(user_spec, self.profile, is_backup_or_cache=True),
            adapters.LAYER_BACKUP_CACHE,
        )

    def test_conflict_prevents_silent_overwrite_of_user_and_magpie_entries(self):
        self.config.write_text(
            json.dumps({
                "mcpServers": {
                    "manual-svc": {"command": "node", "args": ["user-custom.js"]},
                    "magpie-svc": {"url": "http://127.0.0.1:9000/magpie"},
                }
            }) + "\n",
            encoding="utf-8",
        )
        reg = {
            "servers": {
                "manual-svc": {"command": "node", "args": ["registry-version.js"]},
                "magpie-svc": {"command": "npx", "args": ["-y", "magpie-svc"]},
            }
        }
        candidate = sync.build_candidate(self.profile, reg)
        summary = sync.summarize_candidate(candidate.path)
        conflict_names = {item["name"] for item in summary["conflicts"]}
        self.assertEqual(conflict_names, {"manual-svc", "magpie-svc"})
        self.assertEqual(candidate.servers["manual-svc"]["args"], ["user-custom.js"])
        self.assertEqual(candidate.servers["magpie-svc"]["url"], "http://127.0.0.1:9000/magpie")

    def test_apply_install_and_uninstall_updates_managed_markers_and_audit_log(self):
        reg = {
            "servers": {
                "memory": {
                    "kind": "npm-on-demand",
                    "package": "@modelcontextprotocol/server-memory",
                    "versionPolicy": "pin-before-sync",
                    "version": "1.0.0",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-memory"],
                }
            }
        }
        with mock.patch.object(sync.registry, "load", return_value=reg):
            cand_path = sync.install_candidate(self.profile, reg, "memory")
            sync.apply_candidate(cand_path)

        managed = provenance.get_managed("fake", "memory", self.managed_path)
        self.assertIsNotNone(managed)
        self.assertEqual(managed["writtenVersion"], "1.0.0")

        audit_entries = provenance.load_audit(agent="fake", path=self.audit_path)
        self.assertEqual(len(audit_entries), 1)
        self.assertEqual(audit_entries[0]["result"], "ok")
        self.assertEqual(audit_entries[0]["summary"]["added"], ["memory"])

        with mock.patch.object(sync.registry, "load", return_value=reg):
            rm_path = sync.uninstall_candidate(self.profile, "memory")
            sync.apply_candidate(rm_path)

        self.assertIsNone(provenance.get_managed("fake", "memory", self.managed_path))
        audit_entries = provenance.load_audit(agent="fake", path=self.audit_path)
        self.assertEqual(len(audit_entries), 2)
        self.assertEqual(audit_entries[1]["summary"]["removed"], ["memory"])


if __name__ == "__main__":
    unittest.main()
