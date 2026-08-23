"""Pydantic models for MCP tool inputs/outputs."""

from typing import Any, Literal

from pydantic import BaseModel


class ArchicadInstance(BaseModel):
    """Information about a running Archicad instance."""

    port: int
    project_name: str
    project_path: str | None
    project_type: Literal["solo", "teamwork", "untitled"]
    archicad_version: str
    is_tapir_available: bool
    tapir_version: str | None = None


ScriptErrorCode = Literal[
    "syntax_error",
    "runtime_error",
    "timeout",
    "worker_start",
    "worker_exit",
    "worker_protocol",
    "result_not_json",
]


class ScriptResult(BaseModel):
    """Result of same-account local-user script execution."""

    success: bool
    result: Any | None
    stdout: str
    stderr: str = ""
    error: str | None
    error_code: ScriptErrorCode | None = None
    execution_model: Literal["local_user"] = "local_user"
    execution_time_ms: int


class CommandDoc(BaseModel):
    """Documentation for a single command."""

    name: str
    api: Literal["builtin", "tapir"]
    category: str
    description: str
    parameters_schema: dict[str, Any] | None = None
    returns_schema: dict[str, Any] | None = None
    examples: list[dict[str, Any]] = []


class DocSearchResult(BaseModel):
    """Result of documentation search."""

    total: int
    commands: list[CommandDoc]
    categories: list[str] | None = None
