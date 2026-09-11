"""WebMCP tool schema definitions (Chrome WebMCP-compatible).

Based on Chrome's WebMCP API: tools have name, description, input_schema (JSON Schema),
and annotations (readOnlyHint, idempotentHint, destructiveHint, openWorldHint).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolAnnotation:
    readOnlyHint: bool = False
    idempotentHint: bool = False
    destructiveHint: bool = False
    openWorldHint: bool = False


@dataclass
class WebMCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotation = field(default_factory=ToolAnnotation)

    @classmethod
    def from_dict(cls, d: dict) -> "WebMCPTool":
        ann = d.get("annotations", {})
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            input_schema=d.get("input_schema", {}),
            annotations=ToolAnnotation(
                readOnlyHint=ann.get("readOnlyHint", False),
                idempotentHint=ann.get("idempotentHint", False),
                destructiveHint=ann.get("destructiveHint", False),
                openWorldHint=ann.get("openWorldHint", False),
            ),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.annotations.readOnlyHint,
                "idempotentHint": self.annotations.idempotentHint,
                "destructiveHint": self.annotations.destructiveHint,
                "openWorldHint": self.annotations.openWorldHint,
            },
        }

    @property
    def is_read_only(self) -> bool:
        return self.annotations.readOnlyHint

    @property
    def is_destructive(self) -> bool:
        return self.annotations.destructiveHint

    @property
    def is_idempotent(self) -> bool:
        return self.annotations.idempotentHint


@dataclass
class WebMCPSite:
    origin: str  # e.g. "example.com"
    tools: list[WebMCPTool] = field(default_factory=list)
    discovered_at: float = 0.0

    def tool_by_name(self, name: str) -> WebMCPTool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ToolCall":
        return cls(tool=d["tool"], args=d.get("arguments", {}), request_id=d.get("request_id", ""))

    def to_dict(self) -> dict:
        return {"tool": self.tool, "arguments": self.args, "request_id": self.request_id}


@dataclass
class ToolResult:
    request_id: str
    ok: bool
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"request_id": self.request_id, "ok": self.ok}
        if self.ok:
            d["result"] = self.result
        else:
            d["error"] = self.error or "unknown error"
        return d
