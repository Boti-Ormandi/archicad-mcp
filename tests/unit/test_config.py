"""Unit tests for runtime configuration."""

from __future__ import annotations

import math

import pytest

from archicad_mcp.config import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    OFFICIAL_PORTS,
    get_runtime_config,
    validate_script_timeout,
)


def test_runtime_config_is_minimal_local_user_and_automatic_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    config = get_runtime_config()

    assert config.transport == "stdio"
    assert config.execution_model == "local_user"
    assert config.default_timeout_seconds == 300.0
    assert config.schema_update_mode == "automatic"
    assert not any(
        token in field
        for field in config.__dataclass_fields__
        for token in ("security", "sandbox", "approval", "blocked", "allowed")
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", "automatic"), ("1", "offline")],
)
def test_offline_switch_has_strict_boolean_grammar(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: str,
) -> None:
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", value)
    assert get_runtime_config().schema_update_mode == expected


@pytest.mark.parametrize("value", ["", "true", "false", "yes", "2", " 1", "1 "])
def test_offline_switch_rejects_all_other_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", value)
    with pytest.raises(ValueError, match="must be exactly '0' or '1'"):
        get_runtime_config()


def test_official_ports_are_exact() -> None:
    assert tuple(range(19723, 19744)) == OFFICIAL_PORTS
    assert 19743 in OFFICIAL_PORTS
    assert 19744 not in OFFICIAL_PORTS


def test_default_timeout_is_finite() -> None:
    assert DEFAULT_SCRIPT_TIMEOUT_SECONDS == 300.0
    assert math.isfinite(DEFAULT_SCRIPT_TIMEOUT_SECONDS)


@pytest.mark.parametrize("value", [True, False, 0, -1, math.inf, -math.inf, math.nan, "5"])
def test_timeout_rejects_invalid_public_values(value: object) -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        validate_script_timeout(value)


def test_timeout_accepts_positive_values_without_arbitrary_cap() -> None:
    assert validate_script_timeout(0.001) == 0.001
    assert validate_script_timeout(1_000_000) == 1_000_000.0
    assert validate_script_timeout(None) is None
