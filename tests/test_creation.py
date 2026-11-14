from __future__ import annotations

import unittest

from mcpmanager.creation import build_server_spec


class BuildServerSpecTests(unittest.TestCase):
    def build(self, **overrides):
        values = {
            "name": "demo",
            "command": "npx",
            "args_text": "-y @scope/server@1.2.3",
            "url": "",
            "env": {"MODE": "fast", "API_TOKEN": "example-secret"},
            "kind": "npm-on-demand",
            "package": "@scope/server",
            "version": "1.2.3",
            "agent_id": "trae",
        }
        values.update(overrides)
        return build_server_spec(**values)

    def test_local_server_builds_canonical_spec_and_detects_secret_key(self):
        name, spec, secret_keys = self.build()
        self.assertEqual(name, "demo")
        self.assertEqual(spec["command"], "npx")
        self.assertEqual(spec["args"], ["-y", "@scope/server@1.2.3"])
        self.assertEqual(spec["agents"], ["trae"])
        self.assertEqual(spec["versionPolicy"], "pin-before-sync")
        self.assertEqual(secret_keys, ["API_TOKEN"])

    def test_remote_url_builds_http_spec(self):
        name, spec, secrets = self.build(
            command="", args_text="", url="https://example.test/mcp",
            env={}, package="", version="", kind="",
        )
        self.assertEqual(name, "demo")
        self.assertEqual(spec["url"], "https://example.test/mcp")
        self.assertEqual(spec["kind"], "remote-http")
        self.assertFalse(secrets)

    def test_requires_exactly_one_transport(self):
        for command, url in (("", ""), ("node", "https://example.test/mcp")):
            with self.subTest(command=command, url=url), self.assertRaises(ValueError):
                self.build(command=command, url=url)

    def test_rejects_non_http_remote_url(self):
        with self.assertRaisesRegex(ValueError, "http://"):
            self.build(command="", args_text="", url="ftp://example.test/server")

    def test_rejects_empty_or_duplicate_environment_keys(self):
        with self.assertRaisesRegex(ValueError, "名称不能为空"):
            self.build(env={"": "value"})
        with self.assertRaisesRegex(ValueError, "重复"):
            self.build(env={"A": "1", "A ": "2"})

    def test_environment_value_preserves_semicolons_and_equals(self):
        name, spec, _ = self.build(env={"PATH": "C:\\a;C:\\b", "URL": "http://x?a=1;b=2"})
        self.assertEqual(spec["env"]["PATH"], "C:\\a;C:\\b")
        self.assertEqual(spec["env"]["URL"], "http://x?a=1;b=2")

    def test_version_requires_package(self):
        with self.assertRaisesRegex(ValueError, "包名"):
            self.build(package="", version="1.0.0")


if __name__ == "__main__":
    unittest.main()
