"""Data model shared by CLI, GUI and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class Profile:
    """One Agent's MCP config: where it lives and how it shapes entries."""

    id: str
    name: str
    config: str
    format: str = "json"
    schema: str = "mcpServers"
    command: str = "string"  # "string" | "array"
    env: str = "env"
    transport: str = ""  # "" | "type"
    stdio_type: str = "stdio"
    http_type: str = "http"
    url_key: str = "url"
    enabled: bool = False
    tools: bool = False
    readonly: bool = False
    aliases: list[str] = field(default_factory=list)
    notes: str = ""
    source: str = ""

    @property
    def path(self) -> Path:
        return paths.expand_path(self.config)

    @property
    def display_path(self) -> str:
        return str(self.path)

    def matches(self, value: str) -> bool:
        wanted = value.strip().lower()
        return wanted in {self.id.lower(), self.name.lower(), *(a.lower() for a in self.aliases)}

    def __post_init__(self) -> None:
        if self.format not in {"json", "jsonc", "toml"}:
            raise ValueError(f"{self.id}: unsupported format {self.format!r}")
        if self.command not in {"string", "array"}:
            raise ValueError(f"{self.id}: command must be 'string' or 'array'")
        if self.transport not in {"", "type"}:
            raise ValueError(f"{self.id}: transport must be '' or 'type'")


@dataclass
class Candidate:
    """A reviewed, not-yet-applied copy of one Agent's MCP section."""

    profile: Profile
    target: Path
    servers: dict[str, Any]
    source_sha256: str
    generated_at: str
    path: Path
