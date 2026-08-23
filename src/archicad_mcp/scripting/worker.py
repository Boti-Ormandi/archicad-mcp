"""Package worker entry point for disposable local-user script execution."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Never, TextIO, cast

import aiohttp

from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.scripting.api import ArchicadAPI

_SCRIPT_FILENAME = "<archicad-mcp-script>"
_WRAPPER_LINE_OFFSET = 2


def _reject_json_constant(value: str) -> Never:
    raise json.JSONDecodeError("non-standard JSON constant", value, 0)


def _response(
    *,
    success: bool,
    result: Any | None,
    stdout: str,
    stderr: str,
    error: str | None,
    error_code: str | None,
    started: float,
) -> dict[str, Any]:
    return {
        "success": success,
        "result": result,
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "error_code": error_code,
        "execution_model": "local_user",
        "execution_time_ms": int((time.monotonic() - started) * 1000),
    }


def _wrap_script(script: str) -> str:
    indented = "\n".join("    " + line if line.strip() else line for line in script.split("\n"))
    return f"async def __script_main__():\n    result = None\n{indented}\n    return result\n"


def _format_syntax_error(exc: SyntaxError) -> str:
    wrapped_line = exc.lineno or 0
    script_line = max(1, wrapped_line - _WRAPPER_LINE_OFFSET)
    return f"Line {script_line}: SyntaxError: {exc.msg}"


def _format_runtime_error(exc: BaseException, script: str) -> str:
    script_line: int | None = None
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if frame.filename == _SCRIPT_FILENAME and frame.lineno is not None:
            script_line = frame.lineno - _WRAPPER_LINE_OFFSET
            break

    error_type = type(exc).__name__
    error_message = str(exc)
    if script_line is None or script_line <= 0:
        return f"{error_type}: {error_message}"

    lines = script.split("\n")
    if script_line <= len(lines):
        source = lines[script_line - 1].strip()
        return f"Line {script_line}: {error_type}: {error_message}\n  > {source}"
    return f"Line {script_line}: {error_type}: {error_message}"


def _flush(stream: TextIO) -> None:
    with suppress(OSError, ValueError):
        stream.flush()


def _read_capture(stream: Any) -> str:
    stream.seek(0)
    data = cast(bytes, stream.read())
    return data.decode("utf-8", errors="replace")


async def _execute_script(script: str, port: int) -> tuple[Any | None, str | None, str | None]:
    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        connection = ArchicadConnection(port, session, {"tapirAvailable": None})
        api = ArchicadAPI(connection)
        namespace: dict[str, Any] = {"archicad": api, "port": port}

        try:
            code = compile(_wrap_script(script), _SCRIPT_FILENAME, "exec")
            exec(code, namespace)
            script_main = cast(Callable[[], Awaitable[Any]], namespace["__script_main__"])
            result = await script_main()
        except SyntaxError as exc:
            return None, "syntax_error", _format_syntax_error(exc)
        except BaseException as exc:
            return None, "runtime_error", _format_runtime_error(exc, script)

    try:
        serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(serialized)
    except Exception:
        return None, "result_not_json", "Script result is not JSON-compatible"
    return normalized, None, None


async def _handle_request(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    script = request.get("script")
    port = request.get("port")
    if not isinstance(script, str) or type(port) is not int:
        return _response(
            success=False,
            result=None,
            stdout="",
            stderr="",
            error="Script worker received an invalid execution request",
            error_code="worker_protocol",
            started=started,
        )

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)

    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_capture,
        tempfile.TemporaryFile(mode="w+b") as stderr_capture,
    ):
        try:
            _flush(original_stdout)
            _flush(original_stderr)
            os.dup2(stdout_capture.fileno(), 1)
            os.dup2(stderr_capture.fileno(), 2)
            result, error_code, error = await _execute_script(script, port)
            _flush(sys.stdout)
            _flush(sys.stderr)
            _flush(original_stdout)
            _flush(original_stderr)
        finally:
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stdout_fd)
            os.close(saved_stderr_fd)
            sys.stdout = original_stdout
            sys.stderr = original_stderr

        stdout = _read_capture(stdout_capture)
        stderr = _read_capture(stderr_capture)

    return _response(
        success=error_code is None,
        result=result,
        stdout=stdout,
        stderr=stderr,
        error=error,
        error_code=error_code,
        started=started,
    )


def _emit_response(response: dict[str, Any]) -> None:
    payload = json.dumps(response, ensure_ascii=True, allow_nan=False).encode("utf-8")
    offset = 0
    while offset < len(payload):
        offset += os.write(1, payload[offset:])


def main() -> None:
    try:
        request_bytes = sys.stdin.buffer.read()
        request = json.loads(request_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        response = asyncio.run(_handle_request(request))
    except BaseException:
        started = time.monotonic()
        response = _response(
            success=False,
            result=None,
            stdout="",
            stderr="",
            error="Script worker failed to process the execution request",
            error_code="worker_protocol",
            started=started,
        )
    _emit_response(response)


if __name__ == "__main__":
    main()
