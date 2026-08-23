"""Disposable same-account worker execution for Archicad scripts."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import suppress
from typing import Any, Never

from pydantic import ValidationError

from archicad_mcp.config import DEFAULT_SCRIPT_TIMEOUT_SECONDS, validate_script_timeout
from archicad_mcp.models import ScriptErrorCode, ScriptResult


def _reject_json_constant(value: str) -> Never:
    raise json.JSONDecodeError("non-standard JSON constant", value, 0)


class ScriptExecutor:
    """Run scripts in disposable local-user Python worker processes."""

    def __init__(self) -> None:
        self.default_timeout_seconds = DEFAULT_SCRIPT_TIMEOUT_SECONDS

    async def run(
        self,
        script: str,
        port: int,
        timeout_seconds: float | None = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
    ) -> ScriptResult:
        """Execute one script in an owned worker process and return its structured result."""
        timeout = validate_script_timeout(timeout_seconds)
        started = time.monotonic()
        request = json.dumps(
            {"script": script, "port": port},
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *self._worker_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, subprocess.SubprocessError):
            return self._failure(
                "worker_start",
                "Script worker could not be started",
                started,
            )
        communicate_task = asyncio.create_task(process.communicate(input=request))

        try:
            if timeout is None:
                stdout, _worker_stderr = await asyncio.shield(communicate_task)
            else:
                done, _pending = await asyncio.wait({communicate_task}, timeout=timeout)
                if not done:
                    await self._terminate_owned_process(process)
                    with suppress(Exception):
                        await communicate_task
                    return self._failure(
                        "timeout",
                        f"Script timed out after {timeout:g} seconds",
                        started,
                    )
                stdout, _worker_stderr = communicate_task.result()
        except asyncio.CancelledError:
            await self._terminate_owned_process(process)
            with suppress(Exception):
                await asyncio.shield(communicate_task)
            raise

        if process.returncode != 0:
            exit_code = process.returncode
            return self._failure(
                "worker_exit",
                f"Script worker exited abnormally with code {exit_code}",
                started,
            )

        return self._parse_response(stdout, started)

    def _worker_command(self) -> tuple[str, ...]:
        """Return the importable package worker command for the current interpreter."""
        return (sys.executable, "-m", "archicad_mcp.scripting.worker")

    async def _terminate_owned_process(self, process: asyncio.subprocess.Process) -> None:
        """Terminate and await only the exact process created for this execution."""
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    def _parse_response(self, payload: bytes, started: float) -> ScriptResult:
        """Validate a worker response without exposing protocol internals."""
        try:
            raw: Any = json.loads(payload.decode("utf-8"), parse_constant=_reject_json_constant)
            if not isinstance(raw, dict):
                raise TypeError("worker response is not an object")
            return ScriptResult.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValidationError):
            return self._failure(
                "worker_protocol",
                "Script worker returned a malformed response",
                started,
            )

    def _failure(self, error_code: ScriptErrorCode, error: str, started: float) -> ScriptResult:
        """Build a parent-side failure result."""
        return ScriptResult(
            success=False,
            result=None,
            stdout="",
            stderr="",
            error=error,
            error_code=error_code,
            execution_model="local_user",
            execution_time_ms=int((time.monotonic() - started) * 1000),
        )
