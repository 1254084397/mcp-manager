"""Command line interface: ``python -m mcpmanager ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import adapters, discovery, doctor, formats, paths, profiles as profiles_mod, registry, sync, versioning


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
        state = "OK" if profile.path.exists() else "--"
        print(f"{profile.id:18} {profile.name:20} {state:6} {profile.path}")


def _print_list(agent: str) -> None:
    profile = _profile(agent)
    section = formats.read_section(profile.path, profile.format, profile.schema)
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
        print(f"{name:24} {'remote' if url else 'stdio':8} {target}")


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
    profile_paths = [profile.path for profile in _profiles().values() if profile.path.exists()]
    report, unknown = discovery.scan_report(data, profile_paths)
    paths.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.REPORT_DIR / "discovery-report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"扫描到 {len(report['configFiles'])} 个有效配置，{len(report['discoveredServers'])} 个 MCP 名称。")
    print("未登记：" + (", ".join(unknown) if unknown else "无"))
    print("报告：" + str(target))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    profile = _profile(args.agent)
    rows = doctor.diagnose(profile, network=args.network)
    print(f"{profile.name}  {profile.path}")
    print(f"{'MCP':24} {'状态':6} {'详情'}")
    for row in rows:
        state = "OK" if row["available"] else ("--" if row["available"] is None else "FAIL")
        print(f"{row['name']:24} {state:6} {row['detail']}")
        for problem in row["problems"]:
            print(f"{'':24}        ! {problem}")
    if args.env:
        print()
        print("本机运行时：")
        for row in doctor.environment_report():
            print(f"  {row['name']:10} {'OK' if row['available'] else '--':3} {row['path']}")
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
            print(f"{name:24} 查询失败：{exc}")
            continue
        if registry.normalize_version(latest) == registry.normalize_version(current):
            print(f"{name:24} 已是最新 {current}")
        else:
            updatable.append(name)
            print(f"{name:24} {current or '未登记'} -> {latest}")
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
    p.set_defaults(func=_cmd_doctor)

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
    try:
        return args.func(args)
    except (KeyError, FileNotFoundError, formats.ConfigError, sync.SecretError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
