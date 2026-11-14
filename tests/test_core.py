from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcpmanager import adapters, formats, profiles, registry, sync
from mcpmanager.model import Profile


class ProfileTests(unittest.TestCase):
    def test_bundled_profiles_load(self):
        data = profiles.load_profiles(reload=True)
        for agent in ("trae", "vscode", "opencode", "copilot-cli", "codex"):
            self.assertIn(agent, data)
        self.assertEqual(data["opencode"].command, "array")
        self.assertEqual(data["opencode"].env, "environment")
        self.assertEqual(data["vscode"].schema, "servers")
        self.assertEqual(data["codex"].format, "toml")
        self.assertTrue(data["trae"].path.is_absolute())

    def test_alias_resolution(self):
        data = profiles.load_profiles(reload=True)
        self.assertEqual(profiles.resolve_profile(data, "copilot").id, "copilot-cli")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.profiles = profiles.load_profiles(reload=True)

    def test_opencode_shape(self):
        spec = {"command": "node", "args": ["server.js"], "env": {"A": "1"}}
        result = adapters.to_agent(spec, self.profiles["opencode"])
        self.assertEqual(result["command"], ["node", "server.js"])
        self.assertNotIn("args", result)
        self.assertEqual(result["environment"], {"A": "1"})
        self.assertEqual(result["type"], "local")
        self.assertTrue(result["enabled"])

    def test_copilot_shape(self):
        spec = {"command": "node", "args": ["server.js"]}
        result = adapters.to_agent(spec, self.profiles["copilot-cli"])
        self.assertEqual(result["command"], "node")
        self.assertEqual(result["type"], "local")
        self.assertEqual(result["tools"], ["*"])

    def test_remote_shape(self):
        result = adapters.to_agent({"url": "https://example.com"}, self.profiles["vscode"])
        self.assertEqual(result["type"], "http")
        self.assertEqual(result["url"], "https://example.com")

    def test_registry_metadata_is_stripped(self):
        spec = {"command": "x", "kind": "npm-global", "agents": ["trae"], "version": "1"}
        result = adapters.to_agent(spec, self.profiles["trae"])
        self.assertNotIn("kind", result)
        self.assertNotIn("agents", result)


class PinTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "servers": {
                "playwright": {
                    "kind": "npm-on-demand",
                    "package": "@playwright/mcp",
                    "versionPolicy": "pin-before-sync",
                    "version": "1.2.3",
                },
                "markitdown": {
                    "kind": "uv-on-demand",
                    "package": "markitdown-mcp",
                    "versionPolicy": "pin-before-sync",
                    "version": "0.0.1a8",
                },
            }
        }

    def test_npm_pin(self):
        spec = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
        result = registry.patch_pins("playwright", spec, self.data)
        self.assertEqual(result["args"], ["-y", "@playwright/mcp@1.2.3"])

    def test_uv_pin_only_after_from(self):
        spec = {"command": "uvx", "args": ["--from", "markitdown-mcp", "markitdown-mcp"]}
        result = registry.patch_pins("markitdown", spec, self.data)
        self.assertEqual(result["args"], ["--from", "markitdown-mcp==0.0.1a8", "markitdown-mcp"])

    def test_unpinned_kind_is_untouched(self):
        spec = {"command": "uvx", "args": ["markitdown-mcp"]}
        data = {"servers": {"markitdown": {"kind": "uv-on-demand", "package": "markitdown-mcp"}}}
        result = registry.patch_pins("markitdown", spec, data)
        self.assertEqual(result["args"], ["markitdown-mcp"])


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_jsonc_block_preserves_comments(self):
        path = self.tmp / "opencode.json"
        path.write_text(
            '{\n  // keep me\n  "theme": "dark",\n  "mcp": {\n    "old": {"command": "x"}\n  }\n}\n',
            encoding="utf-8",
        )
        mode = formats.write_section(path, "jsonc", "mcp", {"new": {"command": "y"}})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(mode, "preserved")
        self.assertIn("// keep me", text)
        self.assertEqual(json.loads(formats.strip_jsonc(text))["mcp"], {"new": {"command": "y"}})

    def test_json_missing_key_appends_without_dropping_comments(self):
        path = self.tmp / "config.jsonc"
        path.write_text('{\n  // note\n  "servers": {"a": {"command": "x"}}\n}\n', encoding="utf-8")
        mode = formats.write_section(path, "jsonc", "mcp", {"b": {"command": "y"}})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(mode, "appended")
        self.assertIn("// note", text)
        data = json.loads(formats.strip_jsonc(text))
        self.assertEqual(data["servers"], {"a": {"command": "x"}})
        self.assertEqual(data["mcp"], {"b": {"command": "y"}})

    def test_toml_replaces_only_mcp_tables(self):
        path = self.tmp / "config.toml"
        path.write_text(
            'model = "gpt"\n\n'
            "[mcp_servers.old]\ncommand = \"a\"\n\n"
            "# unrelated table\n[other]\nkey = 1\n",
            encoding="utf-8",
        )
        mode = formats.write_section(path, "toml", "mcp_servers", {"new": {"command": "b", "args": ["x"]}})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(mode, "preserved")
        self.assertIn('model = "gpt"', text)
        self.assertIn("[other]\nkey = 1", text)
        self.assertIn("[mcp_servers.new]", text)
        self.assertNotIn("mcp_servers.old", text)
        self.assertIn('command = "b"', text)

    def test_toml_missing_section_is_appended(self):
        path = self.tmp / "config.toml"
        path.write_text('model = "gpt"\n', encoding="utf-8")
        formats.write_section(path, "toml", "mcp_servers", {"s": {"command": "c"}})
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "gpt")
        self.assertEqual(data["mcp_servers"]["s"]["command"], "c")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = self.tmp / "mcp.json"
        self.config.write_text(
            json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}}}) + "\n",
            encoding="utf-8",
        )
        self.profile = Profile(id="fake", name="Fake", config=str(self.config))
        self.registry_data = {
            "servers": {
                "playwright": {
                    "kind": "npm-on-demand",
                    "package": "@playwright/mcp",
                    "versionPolicy": "pin-before-sync",
                    "version": "9.9.9",
                }
            }
        }
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

    def test_build_and_apply_roundtrip(self):
        candidate = sync.build_candidate(self.profile, self.registry_data)
        self.assertEqual(candidate.servers["playwright"]["args"], ["-y", "@playwright/mcp@9.9.9"])
        backup, mode = sync.apply_candidate(candidate.path)
        self.assertEqual(mode, "preserved")
        self.assertTrue(backup.exists())
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["mcpServers"]["playwright"]["args"], ["-y", "@playwright/mcp@9.9.9"])

    def test_stale_candidate_is_rejected(self):
        candidate = sync.build_candidate(self.profile, self.registry_data)
        self.config.write_text('{"mcpServers": {}}\n', encoding="utf-8")
        with self.assertRaises(formats.ConfigError):
            sync.apply_candidate(candidate.path)

    def test_inline_secret_blocks_candidate(self):
        self.config.write_text(
            json.dumps({"mcpServers": {"bad": {"command": "x", "env": {"API_KEY": "secret"}}}}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(sync.SecretError):
            sync.build_candidate(self.profile, {"servers": {}})

    def test_install_candidate_can_create_missing_config(self):
        missing = self.tmp / "new.json"
        profile = Profile(id="new", name="New", config=str(missing))
        data = {"servers": {"memory": {"command": "npx", "args": ["-y", "memory-server"]}}}
        with mock.patch.object(profiles, "load_profiles", lambda reload=False: {"new": profile}):
            path = sync.install_candidate(profile, data, "memory")
            backup, mode = sync.apply_candidate(path)
        self.assertIsNone(backup)
        self.assertEqual(mode, "created")
        self.assertEqual(json.loads(missing.read_text(encoding="utf-8"))["mcpServers"]["memory"]["command"], "npx")

    def test_attach_agent_and_install_appends_agent_and_generates_candidate(self):
        data = {
            "servers": {
                "memory": {
                    "command": "npx",
                    "args": ["-y", "memory-server"],
                    "agents": ["other"],
                }
            }
        }
        candidate, modified = sync.attach_agent_and_install(self.profile, data, "memory")
        self.assertTrue(modified)
        self.assertIn("fake", data["servers"]["memory"]["agents"])
        self.assertIn("other", data["servers"]["memory"]["agents"])
        self.assertTrue(candidate.exists())

    def test_attach_agent_and_install_skips_when_agent_already_listed(self):
        data = {
            "servers": {
                "memory": {
                    "command": "npx",
                    "args": ["-y", "memory-server"],
                    "agents": ["fake"],
                }
            }
        }
        candidate, modified = sync.attach_agent_and_install(self.profile, data, "memory")
        self.assertFalse(modified)
        self.assertEqual(data["servers"]["memory"]["agents"], ["fake"])
        self.assertTrue(candidate.exists())

    def test_attach_agent_and_install_raises_when_missing(self):
        with self.assertRaises(KeyError):
            sync.attach_agent_and_install(self.profile, {"servers": {}}, "missing")


if __name__ == "__main__":
    unittest.main()
