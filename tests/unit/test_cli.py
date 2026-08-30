"""Unit tests for the Archicad MCP command-line interface."""

from __future__ import annotations

import importlib
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

import archicad_mcp.schemas.cache as schema_cache_module
from archicad_mcp import __version__, cli
from archicad_mcp.core.manager import ConnectionManager
from archicad_mcp.schemas import SchemaCache
from archicad_mcp.schemas import cache_store as cs
from archicad_mcp.schemas.cache_store import atomically_replace
from archicad_mcp.schemas.updater import UpdateOutcome
from tests.unit.test_direct_updater import cache_document_bytes


@pytest.fixture(autouse=True)
def isolated_cache_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the OS user-cache root at a per-test directory."""
    root = tmp_path / "cache-root"
    root.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    return root


@pytest.fixture(autouse=True)
def clean_update_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    monkeypatch.delenv("ARCHICAD_MCP_AUTO_UPDATE", raising=False)


def forbid_network_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoNetwork:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("this command must never open a network session")

    monkeypatch.setattr(aiohttp, "ClientSession", _NoNetwork)


def test_no_command_and_explicit_stdio_serve_use_same_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_serve(transport: str) -> int:
        calls.append(transport)
        return 17

    monkeypatch.setattr(cli, "_serve", fake_serve)

    assert cli.main([]) == 17
    assert cli.main(["serve"]) == 17
    assert cli.main(["serve", "--transport", "stdio"]) == 17
    assert calls == ["stdio", "stdio", "stdio"]


def test_transport_rejection_is_parser_exit_2_stderr_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["serve", "--transport", "sse"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert captured.out == ""
    assert captured.err.endswith("\n")
    assert "\r" not in captured.err
    assert captured.err.count("\n") == 1
    assert "invalid choice" in captured.err


def test_version_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as version_exit:
        cli.main(["--version"])
    version_output = capsys.readouterr()
    assert version_exit.value.code == 0
    assert version_output.out == f"archicad-mcp {__version__}\n"
    assert version_output.err == ""

    with pytest.raises(SystemExit) as help_exit:
        cli.main(["--help"])
    help_output = capsys.readouterr()
    assert help_exit.value.code == 0
    assert "{serve,doctor,setup,config,schemas}" in help_output.out
    assert help_output.err == ""


def test_doctor_json_reports_explicit_no_archicad(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def no_archicad() -> dict[str, object]:
        return {
            "status": "warning",
            "code": "ARCHICAD_NOT_FOUND",
            "message": "No Archicad found for test.",
            "ports": [],
            "tapir_ports": [],
            "native_only_ports": [],
        }

    monkeypatch.setattr(cli, "_discover_local_archicad", no_archicad)

    assert cli.main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "warning"
    assert [check["code"] for check in payload["checks"]] == [
        "PACKAGE_IMPORT_OK",
        "SCHEMA_READ_OK",
        "SCHEMA_CACHE_OK",
        "ARCHICAD_NOT_FOUND",
    ]


def test_doctor_surfaces_corrupt_user_cache_without_failing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cs.schema_cache_dir().mkdir(parents=True)
    cs.snapshot_path().write_bytes(b"{corrupt garbage")

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    cache_check = payload["checks"][2]
    assert cache_check["code"] == "SCHEMA_CACHE_ERROR"
    assert cache_check["status"] == "warning"
    assert payload["status"] == "warning"
    assert b"{corrupt garbage" not in capsys.readouterr().out.encode()


@pytest.mark.parametrize("cached_version", ["1.5.8", "1.0.0"])
def test_doctor_reports_cached_snapshot_readable_without_claiming_active(
    cached_version: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An equal-or-older cache is readable but not necessarily the active selection."""

    document_bytes, _inputs = cache_document_bytes(version=cached_version)
    cs.schema_cache_dir().mkdir(parents=True)
    atomically_replace(cs.snapshot_path(), document_bytes)

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    cache_check = payload["checks"][2]
    assert cache_check["code"] == "SCHEMA_CACHE_OK"
    assert cache_check["status"] == "ok"
    message = str(cache_check["message"])
    assert cached_version in message
    assert "active" not in message.lower()


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


def test_setup_is_output_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = tmp_path / "client.json"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = _file_snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["setup"]) == 0

    output = capsys.readouterr().out
    after = _file_snapshot(tmp_path)
    assert before == after
    json_start = output.index("{")
    snippet, _end = json.JSONDecoder().raw_decode(output[json_start:])
    assert snippet == {
        "mcpServers": {
            "archicad": {
                "command": "uvx",
                "args": ["archicad-mcp"],
            }
        }
    }


def test_config_json_is_read_only_exact_and_has_no_gate_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_network_sessions(monkeypatch)
    sentinel = tmp_path / "runtime.conf"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = _file_snapshot(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["config", "--json"]) == 0

    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    after = _file_snapshot(tmp_path)
    assert before == after
    assert payload == {
        "default_timeout_seconds": 300.0,
        "execution_model": "local_user",
        "official_ports": list(range(19723, 19744)),
        "update_environment": {
            "auto_update": {"enabled": True, "error": None},
            "offline": False,
        },
        "transport": "stdio",
    }
    serialized = json.dumps(payload).lower()
    for forbidden in (
        "security",
        "sandbox",
        "approval",
        "confirmation",
        "audit",
        "blocked",
        "manifest_url",
        "trusted_key",
    ):
        assert forbidden not in serialized


def test_config_reports_offline_and_disabled_auto_axes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "1")
    monkeypatch.setenv("ARCHICAD_MCP_AUTO_UPDATE", "0")

    assert cli.main(["config", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    environment = payload["update_environment"]
    assert environment["offline"] is True
    assert environment["auto_update"] == {"enabled": False, "error": None}


def test_config_reports_invalid_update_environment_as_bounded_nulls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "yes")
    monkeypatch.setenv("ARCHICAD_MCP_AUTO_UPDATE", "2")

    assert cli.main(["config", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    environment = payload["update_environment"]
    assert environment["offline"] is None
    assert environment["auto_update"]["enabled"] is None
    assert (
        environment["auto_update"]["error"] == "ARCHICAD_MCP_AUTO_UPDATE must be exactly '0' or '1'"
    )
    assert "yes" not in capsys.readouterr().out


def test_schemas_group_requires_a_known_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as missing:
        cli.main(["schemas"])
    assert missing.value.code == 2
    assert capsys.readouterr().out == ""

    with pytest.raises(SystemExit) as unknown:
        cli.main(["schemas", "reseed"])
    assert unknown.value.code == 2


def test_schemas_status_is_local_only_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_network_sessions(monkeypatch)

    assert cli.main(["schemas", "status", "--json"]) == 0

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["packaged_version"] == "1.5.8"
    assert payload["active"] == {"origin": "packaged", "version": "1.5.8"}
    assert payload["cache"]["present"] is False
    assert payload["cache"]["error"] is None
    assert payload["check_state"]["in_flight"] is False
    assert set(payload) == {
        "active",
        "auto_update",
        "cache",
        "check_state",
        "offline",
        "packaged_version",
    }


def test_schemas_status_never_networks_even_with_stale_ttl_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_network_sessions(monkeypatch)
    cs.schema_cache_dir().mkdir(parents=True)
    cs.store_check_state(cs.CheckState(release_etag='"stale"'))

    assert cli.main(["schemas", "status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["check_state"]["etag_present"] is True


def test_schemas_status_surfaces_corrupt_cache_as_packaged_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cs.schema_cache_dir().mkdir(parents=True)
    cs.snapshot_path().write_bytes(b"{corrupt")

    assert cli.main(["schemas", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["cache"]["present"] is False
    assert payload["cache"]["error"] == "malformed_tapir_json"
    assert payload["active"]["origin"] == "packaged"


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        pytest.param("updated", 0, id="updated-succeeds"),
        pytest.param("current", 0, id="current-succeeds"),
        pytest.param("offline-network-forbidden", 1, id="offline-refused"),
        pytest.param("in-flight", 1, id="lease-busy-refused"),
        pytest.param("upstream-rollback", 1, id="rollback-refused"),
        pytest.param("upstream-equivocation", 1, id="equivocation-refused"),
        pytest.param("auto-disabled", 1, id="disabled-is-nonzero"),
        pytest.param("failed", 1, id="failed-is-nonzero"),
    ],
)
def test_schemas_update_exit_code_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected_exit: int,
) -> None:
    forbid_network_sessions(monkeypatch)

    async def fixed_outcome() -> UpdateOutcome:
        return UpdateOutcome(cast(Any, status), None if expected_exit == 0 else "bounded-code")

    monkeypatch.setattr(cli, "_manual_update_outcome", fixed_outcome)

    assert cli.main(["schemas", "update", "--json"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": status, "error": None if expected_exit == 0 else "bounded-code"}


def test_schemas_update_honors_offline_before_any_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_network_sessions(monkeypatch)
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "1")

    assert cli.main(["schemas", "update", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "offline-network-forbidden", "error": None}
    assert not cs.snapshot_path().exists()


def test_schemas_update_reports_invalid_environment_as_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    forbid_network_sessions(monkeypatch)
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "maybe")

    assert cli.main(["schemas", "update", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"] == "ARCHICAD_MCP_OFFLINE must be exactly '0' or '1'"
    assert "maybe" not in capsys.readouterr().out


def test_schemas_reset_deletes_cached_state_but_keeps_permanent_lock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cs.schema_cache_dir().mkdir(parents=True)
    cs.snapshot_path().write_bytes(b"{stale snapshot")
    cs.store_check_state(cs.CheckState(last_outcome="updated", release_etag='"e"'))
    permanent_lock = cs.acquire_cache_lock(timeout=10.0)
    cs.release_cache_lock(permanent_lock)
    assert cs.cache_lock_path().exists()

    assert cli.main(["schemas", "reset", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "reset"
    assert payload["removed"] == ["check-state.json", "tapir.json"]
    assert not cs.snapshot_path().exists()
    assert not cs.check_state_path().exists()
    assert cs.cache_lock_path().exists(), "the permanent lock file is never deleted"
    assert cs.read_cached_snapshot().cached is None, "reset is the intentional downgrade"


def test_schemas_reset_reports_bounded_lock_busy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cs.schema_cache_dir().mkdir(parents=True)
    started = threading.Event()
    release_holder = threading.Event()

    def hold_native_lock() -> None:
        held = cs.acquire_cache_lock(timeout=30.0)
        try:
            started.set()
            release_holder.wait(15.0)
        finally:
            cs.release_cache_lock(held)

    holder = threading.Thread(target=hold_native_lock, daemon=True)
    holder.start()
    try:
        assert started.wait(10.0)
        began = time.monotonic()
        assert cli.main(["schemas", "reset", "--json"]) == 1
        elapsed = time.monotonic() - began
        assert elapsed < 15.0
    finally:
        release_holder.set()
        holder.join(timeout=15.0)

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "failed", "error": "lock-busy"}
    # Once the holder releases, reset succeeds.
    assert cli.main(["schemas", "reset"]) == 0


def test_doctor_json_does_not_expose_import_or_schema_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = r"C:\Users\private\project\schema.json"

    def fail_import(_: str) -> object:
        raise ImportError(secret)

    def fail_schema(_: object) -> None:
        raise OSError(secret)

    async def no_archicad() -> dict[str, object]:
        return {
            "status": "warning",
            "code": "ARCHICAD_NOT_FOUND",
            "message": "No Archicad found for test.",
            "ports": [],
            "tapir_ports": [],
            "native_only_ports": [],
        }

    monkeypatch.setattr(importlib, "import_module", fail_import)
    monkeypatch.setattr(SchemaCache, "load_embedded", fail_schema)
    monkeypatch.setattr(cli, "_discover_local_archicad", no_archicad)

    assert cli.main(["doctor", "--json"]) == 1

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert secret not in output
    assert payload["checks"][0]["code"] == "PACKAGE_IMPORT_ERROR"
    assert "ImportError" in payload["checks"][0]["message"]
    assert payload["checks"][1]["code"] == "SCHEMA_READ_ERROR"
    assert "OSError" in payload["checks"][1]["message"]


def test_doctor_missing_embedded_schema_warnings_do_not_expose_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema_dir = Path(schema_cache_module.__file__).resolve().parent
    tapir_path = schema_dir / "tapir.json"
    builtin_path = schema_dir / "builtin.json"
    original_exists = Path.exists

    def hide_embedded_schemas(path: Path) -> bool:
        if path.name in {"tapir.json", "builtin.json"}:
            return False
        return original_exists(path)

    async def no_archicad() -> dict[str, object]:
        return {
            "status": "warning",
            "code": "ARCHICAD_NOT_FOUND",
            "message": "No Archicad found for test.",
            "ports": [],
            "tapir_ports": [],
            "native_only_ports": [],
        }

    monkeypatch.setattr(Path, "exists", hide_embedded_schemas)
    monkeypatch.setattr(cli, "_discover_local_archicad", no_archicad)

    logger = schema_cache_module.logger
    original_level = logger.level
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        assert cli.main(["doctor", "--json"]) == 1
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)

    captured = capsys.readouterr()
    for output in (captured.out, captured.err):
        assert str(schema_dir) not in output
        assert str(tapir_path) not in output
        assert str(builtin_path) not in output
    assert "Embedded Tapir schema is missing" in captured.err
    assert "Embedded builtin schema is missing" in captured.err


async def test_discovery_error_does_not_expose_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = r"C:\Users\private\Archicad\connection.json"

    async def fail_scan(_: object) -> None:
        raise OSError(secret)

    monkeypatch.setattr(ConnectionManager, "scan_and_connect", fail_scan)

    check = await cli._discover_local_archicad()
    assert check["code"] == "ARCHICAD_DISCOVERY_ERROR"
    assert "OSError" in str(check["message"])
    assert secret not in str(check["message"])
