"""Unit tests for disposable local-user script execution."""

from __future__ import annotations

import asyncio
import io
import socket
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web

from archicad_mcp.scripting import worker as worker_module
from archicad_mcp.scripting.executor import ScriptExecutor


@pytest.fixture
def executor() -> ScriptExecutor:
    return ScriptExecutor()


@pytest.fixture
async def fake_archicad_port() -> AsyncIterator[int]:
    async def handle(request: web.Request) -> web.Response:
        payload = await request.json()
        command = payload["command"]
        if command == "API.GetProductInfo":
            return web.json_response({"succeeded": True, "result": {"version": "test-version"}})
        if command == "API.ExecuteAddOnCommand":
            parameters = payload["parameters"]["addOnCommandParameters"]
            return web.json_response(
                {"succeeded": True, "result": {"addOnCommandResponse": {"echo": parameters}}}
            )
        return web.json_response(
            {"succeeded": False, "error": {"code": 1, "message": "unexpected command"}}
        )

    app = web.Application()
    app.router.add_post("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = int(sock.getsockname()[1])
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        yield port
    finally:
        await runner.cleanup()


async def test_ordinary_result_async_execution_and_full_imports(executor: ScriptExecutor) -> None:
    script = """
import asyncio
import hashlib
import os
await asyncio.sleep(0)
result = {"value": 21 * 2, "hash": hashlib.sha256(b"x").hexdigest(), "sep": os.sep}
"""
    response = await executor.run(script, 19723)

    assert response.success is True
    assert response.result == {
        "value": 42,
        "hash": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
        "sep": "\\" if sys.platform == "win32" else "/",
    }
    assert response.execution_model == "local_user"
    assert response.error_code is None


async def test_captures_stdout_and_stderr(executor: ScriptExecutor) -> None:
    response = await executor.run(
        'import os, sys\nos.write(1, b"raw stdout\\n")\nos.write(2, b"raw stderr\\n")\nprint("captured stdout")\nprint("captured stderr", file=sys.stderr)\nresult = 1',
        19723,
    )

    assert response.success is True
    assert response.stdout.splitlines() == ["raw stdout", "captured stdout"]
    assert response.stderr.splitlines() == ["raw stderr", "captured stderr"]


async def test_json_result_is_not_arbitrarily_truncated(executor: ScriptExecutor) -> None:
    response = await executor.run("result = list(range(1000))", 19723)

    assert response.success is True
    assert response.result == list(range(1000))


async def test_non_json_result_is_structured_failure(executor: ScriptExecutor) -> None:
    response = await executor.run("result = {1, 2, 3}", 19723)

    assert response.success is False
    assert response.result is None
    assert response.error_code == "result_not_json"
    assert response.error == "Script result is not JSON-compatible"
    assert response.execution_model == "local_user"


@pytest.mark.parametrize(
    ("script", "error_code", "needle"),
    [
        ("x =\n", "syntax_error", "SyntaxError"),
        ("value = 1 / 0\nresult = value", "runtime_error", "ZeroDivisionError"),
    ],
)
async def test_script_errors_have_useful_lines_without_internal_paths(
    executor: ScriptExecutor,
    script: str,
    error_code: str,
    needle: str,
) -> None:
    response = await executor.run(script, 19723)

    assert response.success is False
    assert response.error_code == error_code
    assert response.error is not None
    assert needle in response.error
    assert "Line 1" in response.error
    assert "worker.py" not in response.error
    assert "executor.py" not in response.error
    assert "archicad-mcp-simplified-integration" not in response.error


async def test_default_override_and_disabled_timeout_contract(executor: ScriptExecutor) -> None:
    assert executor.default_timeout_seconds == 300.0

    response = await executor.run(
        "import asyncio\nawait asyncio.sleep(0.01)\nresult = 'done'",
        19723,
        timeout_seconds=5.0,
    )
    disabled = await executor.run("result = 'no deadline'", 19723, timeout_seconds=None)

    assert response.success is True
    assert response.result == "done"
    assert disabled.success is True
    assert disabled.result == "no deadline"


async def test_single_cpu_loop_times_out_and_owned_worker_is_cleaned(
    executor: ScriptExecutor,
) -> None:
    response = await executor.run("while True:\n    pass", 19723, timeout_seconds=0.1)

    assert response.success is False
    assert response.error_code == "timeout"
    assert response.error == "Script timed out after 0.1 seconds"
    assert response.execution_time_ms < 5000


async def test_cancellation_terminates_exact_owned_child(
    executor: ScriptExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = asyncio.create_subprocess_exec
    owned: list[asyncio.subprocess.Process] = []
    child_started = asyncio.Event()

    async def capture_create(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_create(*args, **kwargs)
        owned.append(process)
        child_started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_create)
    task = asyncio.create_task(
        executor.run(
            "import asyncio\nawait asyncio.sleep(60)\nresult = 'late'",
            19723,
            timeout_seconds=None,
        )
    )
    await asyncio.wait_for(child_started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(owned) == 1
    assert owned[0].returncode is not None


async def test_worker_start_failure_is_structured_without_os_details(
    executor: ScriptExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = r"E:\private\python.exe"

    async def fail_create(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        raise FileNotFoundError(secret)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_create)

    response = await executor.run("result = 1", 19723)

    assert response.success is False
    assert response.error_code == "worker_start"
    assert response.error == "Script worker could not be started"
    assert response.stdout == ""
    assert response.stderr == ""
    assert secret not in response.error


async def test_surrogate_source_returns_structured_script_failure(executor: ScriptExecutor) -> None:
    script = "raise ValueError('\ud800')"

    response = await executor.run(script, 19723)

    assert response.success is False
    assert response.error_code in {"syntax_error", "runtime_error"}
    assert response.error is not None
    assert response.execution_model == "local_user"


async def test_abnormal_worker_exit_is_structured(executor: ScriptExecutor) -> None:
    response = await executor.run("import os\nos._exit(7)", 19723)

    assert response.success is False
    assert response.error_code == "worker_exit"
    assert response.error == "Script worker exited abnormally with code 7"
    assert response.stdout == ""
    assert response.stderr == ""


async def test_malformed_worker_response_is_structured(
    executor: ScriptExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = (
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'not-json')",
    )
    monkeypatch.setattr(executor, "_worker_command", lambda: command)

    response = await executor.run("result = 1", 19723)

    assert response.success is False
    assert response.error_code == "worker_protocol"
    assert response.error == "Script worker returned a malformed response"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parent_rejects_nonstandard_worker_response_constants(
    executor: ScriptExecutor,
    constant: str,
) -> None:
    payload = (
        '{"success":true,"result":'
        + constant
        + ',"stdout":"","stderr":"","error":null,"error_code":null,'
        '"execution_model":"local_user","execution_time_ms":1}'
    ).encode()

    response = executor._parse_response(payload, time.monotonic())

    assert response.success is False
    assert response.error_code == "worker_protocol"
    assert response.error == "Script worker returned a malformed response"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_worker_rejects_nonstandard_request_constants(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    stdin = io.TextIOWrapper(
        io.BytesIO(f'{{"script":"result = 1","port":{constant}}}'.encode()),
        encoding="utf-8",
    )
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(worker_module, "_emit_response", emitted.append)

    worker_module.main()

    assert len(emitted) == 1
    assert emitted[0]["success"] is False
    assert emitted[0]["error_code"] == "worker_protocol"


async def test_worker_uses_fresh_loopback_archicad_connection(
    executor: ScriptExecutor,
    fake_archicad_port: int,
) -> None:
    script = """
native = await archicad.command("GetProductInfo")
tapir = await archicad.tapir("Echo", {"value": 7})
result = {"version": native["version"], "echo": tapir["echo"]}
"""
    response = await executor.run(script, fake_archicad_port, timeout_seconds=5)

    assert response.success is True
    assert response.result == {"version": "test-version", "echo": {"value": 7}}
