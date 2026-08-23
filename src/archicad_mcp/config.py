"""Runtime defaults shared by the server and command-line interface."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Literal, cast

DEFAULT_TRANSPORT: Literal["stdio"] = "stdio"
OFFICIAL_PORTS = tuple(range(19723, 19744))
EXECUTION_MODEL: Literal["local_user"] = "local_user"
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 300.0
SchemaUpdateMode = Literal["automatic", "offline"]


@dataclass(frozen=True)
class RuntimeConfig:
    """Effective nonsecret runtime facts."""

    transport: Literal["stdio"] = DEFAULT_TRANSPORT
    official_ports: tuple[int, ...] = OFFICIAL_PORTS
    execution_model: Literal["local_user"] = EXECUTION_MODEL
    default_timeout_seconds: float = DEFAULT_SCRIPT_TIMEOUT_SECONDS
    schema_update_mode: SchemaUpdateMode = "automatic"


def _schema_update_mode_from_env() -> SchemaUpdateMode:
    """Read ARCHICAD_MCP_OFFLINE using the strict grammar: unset/"0" or "1"."""
    value = os.environ.get("ARCHICAD_MCP_OFFLINE")
    if value is None or value == "0":
        return "automatic"
    if value == "1":
        return "offline"
    raise ValueError("ARCHICAD_MCP_OFFLINE must be exactly '0' or '1'")


def get_runtime_config() -> RuntimeConfig:
    """Return the effective runtime configuration."""
    return RuntimeConfig(schema_update_mode=_schema_update_mode_from_env())


def validate_script_timeout(value: object) -> float | None:
    """Validate a public script timeout without imposing an arbitrary maximum."""
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("timeout_seconds must be null or a positive finite number")
    try:
        timeout = float(cast(int | float, value))
    except OverflowError as exc:
        raise ValueError("timeout_seconds must be null or a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be null or a positive finite number")
    return timeout
