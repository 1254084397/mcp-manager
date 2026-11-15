"""Command line interface: ``python -m mcpmanager ...``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import (adapters, discovery, doctor, formats, handshake, health, paths,
               profiles as profiles_mod, registry, sync, versioning)


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
    print(f"{'ID':18} {'名称':20} {'状态':6} 配置")
    for profile in _profiles().values():
        state = "OK" if any(path.exists() for path in profile.paths) else "--"
        print(f"{profile.id:18} {profile.name:20} {state:6} {profile.path}")
        for extra in profile.paths[1:]:
            if extra.exists():
                mark = "只读，存在"
            else:
                mark = "只读，可选，未发现" if profile.auxiliary_optional else "只读，必选，缺失"
            print(f"{'':18} {'':20} {'附加':6} {extra}（{mark}）")


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
    profile = _profile(args.agent)
    data = registry.load()
    candidate = sync.build_candidate(profile, data)
    print(f"候选已生成：{candidate.path}")
    print(f"目标：{candidate.target}（{len(candidate.servers)} 个 MCP）")
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
    data = registry.load()
    spec = dict(catalog[args.name])
    spec.pop("description", None)
    registry.add_server(data, args.name, spec)
    registry.save(data)
    print(f"已登记 {args.name}。")
    targets = _resolve_targets(args.agent)
    for profile in targets:
        if args.apply:
            if not args.yes and not _confirm(f"写入 {profile.name}？"):
                continue
            section = (
                formats.read_section(profile.path, profile.format, profile.schema) if profile.path.exists() else {}
            )
            pinned = registry.patch_pins(args.name, registry.entry(data, args.name) or spec, data)
            section[args.name] = adapters.to_agent(pinned, profile)
            backup, mode = sync.apply_section(profile, section)
            print(f"{profile.name}：已写入（{mode}）。备份：{backup}")
        else:
            path = sync.install_candidate(profile, data, args.name)
            print(f"{profile.name}：候选 {path}")
            print(f"  检查后应用：mcpmanager apply \"{path}\"")
    return 0


def _cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import main as gui_main

    gui_main()
    return 0


def _cmd_paths(_args: argparse.Namespace) -> int:
    for key, value in doctor.project_paths().items():
        print(f"{key:10} {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="mcpmanager", description="跨 Agent 的 MCP 管理器")
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

    p = sub.add_parser("updates", help="检查可升级的包")
    p.set_defaults(func=_cmd_updates)

    p = sub.add_parser("sync", help="生成候选配置")
    p.add_argument("--agent", required=True)
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

    p = sub.add_parser("install", help="从目录安装 MCP")
    p.add_argument("name")
    p.add_argument("--agent", action="append")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_cmd_install)

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
