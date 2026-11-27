from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcpmanager import adapters, magpie, paths, provenance, skills, sync, targets
from mcpmanager.model import Profile


class MultiTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_wsl_target_auto_detects_non_hardcoded_user_and_home(self):
        wsl_out = "USER=alice\nHOME=/home/alice\nOS=Linux 6.6\nTOOL_node=1\nTOOL_npm=1\nTOOL_uvx=0\nTOOL_python3=1\nTOOL_git=1\n"
        with mock.patch.object(targets, "run_wsl_bash", return_value=(0, wsl_out, "")):
            t = targets._probe_wsl_target("Ubuntu-24.04")
        self.assertTrue(t.online)
        self.assertEqual(t.user, "alice")
        self.assertEqual(t.home, "/home/alice")
        self.assertTrue(t.tools["node"])
        self.assertFalse(t.tools["uvx"])

    def test_detect_target_agents_distinguishes_windows_and_wsl(self):
        wsl_home = self.tmp / "wsl_home" / "alice"
        (wsl_home / ".codex").mkdir(parents=True)
        (wsl_home / ".codex" / "config.toml").write_text('[mcp_servers.x]\ncommand = "node"\n', encoding="utf-8")
        (wsl_home / ".pi" / "agent").mkdir(parents=True)
        (wsl_home / ".pi" / "agent" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        (wsl_home / ".gemini" / "config").mkdir(parents=True)
        (wsl_home / ".gemini" / "config" / "mcp_config.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

        fake_wsl = targets.Target(
            id="wsl:Ubuntu-24.04",
            name="WSL Ubuntu-24.04",
            kind="wsl",
            online=True,
            os_name="Linux",
            user="alice",
            home="/home/alice",
            distro="Ubuntu-24.04",
        )

        def fake_unc(_distro: str, posix_path: str) -> Path:
            if posix_path == "/home/alice":
                return wsl_home
            rel = posix_path.removeprefix("/home/alice/")
            return wsl_home.joinpath(*rel.split("/")) if rel else wsl_home

        with mock.patch.object(targets, "wsl_unc_path", side_effect=fake_unc):
            agents = targets.detect_target_agents(fake_wsl)

        qids = {item["qualifiedId"] for item in agents}
        self.assertEqual(
            qids,
            {
                "wsl:Ubuntu-24.04/codex",
                "wsl:Ubuntu-24.04/pi",
                "wsl:Ubuntu-24.04/antigravity",
            },
        )

    def test_detect_unknown_wsl_agent_and_ignores_caches_and_backups(self):
        wsl_home = self.tmp / "wsl_home_unknown" / "alice"
        (wsl_home / ".some-agent" / "skills" / "s1").mkdir(parents=True)
        (wsl_home / ".some-agent" / "mcp.json").write_text(
            '{"mcpServers": {"demo": {"command": "node"}}}\n', encoding="utf-8"
        )
        # Should be ignored: cache / backup / marketplace / non-MCP dir
        (wsl_home / ".cache" / "mcp.json").parent.mkdir(parents=True)
        (wsl_home / ".cache" / "mcp.json").write_text('{"mcpServers": {"x": {"command": "x"}}}\n', encoding="utf-8")
        (wsl_home / ".backups-test").mkdir(parents=True)
        (wsl_home / ".some-agent" / "mcp.json.bak").write_text('{"mcpServers": {}}\n', encoding="utf-8")

        fake_wsl = targets.Target(
            id="wsl:Ubuntu-24.04",
            name="WSL Ubuntu-24.04",
            kind="wsl",
            online=True,
            os_name="Linux",
            user="alice",
            home="/home/alice",
            distro="Ubuntu-24.04",
        )

        def fake_unc(_distro: str, posix_path: str) -> Path:
            if posix_path == "/home/alice":
                return wsl_home
            rel = posix_path.removeprefix("/home/alice/")
            return wsl_home.joinpath(*rel.split("/")) if rel else wsl_home

        with mock.patch.object(targets, "wsl_unc_path", side_effect=fake_unc):
            agents = targets.detect_target_agents(fake_wsl)

        self.assertEqual(len(agents), 1)
        unknown = agents[0]
        self.assertTrue(unknown["agent"].startswith("discovered-"))
        self.assertTrue(unknown["readonly"])
        self.assertTrue(unknown["discovered"])
        self.assertEqual(unknown["mcpCount"], 1)
        self.assertEqual(unknown["skillsCount"], 1)
        self.assertEqual(unknown["configState"], "unknown_readonly")

    def test_readonly_unknown_agent_blocked_from_skills_and_mcp_sync(self):
        wsl_home = self.tmp / "wsl_home_ro" / "alice"
        (wsl_home / ".pi" / "agent" / "skills" / "s1").mkdir(parents=True)
        (wsl_home / ".pi" / "agent" / "skills" / "s1" / "SKILL.md").write_text("# s1", encoding="utf-8")
        (wsl_home / ".pi" / "agent" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
        (wsl_home / ".custom-agent" / "skills" / "s2").mkdir(parents=True)
        (wsl_home / ".custom-agent" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

        fake_wsl = targets.Target(
            id="wsl:Ubuntu-24.04",
            name="WSL Ubuntu-24.04",
            kind="wsl",
            online=True,
            os_name="Linux",
            user="alice",
            home="/home/alice",
            distro="Ubuntu-24.04",
        )

        def fake_unc(_distro: str, posix_path: str) -> Path:
            if posix_path == "/home/alice":
                return wsl_home
            rel = posix_path.removeprefix("/home/alice/")
            return wsl_home.joinpath(*rel.split("/")) if rel else wsl_home

        with mock.patch.object(targets, "wsl_unc_path", side_effect=fake_unc), \
             mock.patch.object(targets, "resolve_target", return_value=fake_wsl):
            agents = targets.detect_target_agents(fake_wsl)
            unknown_id = next(a["agent"] for a in agents if a.get("discovered"))
            with self.assertRaises(ValueError):
                skills.plan_skills_sync("wsl:Ubuntu-24.04", "pi", unknown_id, mode="copy")
            with self.assertRaises(ValueError):
                skills.plan_skills_sync("wsl:Ubuntu-24.04", unknown_id, "pi", mode="copy")
            with self.assertRaises(ValueError):
                sync.build_target_candidate("wsl:Ubuntu-24.04", unknown_id, {"servers": {}})

    def test_windows_unknown_agent_auto_discovered_and_ignores_backups(self):
        win_home = self.tmp / "win_home"
        appdata = self.tmp / "appdata"
        localappdata = self.tmp / "localappdata"
        (win_home / ".mystery-agent" / "skills" / "k1").mkdir(parents=True)
        (win_home / ".mystery-agent" / "mcp.json").write_text(
            '{"mcpServers": {"m1": {"command": "node"}}}\n', encoding="utf-8"
        )
        (appdata / "OtherAgent").mkdir(parents=True)
        (appdata / "OtherAgent" / "mcp_config.json").write_text(
            '{"servers": {"m2": {"command": "python"}}}\n', encoding="utf-8"
        )
        # Ignored folders
        (appdata / "backups" / "mcp.json").parent.mkdir(parents=True)
        (appdata / "backups" / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")

        fake_win = targets.Target(
            id="windows", name="Windows 本机", kind="windows", online=True,
            os_name="Windows", user="u", home=str(win_home),
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)}):
            disc = targets._discover_unknown_windows_agents(fake_win, set(), set())

        names = {d["name"] for d in disc}
        self.assertIn("未知 Agent (mystery-agent)", names)
        self.assertIn("未知 Agent (OtherAgent)", names)
        self.assertTrue(all(d["readonly"] and d["discovered"] for d in disc))
        self.assertFalse(any("backups" in d["name"].lower() for d in disc))

    def test_trae_auxiliary_dirs_excluded_from_unknown_discovery(self):
        from mcpmanager import profiles as profiles_mod

        pool = profiles_mod.load_profiles(reload=True)
        self.assertEqual(len(pool["trae"].paths), 2)
        self.assertEqual(len(pool["trae-solo"].paths), 2)

        win_home = self.tmp / "win_home_trae"
        appdata = self.tmp / "appdata_trae"
        (win_home / ".trae-cn").mkdir(parents=True)
        (win_home / ".trae-cn" / "mcp.json").write_text('{"mcpServers": {"p": {"command": "node"}}}\n', encoding="utf-8")
        (win_home / ".trae-aicc").mkdir(parents=True)
        (win_home / ".trae-aicc" / "mcp.json").write_text('{"mcpServers": {"q": {"command": "node"}}}\n', encoding="utf-8")
        (appdata / "TRAE SOLO CN" / "skills" / "s1").mkdir(parents=True)

        fake_win = targets.Target(
            id="windows", name="Windows 本机", kind="windows", online=True,
            os_name="Windows", user="u", home=str(win_home),
        )
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata), "LOCALAPPDATA": str(self.tmp / "empty_local")}):
            disc = targets._discover_unknown_windows_agents(fake_win, set(), set())
        self.assertEqual(disc, [])

    def test_identity_classification_routing_and_effective_skills(self):
        # Lone config file without exe/runtime -> residual
        state, _conf, ev, is_res = targets._classify_agent_identity(
            qualified_id="windows/claude-code",
            has_exe=False,
            has_runtime=False,
            has_config=True,
            has_skills=False,
            has_magpie_applied=False,
            mcp_count=0,
            overrides={},
        )
        self.assertEqual(state, "residual")
        self.assertTrue(is_res)
        self.assertEqual(ev, ["config_file"])

        # User override confirmed/ignored
        id_file = self.tmp / "agent-identity.json"
        targets.set_agent_identity_override("windows/claude-code", "confirmed", path=id_file)
        state2, conf2, _, is_res2 = targets._classify_agent_identity(
            qualified_id="windows/claude-code",
            has_exe=False,
            has_runtime=False,
            has_config=True,
            has_skills=False,
            has_magpie_applied=False,
            mcp_count=0,
            overrides=targets.load_identity_overrides(id_file),
        )
        self.assertEqual(state2, "confirmed")
        self.assertEqual(conf2, 1.0)
        self.assertFalse(is_res2)

        # Routing status: direct vs hybrid vs routed
        self.assertEqual(targets._routing_status({"laya": {"command": "py"}}), ("direct", False))
        self.assertEqual(
            targets._routing_status({"magpie": {"url": "http://127.0.0.1:3425/mcp"}, "laya": {"command": "py"}}),
            ("hybrid", True),
        )
        self.assertEqual(targets._routing_status({"magpie": {"url": "http://127.0.0.1:3425/mcp"}}), ("routed", True))

        # Windows Pi profile exists
        from mcpmanager import profiles as profiles_mod
        pool = profiles_mod.load_profiles(reload=True)
        self.assertIn("pi", pool)
        self.assertTrue(pool["pi"].shared_skills_reader)


class PathIsolationAndRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_cross_platform_path_violations(self):
        for bad_win in (
            {"command": "node", "args": [r"C:\tools\s.js"]},
            {"command": r"D:\AI\laya\.venv\Scripts\python.exe"},
            {"command": r"E:\CodexHome\srv.exe"},
            {"command": "node", "args": [r"\Users\me\app.js"]},
            {"command": r"C:\Program Files\nodejs\node.exe"},
        ):
            self.assertIsNotNone(adapters.target_path_violation(bad_win, "wsl"))

        bad_posix = {"command": "/home/alice/.local/bin/srv"}
        self.assertIsNotNone(adapters.target_path_violation(bad_posix, "windows"))
        self.assertIsNone(adapters.target_path_violation({"url": "https://mcp.grep.app"}, "wsl"))
        self.assertIsNone(adapters.target_path_violation({"url": "https://mcp.grep.app"}, "windows"))

    def test_target_registry_resolution_isolates_windows_and_wsl(self):
        defn = {
            "kind": "npm-on-demand",
            "targets": {
                "windows": {"command": "node", "args": ["D:/CLI/npm/cli.js"]},
                "wsl": {"command": "/usr/bin/node", "args": ["/home/alice/.local/cli.js"]},
            },
        }
        win_spec = adapters.resolve_target_spec(defn, "windows")
        wsl_spec = adapters.resolve_target_spec(defn, "wsl")
        self.assertEqual(win_spec["args"], ["D:/CLI/npm/cli.js"])
        self.assertEqual(wsl_spec["args"], ["/home/alice/.local/cli.js"])

    def test_offline_target_blocks_sync_and_windows_paths_skipped_in_wsl(self):
        offline = targets.Target(
            id="wsl:Ubuntu-24.04", name="WSL", kind="wsl", online=False, os_name="Linux", user="", home="", distro="Ubuntu-24.04"
        )
        with mock.patch.object(targets, "resolve_target", return_value=offline):
            with self.assertRaises(RuntimeError):
                sync.build_target_candidate("wsl:Ubuntu-24.04", "codex", {"servers": {}})

        online = targets.Target(
            id="wsl:Ubuntu-24.04", name="WSL", kind="wsl", online=True, os_name="Linux", user="alice", home="/home/alice", distro="Ubuntu-24.04"
        )
        unc_cfg = self.tmp / "config.toml"
        unc_cfg.write_text('[mcp_servers.win_bad]\ncommand = "D:/AI/laya/srv.exe"\n', encoding="utf-8")
        fake_agents = [{
            "target": "wsl:Ubuntu-24.04",
            "agent": "codex",
            "name": "Codex CLI",
            "configPath": "/home/alice/.codex/config.toml",
            "uncConfigPath": str(unc_cfg),
            "format": "toml",
            "schema": "mcp_servers",
        }]
        reg = {"servers": {"win_bad": {"command": "D:/AI/laya/srv.exe"}}}
        with mock.patch.object(targets, "resolve_target", return_value=online), \
             mock.patch.object(targets, "detect_target_agents", return_value=fake_agents), \
             mock.patch.object(sync.paths, "GENERATED_DIR", self.tmp / "generated"):
            cand_path = sync.build_target_candidate("wsl:Ubuntu-24.04", "codex", reg)
            cand_unreg = sync.build_target_candidate("wsl:Ubuntu-24.04", "codex", {"servers": {}})
        cand = sync.load_candidate(cand_path)
        self.assertTrue(any(item["name"] == "win_bad" for item in cand["summary"]["skipped"]))
        unreg = sync.load_candidate(cand_unreg)
        self.assertTrue(any(item["name"] == "win_bad" for item in unreg["summary"]["skipped"]))

    def test_rollback_restores_backup_from_audit(self):
        cfg = self.tmp / "mcp.json"
        cfg.write_text('{"mcpServers": {"old": {"command": "node"}}}\n', encoding="utf-8")
        prof = Profile(id="fake", name="Fake", config=str(cfg))
        audit_path = self.tmp / "audit.jsonl"
        managed_path = self.tmp / "managed.json"
        reg = {"servers": {"added_srv": {"command": "npx", "args": ["-y", "x"]}}}

        with mock.patch.object(sync.paths, "GENERATED_DIR", self.tmp / "generated"), \
             mock.patch.object(sync.paths, "BACKUP_DIR", self.tmp / "backups"), \
             mock.patch.object(provenance, "AUDIT_PATH", audit_path), \
             mock.patch.object(provenance, "MANAGED_PATH", managed_path), \
             mock.patch.object(sync.profiles_mod, "load_profiles", return_value={"fake": prof}), \
             mock.patch.object(sync.registry, "load", return_value=reg):
            cand = sync.install_candidate(prof, reg, "added_srv")
            sync.apply_candidate(cand)
            self.assertIn("added_srv", json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"])

            used, safety = sync.rollback_from_audit("windows", "fake")
            self.assertTrue(used.exists())
            self.assertIsNotNone(safety)
            restored = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
            self.assertIn("old", restored)
            self.assertNotIn("added_srv", restored)


class SkillsAndMagpieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_skills_copy_mode_preserves_system_and_records_hashes(self):
        pi_skills = self.tmp / "pi_skills"
        codex_skills = self.tmp / "codex_skills"
        (pi_skills / "context-mode").mkdir(parents=True)
        (pi_skills / "context-mode" / "SKILL.md").write_text("# cm\n", encoding="utf-8")
        (codex_skills / ".system").mkdir(parents=True)
        (codex_skills / ".system" / "sys.txt").write_text("keep", encoding="utf-8")

        fake_win = targets.Target(
            id="windows", name="Win", kind="windows", online=True, os_name="Windows", user="me", home=str(self.tmp)
        )
        fake_agents = [
            {"agent": "pi", "skillsPath": str(pi_skills)},
            {"agent": "codex", "skillsPath": str(codex_skills)},
        ]
        state_file = self.tmp / "skills-sync.json"
        audit_file = self.tmp / "audit.jsonl"
        backup_dir = self.tmp / "backups"
        with mock.patch.object(targets, "resolve_target", return_value=fake_win), \
             mock.patch.object(targets, "detect_target_agents", return_value=fake_agents), \
             mock.patch.object(skills, "SKILLS_STATE_PATH", state_file), \
             mock.patch.object(provenance, "AUDIT_PATH", audit_file), \
             mock.patch.object(skills.paths, "BACKUP_DIR", backup_dir):
            plan = skills.plan_skills_sync("windows", "pi", "codex", mode="copy")
            self.assertEqual(plan["added"], ["context-mode"])
            self.assertEqual(plan["preserved"], [".system"])
            self.assertFalse(plan["autoSyncsOnEdit"])
            res = skills.apply_skills_sync(plan)
            self.assertIsNotNone(res["backup"])

            # Owned skill modified only on source -> copy mode updates it, backs up, and applies
            (pi_skills / "context-mode" / "SKILL.md").write_text("# cm v2\n", encoding="utf-8")
            plan2 = skills.plan_skills_sync("windows", "pi", "codex", mode="copy")
            self.assertEqual(plan2["updated"], ["context-mode"])
            skills.apply_skills_sync(plan2)
            self.assertEqual((codex_skills / "context-mode" / "SKILL.md").read_text(encoding="utf-8"), "# cm v2\n")

            # Destination copy manually edited by user -> conflict, must not overwrite
            (codex_skills / "context-mode" / "SKILL.md").write_text("# user edit\n", encoding="utf-8")
            (pi_skills / "context-mode" / "SKILL.md").write_text("# cm v3\n", encoding="utf-8")
            plan3 = skills.plan_skills_sync("windows", "pi", "codex", mode="copy")
            self.assertEqual(len(plan3["conflicts"]), 1)
            self.assertEqual(plan3["updated"], [])

            # Rollback restores v1
            sync.rollback_from_audit("windows", "codex")
            self.assertEqual((codex_skills / "context-mode" / "SKILL.md").read_text(encoding="utf-8"), "# cm\n")
            self.assertTrue((codex_skills / ".system" / "sys.txt").is_file())
            self.assertTrue(state_file.is_file())

    def test_shared_directory_handles_already_shared_and_blocks_non_empty_dest(self):
        pi_skills = self.tmp / "pi_skills"
        codex_skills = self.tmp / "codex_skills"
        (pi_skills / "s1").mkdir(parents=True)
        (codex_skills / "user_custom").mkdir(parents=True)

        fake_win = targets.Target(
            id="windows", name="Win", kind="windows", online=True, os_name="Windows", user="me", home=str(self.tmp)
        )
        fake_agents = [
            {"agent": "pi", "skillsPath": str(pi_skills)},
            {"agent": "antigravity", "skillsPath": str(pi_skills)},
            {"agent": "codex", "skillsPath": str(codex_skills)},
        ]
        with mock.patch.object(targets, "resolve_target", return_value=fake_win), \
             mock.patch.object(targets, "detect_target_agents", return_value=fake_agents):
            same_plan = skills.plan_skills_sync("windows", "pi", "antigravity", mode="shared-directory")
            self.assertTrue(same_plan["alreadyShared"])
            self.assertTrue(same_plan["alreadySynced"])
            res = skills.apply_skills_sync(same_plan)
            self.assertFalse(res["applied"])
            with self.assertRaises(ValueError):
                skills.plan_skills_sync("windows", "pi", "codex", mode="shared-directory")

            # With replace=True, backs up non-empty dir and replaces with symlink
            state_file = self.tmp / "skills-sync.json"
            audit_file = self.tmp / "audit.jsonl"
            backup_dir = self.tmp / "backups"
            with mock.patch.object(skills, "SKILLS_STATE_PATH", state_file), \
                 mock.patch.object(provenance, "AUDIT_PATH", audit_file), \
                 mock.patch.object(skills.paths, "BACKUP_DIR", backup_dir):
                rep_plan = skills.plan_skills_sync("windows", "pi", "codex", mode="shared-directory", replace=True)
                self.assertFalse(rep_plan["alreadySynced"])
                rep_res = skills.apply_skills_sync(rep_plan)
                self.assertTrue(rep_res["applied"])
                self.assertIsNotNone(rep_res["backup"])
                self.assertTrue(skills._is_link_or_junction(codex_skills))
                self.assertTrue((Path(rep_res["backup"]) / "user_custom").is_dir())

    def test_symlink_per_entry_switch_to_copy_unlinks_symlink(self):
        pi_skills = self.tmp / "pi_skills"
        codex_skills = self.tmp / "codex_skills"
        (pi_skills / "cm").mkdir(parents=True)
        (pi_skills / "cm" / "SKILL.md").write_text("# cm\n", encoding="utf-8")
        codex_skills.mkdir(parents=True)

        fake_win = targets.Target(
            id="windows", name="Win", kind="windows", online=True, os_name="Windows", user="me", home=str(self.tmp)
        )
        fake_agents = [
            {"agent": "pi", "skillsPath": str(pi_skills)},
            {"agent": "codex", "skillsPath": str(codex_skills)},
        ]
        state_file = self.tmp / "skills-sync.json"
        audit_file = self.tmp / "audit.jsonl"
        backup_dir = self.tmp / "backups"
        with mock.patch.object(targets, "resolve_target", return_value=fake_win), \
             mock.patch.object(targets, "detect_target_agents", return_value=fake_agents), \
             mock.patch.object(skills, "SKILLS_STATE_PATH", state_file), \
             mock.patch.object(provenance, "AUDIT_PATH", audit_file), \
             mock.patch.object(skills.paths, "BACKUP_DIR", backup_dir):
            link_plan = skills.plan_skills_sync("windows", "pi", "codex", mode="symlink-per-entry")
            skills.apply_skills_sync(link_plan)
            self.assertTrue(skills._is_link_or_junction(codex_skills / "cm"))

            copy_plan = skills.plan_skills_sync("windows", "pi", "codex", mode="copy")
            self.assertEqual(copy_plan["updated"], ["cm"])
            skills.apply_skills_sync(copy_plan)
            self.assertFalse(skills._is_link_or_junction(codex_skills / "cm"))
            self.assertTrue((codex_skills / "cm" / "SKILL.md").is_file())

    def test_magpie_diff_detects_windows_paths_in_wsl(self):
        wsl_target = targets.Target(
            id="wsl:Ubuntu-24.04", name="WSL", kind="wsl", online=True, os_name="Linux", user="alice", home="/home/alice", distro="Ubuntu-24.04"
        )
        wsl_cfg = self.tmp / "wsl_codex.toml"
        wsl_cfg.write_text('[mcp_servers.bad]\ncommand = "D:/AI/bad.exe"\n', encoding="utf-8")
        with mock.patch.object(targets, "list_targets", return_value=[wsl_target]), \
             mock.patch.object(targets, "detect_target_agents", return_value=[{
                 "qualifiedId": "wsl:Ubuntu-24.04/codex",
                 "uncConfigPath": str(wsl_cfg),
                 "format": "toml",
                 "schema": "mcp_servers",
             }]):
            diff = magpie.magpie_diff()
        self.assertEqual(len(diff["pathViolations"]), 1)
        self.assertEqual(diff["pathViolations"][0]["name"], "bad")

    def test_summarize_skills_plan_rows_and_diff(self):
        plan = {
            "target": "wsl:Ubuntu-24.04",
            "sourceAgent": "pi",
            "destAgent": "codex",
            "sourcePath": "/home/alice/.pi/agent/skills",
            "destPath": "/home/alice/.codex/skills",
            "mode": "copy",
            "destSnapshotHash": "abc123",
            "added": ["jupyter"],
            "updated": ["bio"],
            "kept": ["context-mode"],
            "conflicts": [{"name": "nature-figure", "reason": "目标副本已被用户手动修改"}],
            "preserved": [".system"],
            "skipped": [],
        }
        rows = skills.summarize_skills_plan_rows(
            plan,
            source_entries=["context-mode", "jupyter", "bio", "nature-figure"],
            dest_entries=["context-mode", "bio", "nature-figure", "local-only"],
        )
        by_name = {r["skill"]: r for r in rows}
        self.assertEqual(by_name["jupyter"]["action"], "新增")
        self.assertEqual(by_name["bio"]["action"], "更新")
        self.assertEqual(by_name["context-mode"]["action"], "保留")
        self.assertEqual(by_name["nature-figure"]["action"], "冲突")
        self.assertEqual(by_name[".system"]["action"], "不处理")
        self.assertEqual(by_name["local-only"]["sourceState"], "仅目标存在")

        diff_text = skills.format_skills_plan_diff(plan)
        self.assertIn("+ jupyter", diff_text)
        self.assertIn("~ bio", diff_text)
        self.assertIn("! nature-figure", diff_text)
        self.assertIn("# .system", diff_text)

    def test_plan_skills_sync_empty_include_returns_empty_and_shared_rejects_partial(self):
        pi_skills = self.tmp / "pi_skills_empty"
        codex_skills = self.tmp / "codex_skills_empty"
        (pi_skills / "a").mkdir(parents=True)
        (pi_skills / "a" / "SKILL.md").write_text("# a", encoding="utf-8")
        (pi_skills / "b").mkdir(parents=True)
        (pi_skills / "b" / "SKILL.md").write_text("# b", encoding="utf-8")

        fake_win = targets.Target(
            id="windows", name="Windows", kind="windows", online=True, os_name="Windows", user="u", home=str(self.tmp)
        )
        fake_agents = [
            {"agent": "pi", "skillsPath": str(pi_skills)},
            {"agent": "codex", "skillsPath": str(codex_skills)},
        ]
        with mock.patch.object(targets, "resolve_target", return_value=fake_win), \
             mock.patch.object(targets, "detect_target_agents", return_value=fake_agents), \
             mock.patch.object(skills, "SKILLS_STATE_PATH", self.tmp / "skills-sync.json"):
            empty_plan = skills.plan_skills_sync("windows", "pi", "codex", mode="copy", include=[])
            self.assertEqual(empty_plan["added"], [])
            self.assertEqual(empty_plan["updated"], [])
            self.assertEqual(empty_plan["kept"], [])
            self.assertTrue(empty_plan["alreadySynced"])

            with self.assertRaises(ValueError):
                skills.plan_skills_sync("windows", "pi", "codex", mode="shared-directory", include=["a"])


if __name__ == "__main__":
    unittest.main()
