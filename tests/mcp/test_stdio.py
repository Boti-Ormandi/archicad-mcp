"""Subprocess tests for the installed MCP stdio console entry point."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import sysconfig
import threading
from contextlib import suppress
from importlib.metadata import Distribution, distribution
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _editable_source(dist: Distribution) -> Path | None:
    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        return None
    direct_url = json.loads(direct_url_text)
    if not direct_url.get("dir_info", {}).get("editable", False):
        return None
    parsed = urlparse(direct_url["url"])
    assert parsed.scheme == "file"
    return Path(url2pathname(parsed.path)).resolve()


def _active_project_console_script() -> Path:
    """Return the exact console script installed by this environment's distribution."""
    dist = distribution("archicad-mcp")
    entry_points = [ep for ep in dist.entry_points if ep.name == "archicad-mcp"]
    assert len(entry_points) == 1
    assert entry_points[0].group == "console_scripts"
    assert entry_points[0].value == "archicad_mcp.cli:main"

    script_name = "archicad-mcp.exe" if os.name == "nt" else "archicad-mcp"
    scripts_dir = Path(sysconfig.get_path("scripts")).resolve()
    script = (scripts_dir / script_name).resolve()
    assert script.is_file()
    assert _is_within(script, Path(sys.prefix).resolve())

    editable_source = _editable_source(dist)
    if editable_source is not None:
        assert editable_source == _PROJECT_ROOT
    else:
        assert _is_within(Path(str(dist.locate_file(""))).resolve(), Path(sys.prefix).resolve())
    return script


def _isolated_child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["ARCHICAD_MCP_OFFLINE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "protocol_version"),
    [("auto", "2026-07-28"), ("legacy", "2025-11-25")],
)
async def test_installed_console_serves_modern_and_legacy_clients(
    mode: str,
    protocol_version: str,
    tmp_path: Path,
) -> None:
    """Public SDK clients exercise the exact active console script in both protocol eras."""
    parameters = StdioServerParameters(
        command=str(_active_project_console_script()),
        cwd=tmp_path,
        env={
            "ARCHICAD_MCP_OFFLINE": "1",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
        },
    )

    stderr_path = tmp_path / "server-stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as stderr:
        async with Client(
            stdio_client(parameters, errlog=stderr),
            mode=mode,
            read_timeout_seconds=30,
        ) as client:
            assert client.protocol_version == protocol_version

            tools = await client.list_tools()
            expected_names = {"list_instances", "get_docs", "get_properties", "execute_script"}
            assert {tool.name for tool in tools.tools} == expected_names
            assert all(tool.output_schema is not None for tool in tools.tools)

            instances = await client.call_tool("list_instances")
            assert not instances.is_error
            result = instances.structured_content["result"]
            assert isinstance(result, list)
            for entry in result:
                assert {
                    "port",
                    "project_name",
                    "project_type",
                    "archicad_version",
                    "is_tapir_available",
                    "tapir_version",
                } <= set(entry)

            docs = await client.call_tool("get_docs")
            assert not docs.is_error
            assert docs.structured_content is not None
            # The projection reflects whatever this machine's discovery found;
            # each legitimate state keeps its exact documented catalog counts.
            observed_status = docs.structured_content["status"]
            if observed_status == "tapir_unavailable":
                assert docs.structured_content["provider_counts"] == {"native": 73, "tapir": 0}
                assert docs.structured_content["total"] == 73
            else:
                assert observed_status in {"compatibility_unknown", "tapir_available"}
                assert docs.structured_content["provider_counts"] == {
                    "native": 73,
                    "tapir": 236,
                }
                assert docs.structured_content["total"] == 309

            first_success = await client.call_tool("get_docs", {"command": "API.GetAllElements"})
            assert not first_success.is_error
            assert first_success.structured_content is not None
            assert first_success.structured_content["id"] == "native:API.GetAllElements"
            assert "command" in first_success.structured_content
            assert "$defs" in first_success.structured_content

            # Negative targeting is asserted only when discovery found nothing,
            # so the test stays hermetic on machines with live Archicad and
            # never sends operations toward a running model.
            if not result:
                missing_instance = await client.call_tool("get_properties", {"port": 19723})
                assert missing_instance.is_error
                assert missing_instance.structured_content is None

                missing_script_target = await client.call_tool(
                    "execute_script",
                    {"port": 19723, "script": "return 1"},
                )
                assert missing_script_target.is_error
                assert missing_script_target.structured_content is None

        stderr.seek(0)
        assert "Tool already exists: execute_script" not in stderr.read()


def test_installed_console_raw_legacy_framing_eof_and_stdout_cleanliness(
    tmp_path: Path,
) -> None:
    """The exact console launcher emits only framed JSON and exits after stdin EOF."""
    protocol_version = "2024-11-05"
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "test"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    process = subprocess.Popen(
        [_active_project_console_script()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=tmp_path,
        env=_isolated_child_environment(),
    )
    stdin = process.stdin
    stdout = process.stdout
    stderr_stream = process.stderr
    assert stdin is not None
    assert stdout is not None
    assert stderr_stream is not None

    stdout_lines: list[str] = []
    stderr_parts: list[str] = []
    responses: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in stdout:
                stdout_lines.append(line)
                responses.put(line)
        finally:
            responses.put(None)

    def read_stderr() -> None:
        stderr_parts.append(stderr_stream.read())

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    failure: Exception | None = None
    initialize_response: dict[str, Any] | None = None
    forced_cleanup = False
    try:
        stdin.write(json.dumps(messages[0]) + "\n")
        stdin.flush()
        response_line = responses.get(timeout=30)
        if response_line is None:
            raise EOFError("stdout closed before the initialize response")
        initialize_response = json.loads(response_line)

        for message in messages[1:]:
            stdin.write(json.dumps(message) + "\n")
        stdin.flush()
        stdin.close()
        process.wait(timeout=30)
    except Exception as caught:
        failure = caught
    finally:
        if not stdin.closed:
            try:
                stdin.close()
            except OSError as caught:
                if failure is None:
                    failure = caught
        if failure is not None and process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        if process.poll() is None:
            forced_cleanup = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    stderr = "".join(stderr_parts)
    if failure is not None:
        raise AssertionError(
            f"console exchange failed ({type(failure).__name__}: {failure}); "
            f"returncode={process.returncode}; forced_cleanup={forced_cleanup}; "
            f"stdout={''.join(stdout_lines)!r}; stderr={stderr!r}"
        ) from failure

    wire_messages: list[dict[str, Any]] = [json.loads(line) for line in stdout_lines]
    assert process.returncode == 0, stderr
    assert initialize_response is not None
    assert not stdout_thread.is_alive()
    assert not stderr_thread.is_alive()
    assert len(wire_messages) == 2
    assert wire_messages[0] == initialize_response
    assert [message["id"] for message in wire_messages] == [1, 2]
    assert wire_messages[0]["result"]["protocolVersion"] == protocol_version
    assert {tool["name"] for tool in wire_messages[1]["result"]["tools"]} == {
        "list_instances",
        "get_docs",
        "get_properties",
        "execute_script",
    }
