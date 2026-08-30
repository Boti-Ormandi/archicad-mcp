"""Command-line interface for Archicad MCP."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from typing import Any, Never

import aiohttp

from archicad_mcp import __version__
from archicad_mcp.config import (
    DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    DEFAULT_TRANSPORT,
    EXECUTION_MODEL,
    OFFICIAL_PORTS,
    get_runtime_config,
)
from archicad_mcp.core.manager import ConnectionManager
from archicad_mcp.schemas import SchemaCache
from archicad_mcp.schemas.cache_store import CacheStoreError, read_cached_snapshot
from archicad_mcp.schemas.updater import (
    PackagedTapir,
    UpdateOutcome,
    auto_update_enabled,
    load_packaged_tapir,
    reset_schema_cache,
    run_update_check,
    schemas_status,
)

_UPDATE_SUCCESS_STATUSES = frozenset({"updated", "current"})


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        diagnostic = f"{self.prog}: error: {message}\n"
        buffer = getattr(sys.stderr, "buffer", None)
        if buffer is None:
            sys.stderr.write(diagnostic)
            sys.stderr.flush()
        else:
            buffer.write(diagnostic.encode("utf-8"))
            buffer.flush()
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser."""
    parser = _ArgumentParser(prog="archicad-mcp", description="Archicad MCP server and diagnostics")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="serve MCP over stdio")
    serve.add_argument(
        "--transport",
        choices=("stdio",),
        default=DEFAULT_TRANSPORT,
        help="MCP transport (only stdio is supported)",
    )

    doctor = subparsers.add_parser("doctor", help="run read-only local diagnostics")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("setup", help="print client setup instructions")

    config = subparsers.add_parser("config", help="print effective runtime configuration")
    config.add_argument("--json", action="store_true", dest="as_json")

    schemas = subparsers.add_parser("schemas", help="inspect and manage the schema cache")
    schemas_subparsers = schemas.add_subparsers(dest="schemas_command", required=True)
    for name, help_text in (
        ("status", "report local-only packaged/cache/active/check diagnostics"),
        ("update", "run one manual update check honoring offline mode"),
        ("reset", "delete the cached schema snapshot and check state"),
    ):
        command_parser = schemas_subparsers.add_parser(name, help=help_text)
        command_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _serve(transport: str) -> int:
    if transport != "stdio":
        raise ValueError("unsupported transport")
    from archicad_mcp.server import main as serve_stdio

    serve_stdio()
    return 0


def _update_environment_payload() -> dict[str, Any]:
    """Report the strict offline/auto axes without leaking any environment value."""

    offline: bool | None
    try:
        offline = get_runtime_config().schema_update_mode == "offline"
    except ValueError:
        offline = None
    auto_enabled: bool | None
    auto_error: str | None = None
    try:
        auto_enabled = auto_update_enabled()
    except ValueError as exc:
        auto_enabled, auto_error = None, str(exc)
    return {
        "auto_update": {"enabled": auto_enabled, "error": auto_error},
        "offline": offline,
    }


def _config_payload() -> dict[str, Any]:
    # The static runtime facts are environment-independent constants; only the
    # update axes read the environment, and they tolerate invalid values as
    # bounded nulls instead of failing the report.
    return {
        "transport": DEFAULT_TRANSPORT,
        "official_ports": list(OFFICIAL_PORTS),
        "execution_model": EXECUTION_MODEL,
        "default_timeout_seconds": DEFAULT_SCRIPT_TIMEOUT_SECONDS,
        "update_environment": _update_environment_payload(),
    }


def _print_config(as_json: bool) -> int:
    payload = _config_payload()
    if as_json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0

    environment = payload["update_environment"]
    auto = environment["auto_update"]
    sys.stdout.write(
        "\n".join(
            [
                f"transport: {payload['transport']}",
                f"official_ports: {OFFICIAL_PORTS[0]}-{OFFICIAL_PORTS[-1]}",
                f"execution_model: {payload['execution_model']}",
                f"default_timeout_seconds: {payload['default_timeout_seconds']:g}",
                f"update_auto_update_enabled: {json.dumps(auto['enabled'])}",
                f"update_offline: {json.dumps(environment['offline'])}",
            ]
        )
        + "\n"
    )
    return 0


def _print_setup() -> int:
    snippet = {
        "mcpServers": {
            "archicad": {
                "command": "uvx",
                "args": ["archicad-mcp"],
            }
        }
    }
    sys.stdout.write("Add this local stdio server to your MCP client:\n\n")
    sys.stdout.write(json.dumps(snippet, indent=2) + "\n\n")
    sys.stdout.write("Save the configuration and restart or reload the client.\n")
    sys.stdout.write(
        'Verify with get_docs(command="API.GetAllElements"); a successful response has the ID '
        "native:API.GetAllElements and includes the command schema.\n"
    )
    sys.stdout.write(
        "Live tools require this server and Archicad to run on the same computer. Tapir is optional "
        "and is needed only for Tapir commands and get_properties.\n"
    )
    return 0


def _check_runtime_imports() -> dict[str, Any]:
    modules = ("archicad_mcp", "aiohttp", "mcp", "pydantic")
    try:
        for module in modules:
            importlib.import_module(module)
    except Exception as exc:
        return {
            "status": "error",
            "code": "PACKAGE_IMPORT_ERROR",
            "message": (
                f"Runtime import failed ({type(exc).__name__}). "
                "Reinstall archicad-mcp and its runtime dependencies."
            ),
        }
    return {
        "status": "ok",
        "code": "PACKAGE_IMPORT_OK",
        "message": f"Runtime imports are available (archicad-mcp {__version__}, Python {sys.version_info.major}.{sys.version_info.minor}).",
    }


def _check_embedded_schemas() -> dict[str, Any]:
    try:
        cache = SchemaCache()
        cache.load_embedded()
        if not cache.commands:
            raise ValueError("no embedded commands were loaded")
    except Exception as exc:
        return {
            "status": "error",
            "code": "SCHEMA_READ_ERROR",
            "message": (
                f"Embedded schemas are not readable ({type(exc).__name__}). "
                "Reinstall archicad-mcp to restore the embedded schema files."
            ),
        }
    return {
        "status": "ok",
        "code": "SCHEMA_READ_OK",
        "message": f"Embedded schemas are readable ({len(cache.commands)} commands).",
    }


async def _discover_local_archicad() -> dict[str, Any]:
    try:
        timeout = aiohttp.ClientTimeout(total=3.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            manager = ConnectionManager(session)
            await manager.scan_and_connect()
            if not manager.connections:
                return {
                    "status": "warning",
                    "code": "ARCHICAD_NOT_FOUND",
                    "message": (
                        f"No Archicad JSON API was found on official local ports "
                        f"{OFFICIAL_PORTS[0]}-{OFFICIAL_PORTS[-1]}. Start Archicad to use live tools."
                    ),
                    "ports": [],
                    "tapir_ports": [],
                    "native_only_ports": [],
                }

            tapir_ports: list[int] = []
            native_only_ports: list[int] = []
            for port, connection in sorted(manager.connections.items()):
                if await connection.check_tapir():
                    tapir_ports.append(port)
                else:
                    native_only_ports.append(port)
    except Exception as exc:
        return {
            "status": "warning",
            "code": "ARCHICAD_DISCOVERY_ERROR",
            "message": (
                f"Local Archicad discovery could not complete ({type(exc).__name__}). "
                "Check local Archicad JSON API availability and retry."
            ),
        }

    if tapir_ports:
        message = (
            f"Archicad with Tapir is available on port(s): {', '.join(map(str, tapir_ports))}."
        )
        if native_only_ports:
            message += f" Native-only instance(s): {', '.join(map(str, native_only_ports))}."
        return {
            "status": "ok",
            "code": "ARCHICAD_TAPIR_READY",
            "message": message,
            "ports": sorted(tapir_ports + native_only_ports),
            "tapir_ports": tapir_ports,
            "native_only_ports": native_only_ports,
        }

    return {
        "status": "warning",
        "code": "ARCHICAD_NATIVE_ONLY",
        "message": (
            "Archicad is reachable, but Tapir was not detected. Built-in commands are available; "
            "install/enable Tapir for Tapir commands."
        ),
        "ports": native_only_ports,
        "tapir_ports": [],
        "native_only_ports": native_only_ports,
    }


def _check_user_cache() -> dict[str, Any]:
    try:
        result = read_cached_snapshot()
    except Exception as exc:
        return {
            "status": "warning",
            "code": "SCHEMA_CACHE_ERROR",
            "message": (
                f"The user cache schema could not be read ({type(exc).__name__}); "
                "the packaged baseline remains active."
            ),
        }
    if result.error_code is not None:
        return {
            "status": "warning",
            "code": "SCHEMA_CACHE_ERROR",
            "message": (
                f"The cached schema was ignored ({result.error_code}); "
                "the packaged baseline remains active and a later update may heal it."
            ),
        }
    version = None if result.cached is None else result.cached.version
    if version is None:
        message = "No cached schema snapshot is present; the packaged baseline is active."
    else:
        message = f"Cached schema snapshot is readable ({version})."
    return {"status": "ok", "code": "SCHEMA_CACHE_OK", "message": message}


async def _doctor_payload() -> dict[str, Any]:
    checks = [
        _check_runtime_imports(),
        _check_embedded_schemas(),
        _check_user_cache(),
        await _discover_local_archicad(),
    ]
    statuses = {str(check["status"]) for check in checks}
    if "error" in statuses:
        status = "error"
    elif "warning" in statuses:
        status = "warning"
    else:
        status = "ok"
    return {"status": status, "checks": checks}


def _print_doctor(as_json: bool) -> int:
    payload = asyncio.run(_doctor_payload())
    if as_json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"doctor: {payload['status']}\n")
        for check in payload["checks"]:
            label = str(check["status"]).upper()
            sys.stdout.write(f"[{label}] {check['code']}: {check['message']}\n")
    return 1 if payload["status"] == "error" else 0


def _packaged_for_cli() -> PackagedTapir:
    return load_packaged_tapir()


def _emit_payload(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0
    sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return 0


def _print_schemas_status(as_json: bool) -> int:
    return _emit_payload(schemas_status(_packaged_for_cli()), as_json)


async def _manual_update_outcome() -> UpdateOutcome:
    # No session is opened here: offline/lease/TTL refusals happen before the
    # updater creates its own bounded session for an actual network attempt.
    return await run_update_check(_packaged_for_cli(), manual=True)


def _print_schemas_update(as_json: bool) -> int:
    outcome = asyncio.run(_manual_update_outcome())
    payload = {"status": outcome.status, "error": outcome.error}
    _emit_payload(payload, as_json)
    return 0 if outcome.status in _UPDATE_SUCCESS_STATUSES else 1


def _print_schemas_reset(as_json: bool) -> int:
    try:
        removed = reset_schema_cache()
    except CacheStoreError as exc:
        _emit_payload({"status": "failed", "error": exc.code}, as_json)
        return 1
    _emit_payload({"status": "reset", "removed": sorted(removed)}, as_json)
    return 0


def _print_schemas(schemas_command: str, as_json: bool) -> int:
    if schemas_command == "status":
        return _print_schemas_status(as_json)
    if schemas_command == "update":
        return _print_schemas_update(as_json)
    if schemas_command == "reset":
        return _print_schemas_reset(as_json)
    raise RuntimeError(f"unhandled schemas command: {schemas_command}")


def main(argv: list[str] | None = None) -> int:
    """Run the Archicad MCP CLI."""
    args = build_parser().parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        return _serve(getattr(args, "transport", DEFAULT_TRANSPORT))
    if command == "doctor":
        return _print_doctor(bool(args.as_json))
    if command == "setup":
        return _print_setup()
    if command == "config":
        return _print_config(bool(args.as_json))
    if command == "schemas":
        return _print_schemas(str(args.schemas_command), bool(args.as_json))
    raise RuntimeError(f"unhandled command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
