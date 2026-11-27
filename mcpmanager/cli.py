"""Command line interface: ``python -m mcpmanager ...``."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import (adapters, context_audit, discovery, doctor, formats, handshake, health, magpie,
               paths, profiles as profiles_mod, provenance, registry, skills, sync, targets, versioning)


def _profiles():
    paths.ensure_layout()
    return profiles_mod.load_profiles()


def _profile(key: str):
    return profiles_mod.resolve_profile(_profiles(), key)


def _resolve_targets(values: list[str] | None) -> list:
    pool = _profiles()
    if not values:
        return profiles_mod.writable_profiles(pool)
    return [profiles_mod.resolve_profile(pool, value) for value in values]


def _print_agents() -> None:
    print(f"{'ID':18} {'名称':20} {'状态':6} {'来源':8} 配置")
    for profile in _profiles().values():
        info = profiles_mod.describe_profile_paths(profile)
        state = "OK" if any(path.exists() for path in profile.paths) else "--"
        kind_label = "本机覆盖" if info["sourceKind"] == "override" else "内置"
        print(f"{profile.id:18} {profile.name:20} {state:6} {kind_label:8} {profile.path}  ({info['recognitionBasis']})")
        for extra in profile.paths[1:]:
            if extra.exists():
                mark = "只读，存在"
            else:
                mark = "只读，可选，未发现" if profile.auxiliary_optional else "只读，必选，缺失"
            print(f"{'':18} {'':20} {'附加':6} {'':8} {extra}（{mark}）")
        if info["priorityNote"]:
            print(f"{'':18} {'':20} {'提示':6} {'':8} {info['priorityNote']}")


def _print_list(agent: str) -> None:
    profile = _profile(agent)
    section, sources = formats.read_merged_section(profile.paths, profile.format, profile.schema)
    if not section:
        print(f"{profile.name} 未配置任何 MCP：{profile.path}")
        return
    print(f"{profile.name}  {profile.path}")
    print(f"{'MCP':24} {'传输':8} 命令 / URL")
    for name, spec in section.items():
        if not isinstance(spec, dict):
            continue
        url = adapters.url_of(spec, profile)
        target = url or " ".join(filter(None, [adapters.command_of(spec), *adapters.args_of(spec)]))
        source = sources.get(name)
        suffix = "  [附加配置·只读]" if source and source != profile.path else ""
        print(f"{name:24} {'remote' if url else 'stdio':8} {target}{suffix}")


def _cmd_agents(_args: argparse.Namespace) -> int:
    _print_agents()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    if args.agent:
        _print_list(args.agent)
        return 0
    for profile in _profiles().values():
        if profile.path.exists():
            _print_list(profile.id)
            print()
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    data = registry.load()
    profile_paths = [path for profile in _profiles().values() for path in profile.paths if path.exists()]
    report, unknown = discovery.scan_report(data, profile_paths)
    paths.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.REPORT_DIR / "discovery-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"扫描到 {len(report['configFiles'])} 个有效配置，{len(report['discoveredServers'])} 个 MCP 名称。")
    print(f"当前生效配置：{len(report['effectiveServers'])} 个 MCP；"
          f"缓存/插件/备份：{len(report['cachedServers'])} 个 MCP。")
    print("未登记（当前生效）：" + (", ".join(unknown) if unknown else "无"))
    if report["unknownCached"]:
        print("未登记（缓存/插件/备份，仅供参考）：" + ", ".join(report["unknownCached"]))
    print("报告：" + str(target))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    profile = _profile(args.agent)
    rows = doctor.diagnose(profile, network=args.network)
    environment = doctor.environment_report() if args.env else []
    if args.json:
        report = {
            "agent": profile.id,
            "name": profile.name,
            "path": str(profile.path),
            "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "servers": rows,
            "environment": environment,
        }
        paths.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = paths.REPORT_DIR / f"doctor-{profile.id}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"{profile.name}  {profile.path}")
    print(f"{'MCP':24} {'状态':6} {'详情'}")
    for row in rows:
        state = "OK" if row["available"] else ("--" if row["available"] is None else "FAIL")
        print(f"{row['name']:24} {state:6} {row['detail']}")
        for problem in row["problems"]:
            print(f"{'':24}        ! {problem}")
    if environment:
        print()
        print("本机运行时：")
        for row in environment:
            print(f"  {row['name']:10} {'OK' if row['available'] else '--':3} {row['path']}")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    profile = _profile(args.agent)
    section, _sources = formats.read_merged_section(profile.paths, profile.format, profile.schema)
    if args.name:
        if args.name not in section:
            raise KeyError(f"{profile.name} 的配置里没有 {args.name}")
        targets = [(args.name, section[args.name])]
    else:
        targets = [(name, spec) for name, spec in section.items() if isinstance(spec, dict)]
    if not targets:
        print(f"{profile.name} 没有可测试的 MCP。")
        return 0
    failures = 0
    results: dict[str, dict[str, Any]] = {}
    for name, spec in targets:
        result = handshake.probe(profile, name, spec, timeout=args.timeout)
        health.record(profile.id, name, result)
        results[name] = {
            "ok": bool(result["ok"]),
            "stage": result["stage"],
            "detail": result["detail"],
            "toolCount": len(result["tools"]),
            "tools": result["tools"],
            "serverInfo": result.get("serverInfo") or {},
            "skipped": bool(result.get("skipped")),
        }
        if not result["ok"] and not result.get("skipped"):
            failures += 1
        if args.json:
            continue
        state = "OK" if result["ok"] else "FAIL"
        print(f"{name:24} {state:4} {result['stage']:10} {result['detail']}")
        if result["tools"]:
            print(f"{'':24}      工具：{', '.join(result['tools'])}")
    if args.json:
        payload = {
            "agent": profile.id,
            "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "servers": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _cmd_health(args: argparse.Namespace) -> int:
    data = health.load()
    if args.agent:
        data = {args.agent: data.get(args.agent, {})}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    rows = [(agent, name, value) for agent, servers in data.items() for name, value in servers.items()]
    if not rows:
        print("还没有握手缓存。运行 mcpmanager probe --agent <id> 后生成。")
        return 0
    print(f"{'AGENT':16} {'MCP':24} {'状态':6} {'工具':4} 检查时间")
    for agent, name, value in sorted(rows):
        state = "OK" if value.get("ok") else "FAIL"
        print(f"{agent:16} {name:24} {state:6} {value.get('toolCount', 0):<4} {value.get('checkedAt', '')}")
    return 0


def collect_status() -> dict[str, Any]:
    """Collect status summary for all configured Agent profiles without mutating anything."""
    rows = []
    discovered_configs = []
    all_conflicts = []
    cached = health.load()
    reg_data = registry.load()
    managed_all = provenance.load_managed()

    for profile in _profiles().values():
        det = profiles_mod.detect_agent(profile)
        config_state = str(det["configState"])
        count = det["mcpCount"]
        server_names = list(det["serverNames"])
        agent_conflicts: list[dict[str, str]] = []

        if config_state in {"ok", "plugin_only"}:
            try:
                section, _sources = formats.read_merged_section(
                    profile.paths, profile.format, profile.schema
                )
                managed_map = managed_all.get(profile.id, {})
                for srv_name, spec in section.items():
                    if not isinstance(spec, dict) or adapters.is_container(spec):
                        continue
                    managed_entry = managed_map.get(srv_name) if isinstance(managed_map, dict) else None
                    reason = sync._detect_conflict(srv_name, spec, profile, reg_data, managed_entry)
                    if reason:
                        conflict_item = {"agent": profile.id, "name": srv_name, "reason": reason}
                        agent_conflicts.append(conflict_item)
                        all_conflicts.append(conflict_item)
            except Exception:
                pass
        for dup in det.get("pathDuplicates", []):
            shadowed = "，".join(dup["shadowedSources"])
            conflict_item = {
                "agent": profile.id,
                "name": dup["name"],
                "reason": f"多配置文件存在同名 MCP：实际采用 {dup['effectiveSource']}，被隐藏来源：{shadowed}",
            }
            agent_conflicts.append(conflict_item)
            all_conflicts.append(conflict_item)

        for ev in det["evidence"]:
            if ev["exists"]:
                discovered_configs.append({
                    "agent": profile.id,
                    "path": ev["path"],
                    "kind": ev["kind"],
                    "redirected": ev["redirected"],
                    "resolvedPath": ev["resolvedPath"],
                })

        checks = cached.get(profile.id, {})
        if not isinstance(checks, dict):
            checks = {}
        if config_state in {"ok", "plugin_only"}:
            ok = sum(1 for name in server_names if isinstance(checks.get(name), dict) and checks[name].get("ok") is True)
            failed = sum(1 for name in server_names if isinstance(checks.get(name), dict) and checks[name].get("ok") is False)
            checked = ok + failed
            unchecked = max(0, (int(count) if count is not None else 0) - checked)
        else:
            ok = sum(1 for item in checks.values() if isinstance(item, dict) and item.get("ok") is True)
            failed = sum(1 for item in checks.values() if isinstance(item, dict) and item.get("ok") is False)
            checked = ok + failed
            unchecked = 0
        timestamps = [
            str(item.get("checkedAt"))
            for item in checks.values()
            if isinstance(item, dict) and item.get("checkedAt")
        ]
        last_checked_at = max(timestamps) if timestamps else None

        rows.append({
            "agent": profile.id,
            "name": profile.name,
            "detected": bool(det["detected"]),
            "configured": bool(det["configured"]),
            "configFileExists": bool(det["configured"]),
            "agentLoaded": None,
            "configState": config_state,
            "configError": det["configError"],
            "path": det["displayPath"],
            "displayPath": det["displayPath"],
            "resolvedPath": det["resolvedPath"],
            "redirected": bool(det["redirected"]),
            "sourceKind": det["sourceKind"],
            "recognitionBasis": det["recognitionBasis"],
            "recognitionTokens": det["recognitionTokens"],
            "evidence": det["evidence"],
            "candidatePaths": det["evidence"],
            "hasMultipleSources": bool(det["hasMultipleSources"]),
            "priority": det["priority"],
            "priorityNote": det["priorityNote"],
            "mcpCount": count,
            "hostMcpCount": det["hostMcpCount"],
            "sourceCounts": det["sourceCounts"],
            "serverSources": det["serverSources"],
            "pathDuplicates": det["pathDuplicates"],
            "conflicts": agent_conflicts,
            "lastHealth": {
                "ok": ok,
                "failed": failed,
                "unchecked": unchecked,
                "checked": checked,
                "lastCheckedAt": last_checked_at,
                "advisoryOnly": True,
            },
        })
    return {
        "schemaVersion": 2,
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "note": "configured/configFileExists 仅表示配置文件存在，不代表 Agent 当前已加载；lastHealth 来自最近一次本地握手缓存。",
        "notes": [
            "configured/configFileExists 仅表示配置文件存在，不代表 Agent 当前已加载。",
            "lastHealth 来自最近一次本地握手缓存 (local/health.json)，不代表当前运行态。",
        ],
        "agents": rows,
        "discoveredConfigs": discovered_configs,
        "conflicts": all_conflicts,
    }


def _cmd_status(args: argparse.Namespace) -> int:
    """Summarize live Agent config locations and last known MCP health."""
    if getattr(args, "target", None):
        target = targets.resolve_target(args.target)
        t_agents = targets.detect_target_agents(target)
        payload = {
            "schemaVersion": 2,
            "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "target": target.id,
            "online": target.online,
            "agents": t_agents,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        print(f"目标：{target.name}（{target.id}）  状态：{'在线' if target.online else '离线'}  用户：{target.user or '-'}  HOME：{target.home or '-'}")
        print(f"{'QUALIFIED_ID':32} {'配置状态':10} {'MCP':>4} {'Skills':>6} 配置路径")
        for item in t_agents:
            mc = "-" if item["mcpCount"] is None else str(item["mcpCount"])
            print(f"{item['qualifiedId']:32} {item['configState']:10} {mc:>4} {item['skillsCount']:>6} {item['configPath']}")
        return 0
    report = collect_status()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    state_labels = {
        "ok": "存在",
        "plugin_only": "仅插件",
        "missing": "未发现",
        "parse_error": "格式错误",
        "permission_error": "权限错误",
        "read_error": "读取错误",
    }
    print(f"{'AGENT':16} {'检测':6} {'配置状态':8} {'MCP':>4} {'来源数':>6} {'最近握手(成/败/未)':18} {'最后检查':19} 有效路径")
    for row in report["agents"]:
        det_label = "是" if row["detected"] else "否"
        state = state_labels.get(row["configState"], row["configState"])
        count_text = "-" if row["mcpCount"] is None else str(row["mcpCount"])
        src_count = len(row.get("sourceCounts") or {})
        h = row["lastHealth"]
        health_text = f"{h['ok']}/{h['failed']}/{h['unchecked']}"
        last_time = h["lastCheckedAt"] or "-"
        print(f"{row['agent']:16} {det_label:6} {state:8} {count_text:>4} {src_count:>6} {health_text:18} {last_time:19} {row['displayPath']}")
        if row["redirected"]:
            print(f"  [重定向] 显示路径：{row['displayPath']} | 实际路径：{row['resolvedPath']} | 是否重定向：是")
        if row["configError"]:
            print(f"  [错误] {row['configError']}")
        if row["priorityNote"]:
            print(f"  [多路径] {row['priorityNote']}")
        for conflict in row.get("conflicts", []):
            print(f"  [冲突] {conflict['name']}：{conflict['reason']}")
    print()
    print("说明：'存在' 仅表示配置文件在磁盘上存在，不代表 Agent 当前已加载；最近握手来自本地探测缓存。")
    return 0


def _cmd_updates(_args: argparse.Namespace) -> int:
    data = registry.load()
    targets = [(name, spec) for name, spec in registry.servers(data).items() if spec.get("package")]
    if not targets:
        print("注册表里没有带 package 的 MCP。")
        return 0
    updatable = []
    for name, spec in targets:
        field = registry.version_field(spec)
        current = spec.get(field, "") if field else ""
        try:
            latest = versioning.latest_version(spec)
        except Exception as exc:  # noqa: BLE001 - reported inline
            print(f"{name:24} {versioning.STATUS_FAILED:8} {exc}")
            continue
        status, _cmp = versioning.compare_versions(current, latest)
        if status == versioning.STATUS_NEWER:
            updatable.append(name)
        print(f"{name:24} {status:8} {current or '未登记'} -> {latest}")
    if updatable:
        print("可更新：" + ", ".join(updatable))
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    target_id = getattr(args, "target", None) or "windows"
    if target_id != "windows":
        cand_path = sync.build_target_candidate(target_id, args.agent, registry.load())
        summary = sync.summarize_candidate(cand_path)
        diff = sync.candidate_diff(cand_path)
        print(f"目标：{target_id}  Agent：{args.agent}")
        print(f"候选已生成：{cand_path}")
        print(
            "变更计划："
            f"新增 {len(summary['added'])}，更新 {len(summary['updated'])}，"
            f"保留 {len(summary['kept'])}，跳过 {len(summary['skipped'])}，冲突 {len(summary.get('conflicts', []))}"
        )
        for item in summary["skipped"]:
            print(f"  [跳过] {item['name']}：{item['reason']}")
        if diff:
            print("--- 差异预览（凭据已脱敏） ---")
            print(diff)
        if not args.apply:
            print("未应用。检查候选后加 --apply 执行。")
            return 0
        if not args.yes and not _confirm(f"确认备份并写入 {target_id}/{args.agent}？"):
            print("已取消。")
            return 1
        backup, mode = sync.apply_target_candidate(cand_path)
        print(f"已应用（{mode}）。备份：{backup}")
        return 0
    profile = _profile(args.agent)
    data = registry.load()
    candidate = sync.build_candidate(profile, data)
    summary = sync.summarize_candidate(candidate.path)
    diff = sync.candidate_diff(candidate.path)
    print(f"候选已生成：{candidate.path}")
    print(f"目标：{candidate.target}（{len(candidate.servers)} 个 MCP）")
    print(
        "变更计划："
        f"新增 {len(summary['added'])}（{', '.join(summary['added']) or '无'}），"
        f"更新 {len(summary['updated'])}（{', '.join(summary['updated']) or '无'}），"
        f"保留 {len(summary['kept'])}，"
        f"跳过 {len(summary['skipped'])}，"
        f"冲突 {len(summary.get('conflicts', []))}"
    )
    for item in summary.get("conflicts", []):
        print(f"  [冲突] {item['name']}：{item['reason']}")
    for item in summary["skipped"]:
        print(f"  [跳过] {item['name']}：{item['reason']}")
    if diff:
        print("--- 差异预览（凭据已脱敏） ---")
        print(diff)
    if not args.apply:
        print("未应用。检查候选后运行：mcpmanager sync --agent ... --apply")
        return 0
    if not args.yes and not _confirm("确认备份并仅替换 MCP 区块？"):
        print("已取消。")
        return 1
    backup, mode = sync.apply_candidate(candidate.path)
    print(f"已应用（{mode}）。备份：{backup}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    profile = _profile(args.agent)
    path = sync.uninstall_candidate(profile, args.name)
    print(f"卸载候选已生成：{path}")
    print(f"目标：{profile.path}")
    if not args.apply:
        print(f"未应用。检查差异后运行：mcpmanager apply \"{path}\"")
        return 0
    if not args.yes and not _confirm(f"从 {profile.name} 卸载 {args.name} 并备份当前配置？"):
        print("已取消。")
        return 1
    backup, mode = sync.apply_candidate(path)
    print(f"已卸载（{mode}）。备份：{backup}")
    return 0


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt + " [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _cmd_apply(args: argparse.Namespace) -> int:
    path = Path(args.candidate)
    candidate = sync.load_candidate(path)
    label = candidate.get("agentName", candidate["agent"])
    if not args.yes and not _confirm(f"应用 {label} 候选并备份当前配置？"):
        print("已取消。")
        return 1
    backup, mode = sync.apply_candidate(path)
    print(f"已应用（{mode}）。备份：{backup or '无（目标为新建文件）'}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    spec: dict[str, Any] = {"kind": args.kind, "agents": args.agent or ["*"]}
    if args.command:
        spec["command"] = args.command
    if args.arg:
        spec["args"] = args.arg
    if args.env:
        env = {}
        for item in args.env:
            key, _, value = item.partition("=")
            if not key:
                raise SystemExit(f"--env 需要 KEY=VALUE：{item}")
            env[key] = value
        spec["env"] = env
    if args.url:
        spec["url"] = args.url
    if args.package:
        spec["package"] = args.package
    if args.version:
        spec["version"] = args.version
    if not any(key in spec for key in ("command", "url")):
        raise SystemExit("至少需要 --command 或 --url")
    data = registry.load()
    registry.add_server(data, args.name, spec)
    registry.save(data)
    print(f"已登记 {args.name}。运行 mcpmanager sync --agent <id> 生成候选。")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    data = registry.load()
    if not registry.remove_server(data, args.name):
        print(f"注册表里没有 {args.name}。")
        return 1
    registry.save(data)
    print(f"已从注册表移除 {args.name}（Agent 配置未改动）。")
    return 0


def _load_catalog() -> dict[str, Any]:
    if not paths.CATALOG_PATH.exists():
        return {}
    data = json.loads(paths.CATALOG_PATH.read_text(encoding="utf-8-sig"))
    return data.get("servers", {})


def _cmd_catalog(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    query = (args.query or "").lower()
    for name, spec in catalog.items():
        text = f"{name} {spec.get('description', '')}".lower()
        if query and query not in text:
            continue
        target = spec.get("url") or " ".join(filter(None, [spec.get("command"), *(spec.get("args") or [])]))
        print(f"{name:24} {spec.get('description', ''):40} {target}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    catalog = _load_catalog()
    if args.name not in catalog:
        raise SystemExit(f"目录里没有 {args.name}。运行 mcpmanager catalog 查看可用条目。")
    targets = _resolve_targets(args.agent)
    spec = dict(catalog[args.name])
    spec.pop("description", None)
    prepared = copy.deepcopy(registry.load())
    registry.add_server(prepared, args.name, spec)

    if not args.apply:
        registry.save(prepared)
        print(f"已登记 {args.name}。")
        for profile in targets:
            path = sync.install_candidate(profile, prepared, args.name)
            print(f"{profile.name}：候选 {path}")
            print(f"  检查后应用：mcpmanager apply \"{path}\"")
        return 0

    # --apply 保持直接写入语义，但先准备候选并展示差异；确认后才登记注册表、
    # 备份并写入 Agent 配置。全部取消时注册表保持原样。
    for profile in targets:
        path = sync.install_candidate(profile, prepared, args.name)
        if not args.yes:
            print(f"--- {profile.name} 将写入的差异 ---")
            print(sync.candidate_diff(path) or "（无变化）")
            if not _confirm(f"写入 {profile.name}？"):
                print(f"{profile.name}：已取消，Agent 配置和注册表均未修改。")
                continue
        registry.save(prepared)
        backup, mode = sync.apply_candidate(path)
        print(f"{profile.name}：已写入（{mode}）。备份：{backup}")
    return 0


def _cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def _cmd_paths(_args: argparse.Namespace) -> int:
    for key, value in doctor.project_paths().items():
        print(f"{key:10} {value}")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    target_env = getattr(args, "target", None)
    agent = args.agent
    if agent and (not target_env or target_env == "windows"):
        agent = _profile(agent).id
    entries = provenance.load_audit(agent=agent, target_env=target_env, limit=args.limit)
    if args.json:
        print(json.dumps({"entries": entries}, ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("暂无同步审计记录。")
        return 0
    print(f"{'时间':19} {'目标':16} {'AGENT':12} {'模式':10} {'结果':6} {'新增/更/删/跳/冲突':16} 备份")
    for item in entries:
        s = item.get("summary") or {}
        counts = (
            f"{len(s.get('added', []))}/{len(s.get('updated', []))}/"
            f"{len(s.get('removed', []))}/{len(s.get('skipped', []))}/"
            f"{len(s.get('conflicts', []))}"
        )
        print(
            f"{str(item.get('timestamp', '-')):19} "
            f"{str(item.get('targetEnv', 'windows')):16} "
            f"{str(item.get('agent', '-')):12} "
            f"{str(item.get('mode', '-')):10} "
            f"{str(item.get('result', '-')):6} "
            f"{counts:16} {item.get('backupPath') or '-'}"
        )
    return 0


def _cmd_targets(args: argparse.Namespace) -> int:
    report = targets.collect_targets_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"{'TARGET':18} {'类型':8} {'状态':6} {'用户':14} {'Agent数':>7} HOME")
    for item in report["targets"]:
        online = "在线" if item["online"] else "离线"
        print(f"{item['id']:18} {item['kind']:8} {online:6} {item['user'] or '-':14} {len(item['agents']):>7} {item['home'] or '-'}")
    return 0


def _cmd_detect(args: argparse.Namespace) -> int:
    report = targets.collect_targets_report(args.target)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for t in report["targets"]:
        print(f"目标：{t['name']}（{t['id']}）  在线：{'是' if t['online'] else '否'}  OS：{t['os']}  用户：{t['user']}")
        tools_str = ", ".join(f"{k}={'OK' if v else 'NO'}" for k, v in t["tools"].items())
        print(f"工具：{tools_str}  Magpie：{'可访问' if t['magpieReachable'] else '未发现'}")
        for ag in t["agents"]:
            print(f"  - {ag['qualifiedId']:28} MCP={ag['mcpCount']}  Skills={ag['skillsCount']}  {ag['configPath']}")
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    action = args.skills_action
    if action == "list":
        data = skills.list_target_skills(args.target)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for ag in data["agents"]:
            print(f"{ag['target']}/{ag['agent']}  模式={ag['mode']}  数量={ag['count']}  路径={ag['skillsPath']}")
        return 0
    if action == "status":
        data = skills.skills_status(args.target)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        for ag in data["agents"]:
            print(f"{ag['target']}/{ag['agent']:14} 模式={ag['mode']:18} 数量={ag['count']:>3}  {ag['syncNote']}")
        return 0
    if action == "sync":
        plan = skills.plan_skills_sync(
            args.target or "windows",
            args.source,
            args.agent,
            mode=args.mode,
            include=args.include,
            replace=args.replace,
        )
        print(f"目标：{plan['target']}  模式：{plan['mode']}（修改已有Skill自动同步：{'是' if plan['autoSyncsOnEdit'] else '否'}）")
        print(
            f"新增：{len(plan['added'])}  更新：{len(plan['updated'])}  "
            f"保留：{len(plan['kept'])}  系统保留：{', '.join(plan['preserved']) or '无'}  跳过：{len(plan['skipped'])}"
        )
        if not args.apply:
            print("未应用。确认后加 --apply 执行。")
            return 0
        if not args.yes and not _confirm(f"确认按 {plan['mode']} 同步 Skills 到 {plan['destAgent']}？"):
            print("已取消。")
            return 1
        res = skills.apply_skills_sync(plan)
        print(f"Skills 同步完成：新增 {len(res['added'])} 项，更新 {len(res['updated'])} 项，备份：{res['backup'] or '无'}。")
        return 0
    return 1


def _cmd_rollback(args: argparse.Namespace) -> int:
    if not args.yes and not _confirm(f"确认回滚 {args.target}/{args.agent} 到最近一次备份？"):
        print("已取消。")
        return 1
    used, safety = sync.rollback_from_audit(args.target, args.agent)
    print(f"已回滚到：{used}（当前配置已先备份至：{safety or '无'}）")
    return 0


def _cmd_magpie(args: argparse.Namespace) -> int:
    action = args.magpie_action
    fn = {
        "status": magpie.magpie_status,
        "agents": magpie.magpie_agents,
        "library": magpie.magpie_library,
        "diff": magpie.magpie_diff,
    }[action]
    data = fn()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    report = context_audit.collect_context_audit(include_wsl=not args.no_wsl)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    shared = report["sharedSkills"]
    print(f"公共 Skill 目录：{shared['sharedAgentsDir']}（{shared['sharedSkillCount']} 个）")
    if shared["magpieLibraryExists"]:
        print(f"Magpie Skill 总库：{shared['magpieLibraryDir']}（{shared['magpieLibraryCount']} 个）")
    print()
    mcp_labels = {"routed": "已折叠(网关)", "hybrid": "混合", "direct": "直连未折叠", "empty": "空", "error": "读取失败"}
    skill_labels = {
        "routed": "已折叠(路由)",
        "leaked_shared": "公共目录泄漏",
        "expanded": "全量展开",
        "symlink_shared": "软链共享",
        "empty": "空",
        "unknown": "未知",
    }
    print(f"{'AGENT':16} {'MCP模式':12} {'MCP数':>5} {'Skill模式':14} {'Skill数':>7} 说明")
    for row in report["agents"]:
        mcp_m = mcp_labels.get(row["mcp"]["mode"], row["mcp"]["mode"])
        sk_m = skill_labels.get(row["skills"]["mode"], row["skills"]["mode"])
        print(
            f"{row['agent']:16} {mcp_m:12} {row['mcp']['mcpCount']:>5} "
            f"{sk_m:14} {row['skills']['count']:>7} {row['skills']['note']}"
        )
        for dup in row.get("duplicatePlugins", []):
            print(f"  [重复插件] 指向同一目录 {dup['resolvedTarget']}：{', '.join(dup['entries'])}")
    if report["wslAgents"]:
        print("\nWSL 只读发现：")
        for row in report["wslAgents"]:
            mcp_m = mcp_labels.get(row["mcp"]["mode"], row["mcp"]["mode"])
            sk = row["skills"]
            link_note = "（目录软链）" if sk["isSymlink"] else (f"（含 {sk['symlinkEntries']} 个软链）" if sk["symlinkEntries"] else "")
            print(f"  {row['agent']:28} MCP={mcp_m}({row['mcp']['mcpCount']})  Skills={sk['count']}{link_note}")
    if report["recommendations"]:
        print("\n优化建议：")
        for rec in report["recommendations"]:
            print(f"  - {rec}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="mcpmanager", description="跨 Agent 的 MCP 与 Skills 管理器")
    parser.add_argument("--version", action="version", version=f"mcpmanager {__version__}")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="只读模式：允许扫描、诊断、握手和报告，禁止写入配置、安装、升级和修改注册表",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("agents", help="列出已支持的 Agent").set_defaults(func=_cmd_agents)
    sub.add_parser("paths", help="显示数据目录").set_defaults(func=_cmd_paths)

    p = sub.add_parser("list", help="列出某个 Agent 已配置的 MCP")
    p.add_argument("--agent")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("scan", help="扫描本机所有 MCP 配置")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("doctor", help="检查命令/URL 是否可用")
    p.add_argument("--agent", required=True)
    p.add_argument("--network", action="store_true", help="额外探测远程 URL")
    p.add_argument("--env", action="store_true", help="显示 Node/npm/uvx 等运行时")
    p.add_argument("--json", action="store_true", help="输出 JSON 并写入 local/reports")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("probe", help="真实 MCP 握手测试（initialize + tools/list）")
    p.add_argument("--agent", required=True)
    p.add_argument("--name", help="只测试指定的 MCP")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.set_defaults(func=_cmd_probe)

    p = sub.add_parser("health", help="查看上次握手缓存")
    p.add_argument("--agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_health)

    p = sub.add_parser("status", help="汇总 Agent 的有效配置路径、MCP 数量和最近健康状态")
    p.add_argument("--target", help="按运行目标过滤（如 windows 或 wsl:Ubuntu-24.04）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("targets", help="列出本机 Windows 与 WSL 运行目标")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_targets)

    p = sub.add_parser("detect", help="探测指定运行目标中的 Agent、工具链与路径")
    p.add_argument("--target", default="windows")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_detect)

    p = sub.add_parser("updates", help="检查可升级的包")
    p.set_defaults(func=_cmd_updates)

    p = sub.add_parser("sync", help="生成候选配置")
    p.add_argument("--agent", required=True)
    p.add_argument("--target", default="windows", help="运行目标（windows 或 wsl:<distro>）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_sync)

    p = sub.add_parser("apply", help="应用一个候选文件")
    p.add_argument("candidate")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_apply)

    p = sub.add_parser("add", help="把 MCP 登记到主注册表")
    p.add_argument("name")
    p.add_argument("--command")
    p.add_argument("--arg", action="append")
    p.add_argument("--env", action="append", metavar="KEY=VALUE")
    p.add_argument("--url")
    p.add_argument("--kind", default="manual")
    p.add_argument("--package")
    p.add_argument("--version")
    p.add_argument("--agent", action="append")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("remove", help="从主注册表移除 MCP")
    p.add_argument("name")
    p.set_defaults(func=_cmd_remove)

    p = sub.add_parser("uninstall", help="从 Agent 配置中卸载 MCP")
    p.add_argument("name")
    p.add_argument("--agent", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_uninstall)

    p = sub.add_parser("catalog", help="查看内置 MCP 目录")
    p.add_argument("query", nargs="?")
    p.set_defaults(func=_cmd_catalog)

    p = sub.add_parser(
        "install",
        help="从目录安装 MCP：不带 --apply 时登记并生成候选，--apply 展示差异确认后备份并写入",
    )
    p.add_argument("name")
    p.add_argument("--agent", action="append")
    p.add_argument("--apply", action="store_true", help="确认后直接写入 Agent 配置（写入前备份）")
    p.add_argument("--yes", action="store_true", help="跳过交互确认，配合 --apply 用于自动化")
    p.set_defaults(func=_cmd_install)

    p = sub.add_parser("audit", help="查看同步审计记录")
    p.add_argument("--target", help="按运行目标过滤（windows 或 wsl:<distro>）")
    p.add_argument("--agent")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_audit)

    p = sub.add_parser("rollback", help="按审计记录回滚指定目标与 Agent 的配置")
    p.add_argument("--target", default="windows")
    p.add_argument("--agent", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_rollback)

    skills_parser = sub.add_parser("skills", help="管理各目标下的 Agent Skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_action", required=True)
    for name, help_text in (("list", "列出各 Agent 的 Skills 目录与数量"), ("status", "查看 Skills 同步状态与模式")):
        sp = skills_sub.add_parser(name, help=help_text)
        sp.add_argument("--target", default="windows")
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=_cmd_skills)
    sp = skills_sub.add_parser("sync", help="安全同步 Skills 到指定 Agent")
    sp.add_argument("--target", default="windows")
    sp.add_argument("--source", default="pi")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--mode", choices=skills.SYNC_MODES, default="symlink-per-entry")
    sp.add_argument("--include", action="append")
    sp.add_argument("--replace", action="store_true", help="shared-directory 模式下允许备份并替换非空目标目录")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=_cmd_skills)

    magpie_parser = sub.add_parser("magpie", help="只读查看 Magpie 纳管状态与路径差异")
    magpie_sub = magpie_parser.add_subparsers(dest="magpie_action", required=True)
    for name, help_text in (
        ("status", "查看 Magpie 纳管概览"),
        ("agents", "列出 Magpie 纳管的 Agent"),
        ("library", "查看 Magpie Skill 总库"),
        ("diff", "检查跨平台路径与配置差异"),
    ):
        mp = magpie_sub.add_parser(name, help=help_text)
        mp.add_argument("--json", action="store_true")
        mp.set_defaults(func=_cmd_magpie)

    p = sub.add_parser("context", help="审计各 Agent（含 WSL）的 MCP 路由、Skill 折叠与重复插件")
    p.add_argument("--no-wsl", action="store_true", help="跳过 WSL 发行版只读扫描")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_context)

    sub.add_parser("gui", help="打开图形界面").set_defaults(func=_cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "read_only", False):
        os.environ["MCP_MANAGER_READ_ONLY"] = "1"
    try:
        return args.func(args)
    except (KeyError, FileNotFoundError, formats.ConfigError, paths.ReadOnlyError,
            sync.SecretError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
