"""Validation helpers for adding a new MCP entry."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from . import adapters, versioning


def build_server_spec(
    *,
    name: str,
    command: str,
    args_text: str,
    url: str,
    env: dict[str, str],
    kind: str,
    package: str,
    version: str,
    agent_id: str,
) -> tuple[str, dict[str, Any], list[str]]:
    """Validate form input and return its registry name, spec and secret-like env keys."""
    clean_name = name.strip()
    command = command.strip()
    url = url.strip()
    if not clean_name:
        raise ValueError("请填写 MCP 名称。")
    if bool(command) == bool(url):
        raise ValueError("请只填写 command 或 URL 其中一个。")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("远程 MCP URL 必须是完整的 http:// 或 https:// 地址。")
    args_text = args_text.strip()
    if args_text and not command:
        raise ValueError("URL 类型 MCP 不能填写启动参数。")
    try:
        args = versioning.split_command(args_text) if args_text else []
    except ValueError as exc:
        raise ValueError(f"参数解析失败：{exc}") from exc
    clean_env: dict[str, str] = {}
    for raw_key, value in env.items():
        key = (raw_key or "").strip()
        if not key:
            raise ValueError("环境变量名称不能为空。")
        if key in clean_env:
            raise ValueError(f"环境变量 {key} 重复。")
        clean_env[key] = value
    package = package.strip()
    version = version.strip()
    if version and not package:
        raise ValueError("填写版本时也要填写对应的包名。")

    spec: dict[str, Any] = {
        "kind": kind.strip() or ("remote-http" if url else "manual"),
        "agents": [agent_id],
    }
    if command:
        spec["command"] = command
        if args:
            spec["args"] = args
    else:
        spec["url"] = url
    if clean_env:
        spec["env"] = clean_env
    if package:
        spec["package"] = package
        if version:
            spec["version"] = version
            spec["versionPolicy"] = "pin-before-sync"
    secret_keys = [key for key, value in clean_env.items() if adapters.has_inline_secret(value, key)]
    return clean_name, spec, secret_keys
