"""Read and rewrite MCP sections without destroying the rest of a config.

JSON and JSONC files are rewritten by replacing only the top-level MCP object,
so comments and unrelated settings survive. TOML files are rewritten by
replacing only the ``[mcp_servers*]`` table group; if that cannot be done
safely we refuse instead of clobbering the file.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Config cannot be read or safely written."""


# --------------------------------------------------------------------------- #
# JSONC
# --------------------------------------------------------------------------- #
def strip_jsonc(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and nxt == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
        elif char == "/" and nxt == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                index += 1
            index += 2
        else:
            result.append(char)
            index += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(result))


def parse_json(text: str, jsonc: bool = False) -> dict[str, Any]:
    if jsonc:
        text = strip_jsonc(text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是对象")
    return data


def load_config(path: Path, fmt: str) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置不存在：{path}")
    text = path.read_text(encoding="utf-8-sig")
    if fmt == "toml":
        data = tomllib.loads(text)
        if not isinstance(data, dict):
            raise ConfigError("TOML 顶层必须是表")
        return data
    return parse_json(text, jsonc=(fmt == "jsonc"))


def read_section(path: Path, fmt: str, schema: str) -> dict[str, Any]:
    data = load_config(path, fmt)
    section = data.get(schema)
    if not isinstance(section, dict):
        if section is None:
            return {}
        raise ConfigError(f"{path} 的 {schema} 不是对象")
    return section


def read_merged_section(
    paths: list[Path], fmt: str, schema: str
) -> tuple[dict[str, Any], dict[str, Path]]:
    """Merge one Agent's configs, primary first, and remember each source.

    Auxiliary configs are read-only: they may contribute servers that the main
    config does not define, but the primary file always wins on conflicts. A
    missing auxiliary file is skipped; a broken primary file still raises.
    """
    merged: dict[str, Any] = {}
    sources: dict[str, Path] = {}
    for index, path in enumerate(paths):
        if not path.exists():
            continue
        try:
            section = read_section(path, fmt, schema)
        except Exception:
            if index == 0:
                raise
            continue
        for name, spec in section.items():
            if name in merged:
                continue
            merged[name] = spec
            sources[name] = path
    return merged, sources


# --------------------------------------------------------------------------- #
# JSON block replacement
# --------------------------------------------------------------------------- #
def _skip_string(text: str, index: int) -> int:
    i = index + 1
    escaped = False
    while i < len(text):
        char = text[i]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return i
        i += 1
    return len(text) - 1


def _matching_brace(text: str, start: int) -> int | None:
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_string(text, index) + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _top_level_value_span(text: str, key: str) -> tuple[int, int] | None:
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = _skip_string(text, index)
            if depth == 1:
                name = text[index + 1 : end]
                cursor = end + 1
                while cursor < len(text) and text[cursor] in " \t\r\n":
                    cursor += 1
                if name == key and cursor < len(text) and text[cursor] == ":":
                    cursor += 1
                    while cursor < len(text) and text[cursor] in " \t\r\n":
                        cursor += 1
                    if cursor < len(text) and text[cursor] == "{":
                        closing = _matching_brace(text, cursor)
                        if closing is not None:
                            return cursor, closing
            index = end + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        index += 1
    return None


def _render_json_block(text: str, key: str, servers: dict[str, Any]) -> str | None:
    span = _top_level_value_span(text, key)
    if span is None:
        return None
    start, end = span
    line_start = text.rfind("\n", 0, start) + 1
    indent_match = re.match(r"[ \t]*", text[line_start:])
    indent = indent_match.group(0) if indent_match else ""
    lines = json.dumps(servers, ensure_ascii=False, indent=2).splitlines()
    block = "\n".join([lines[0]] + [indent + line for line in lines[1:]])
    return text[:start] + block + text[end + 1 :]


def _append_json_key(text: str, key: str, servers: dict[str, Any]) -> str | None:
    """Insert a missing top-level key before the closing brace, keeping comments."""
    index = len(text) - 1
    while index >= 0 and text[index] in " \t\r\n":
        index -= 1
    if index < 0 or text[index] != "}":
        return None
    line_start = text.rfind("\n", 0, index) + 1
    closing_indent = re.match(r"[ \t]*", text[line_start:index]).group(0)
    inner_indent = closing_indent + "  "
    head = text[:index].rstrip()
    separator = "\n" if head.endswith("{") else ",\n"
    lines = json.dumps(servers, ensure_ascii=False, indent=2).splitlines()
    block = "\n".join([lines[0]] + [inner_indent + line for line in lines[1:]])
    inserted = f'{separator}{inner_indent}"{key}": {block}\n{closing_indent}'
    return head + inserted + text[index:]


# --------------------------------------------------------------------------- #
# TOML
# --------------------------------------------------------------------------- #
def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    raise ConfigError(f"无法写入 TOML 值：{type(value).__name__}")


def dumps_toml_section(schema: str, servers: dict[str, Any]) -> str:
    if not servers:
        return ""
    lines: list[str] = []
    header = _toml_key(schema)
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{name} 的配置必须是对象")
        lines.append(f"[{header}.{_toml_key(str(name))}]")
        for key, value in spec.items():
            if value is None or value == "" or value == []:
                continue
            lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def _toml_section_bounds(lines: list[str], schema: str) -> tuple[int, int] | None:
    """Return [start, end) covering every ``[schema]`` / ``[schema.*]`` header."""
    start: int | None = None
    end: int | None = None
    inside = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
            name = stripped.strip("[]").strip()
            belongs = name == schema or name.startswith(schema + ".")
            if belongs:
                if start is None:
                    start = index
                inside = True
                end = index + 1
                continue
            if inside:
                break
        if inside:
            end = index + 1
    if start is None:
        return None
    assert end is not None
    # Comments directly above the next table belong to that table, not to us.
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return start, end


def replace_toml_section(text: str, schema: str, servers: dict[str, Any]) -> str:
    lines = text.splitlines()
    block = dumps_toml_section(schema, servers)
    bounds = _toml_section_bounds(lines, schema)
    if bounds is None:
        if not block:
            return text
        prefix = text if text.endswith("\n") or not text else text + "\n"
        return prefix + ("\n" if prefix.strip() else "") + block
    start, end = bounds
    head = lines[:start]
    tail = lines[end:]
    while tail and not tail[0].strip():
        tail.pop(0)
    result = head + (block.splitlines() if block else [])
    if block and tail:
        result.append("")
    result += tail
    return "\n".join(result).rstrip("\n") + "\n"


# --------------------------------------------------------------------------- #
# Public writer
# --------------------------------------------------------------------------- #
def write_section(path: Path, fmt: str, schema: str, servers: dict[str, Any]) -> str:
    """Write only the MCP section. Returns created/preserved/appended/rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    candidate: str | None = None
    if original.strip():
        if fmt == "toml":
            candidate = replace_toml_section(original, schema, servers)
        else:
            candidate = _render_json_block(original, schema, servers)
    if candidate is None:
        if fmt == "toml":
            candidate = replace_toml_section(original, schema, servers)
            mode = "appended"
        elif original.strip():
            candidate = _append_json_key(original, schema, servers)
            mode = "appended"
        if candidate is None:
            data = parse_json(original, jsonc=(fmt == "jsonc")) if original.strip() else {}
            data[schema] = servers
            candidate = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            mode = "rewritten"
    else:
        mode = "preserved"

    try:
        check = tomllib.loads(candidate) if fmt == "toml" else parse_json(candidate, jsonc=(fmt == "jsonc"))
    except Exception as exc:
        raise ConfigError(f"写回后配置无法解析，已中止：{exc}") from exc
    if check.get(schema) != servers:
        raise ConfigError("写回校验失败：MCP 区块内容与候选不一致，已中止")

    path.write_text(candidate, encoding="utf-8")
    return "created" if created else mode
