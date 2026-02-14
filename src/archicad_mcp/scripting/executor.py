"""Script execution engine with async support and safety features."""

from __future__ import annotations

import asyncio
import builtins
import collections
import copy
import csv
import datetime
import functools
import io
import itertools
import json
import math
import pathlib
import re
import statistics
import time
import traceback
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import openpyxl

from archicad_mcp.config import SecurityConfig
from archicad_mcp.models import ScriptResult
from archicad_mcp.scripting.api import ArchicadAPI

if TYPE_CHECKING:
    from collections.abc import Callable

    from archicad_mcp.core.connection import ArchicadConnection


# Modules available to scripts
ALLOWED_MODULES: dict[str, Any] = {
    "json": json,
    "csv": csv,
    "math": math,
    "re": re,
    "datetime": datetime,
    "pathlib": pathlib,
    "itertools": itertools,
    "functools": functools,
    "collections": collections,  # defaultdict, Counter, etc.
    "statistics": statistics,  # mean, median, stdev for property aggregation
    "copy": copy,  # deepcopy for nested element dicts
    "io": io,  # StringIO/BytesIO for in-memory file operations
    "Path": Path,
    "openpyxl": openpyxl,
}

# Module names that can be used with `import` in sandboxed mode
_IMPORTABLE_MODULES = frozenset(
    name for name, obj in ALLOWED_MODULES.items() if isinstance(obj, types.ModuleType)
)


def _safe_import(
    name: str,
    globals: dict[str, Any] | None = None,
    locals: dict[str, Any] | None = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    """Restricted __import__ that only allows ALLOWED_MODULES."""
    if name not in _IMPORTABLE_MODULES:
        allowed = ", ".join(sorted(_IMPORTABLE_MODULES))
        raise ImportError(f"Module '{name}' is not available. Available: {allowed}")
    return builtins.__import__(name, globals, locals, fromlist, level)


# Safe subset of builtins for scripts
SCRIPT_BUILTINS: dict[str, Any] = {
    # Constants
    "True": True,
    "False": False,
    "None": None,
    # Types
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
    "bytes": bytes,
    "list": list,
    "tuple": tuple,
    "dict": dict,
    "set": set,
    "frozenset": frozenset,
    # Functions
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "chr": chr,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "format": builtins.format,
    "hasattr": hasattr,
    "hash": hash,
    "hex": hex,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "slice": slice,
    "sorted": sorted,
    "sum": sum,
    "zip": zip,
    # Exceptions (for try/except)
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "RuntimeError": RuntimeError,
    "StopIteration": StopIteration,
    "FileNotFoundError": FileNotFoundError,
    "PermissionError": PermissionError,
    "IOError": IOError,
    # Restricted import (only ALLOWED_MODULES)
    "__import__": _safe_import,
}

# Maximum items in result list before truncation
MAX_RESULT_ITEMS = 500
SAMPLE_SIZE = 50

# Write mode characters in open() mode string
WRITE_MODE_CHARS = frozenset("wxa+")


def _is_write_mode(mode: str) -> bool:
    """Check if file mode string indicates a write operation.

    Args:
        mode: File mode string (e.g., 'r', 'w', 'rb', 'w+').

    Returns:
        True if mode allows writing.
    """
    return bool(set(mode) & WRITE_MODE_CHARS)


def _create_safe_open(config: SecurityConfig) -> Callable[..., Any]:
    """Create a safe open() wrapper that enforces path restrictions.

    Args:
        config: Security configuration with blocked/allowed patterns.

    Returns:
        A wrapped open() function that checks paths before opening.
    """

    def safe_open(
        file: str | Path,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Open a file with security checks.

        Raises:
            PermissionError: If path is blocked by security policy.
        """
        path_str = str(file)
        is_write = _is_write_mode(mode)

        if config.is_path_blocked(path_str, for_write=is_write):
            if is_write and config.mode == "sandboxed":
                raise PermissionError(
                    f"Write access denied: '{path_str}' is not in allowed write paths. "
                    f"Allowed: {', '.join(config.allowed_write_expanded)}"
                )
            raise PermissionError(
                f"Access denied: '{path_str}' is in a blocked directory. "
                f"Blocked: {', '.join(config.blocked_expanded)}"
            )

        return open(file, mode, *args, **kwargs)

    return safe_open


class ScriptExecutor:
    """Executes Python scripts with Archicad API access."""

    async def run(
        self,
        script: str,
        connection: ArchicadConnection,
        timeout_seconds: int | None = None,
        config: SecurityConfig | None = None,
    ) -> ScriptResult:
        """Execute a script and return results.

        Args:
            script: Python source code to execute.
            connection: Archicad connection for API calls.
            timeout_seconds: Optional timeout (None = no timeout).
            config: Security configuration (defaults to unrestricted if None).

        Returns:
            ScriptResult with success status, result, stdout, and error info.
        """
        if config is None:
            config = SecurityConfig()

        start_time = time.time()
        stdout_capture = io.StringIO()

        # Create API instance for this script
        api = ArchicadAPI(connection)

        # Build execution namespace
        namespace = self._build_namespace(api, connection.port, stdout_capture, config)

        try:
            # Wrap script in async function to support await
            wrapped_script = self._wrap_script(script)

            # Compile
            code = compile(wrapped_script, "<script>", "exec")

            # Execute the wrapper to define __script_main__
            exec(code, namespace)
            coro = namespace["__script_result__"]

            # Run with optional timeout
            if timeout_seconds is not None:
                try:
                    await asyncio.wait_for(coro, timeout=timeout_seconds)
                except TimeoutError:
                    return ScriptResult(
                        success=False,
                        result=None,
                        stdout=stdout_capture.getvalue(),
                        error=f"Script timed out after {timeout_seconds} seconds",
                        execution_time_ms=int((time.time() - start_time) * 1000),
                    )
            else:
                await coro

            # Get and process result
            result = namespace.get("result")
            result = self._process_result(result)

            return ScriptResult(
                success=True,
                result=result,
                stdout=stdout_capture.getvalue(),
                error=None,
                execution_time_ms=int((time.time() - start_time) * 1000),
            )

        except SyntaxError as e:
            return ScriptResult(
                success=False,
                result=None,
                stdout=stdout_capture.getvalue(),
                error=f"Syntax error at line {e.lineno}: {e.msg}",
                execution_time_ms=int((time.time() - start_time) * 1000),
            )

        except Exception as e:
            # Extract line number from traceback
            error_msg = self._format_error(e, script)

            return ScriptResult(
                success=False,
                result=None,
                stdout=stdout_capture.getvalue(),
                error=error_msg,
                execution_time_ms=int((time.time() - start_time) * 1000),
            )

    def _build_namespace(
        self,
        api: ArchicadAPI,
        port: int,
        stdout_capture: io.StringIO,
        config: SecurityConfig,
    ) -> dict[str, Any]:
        """Build the execution namespace for scripts."""
        # Create safe_open with path restrictions for BOTH modes
        safe_open = _create_safe_open(config)

        namespace: dict[str, Any] = {
            # Core objects
            "archicad": api,
            "port": port,
            "result": None,
            # Captured print
            "print": lambda *args, **kwargs: builtins.print(*args, file=stdout_capture, **kwargs),
            # File access (path-restricted in both modes)
            "open": safe_open,
            # Allowed modules
            **ALLOWED_MODULES,
        }

        if config.mode == "unrestricted":
            # Full builtins (but open is still path-restricted)
            namespace["__builtins__"] = builtins.__dict__
        else:
            # Sandboxed - restricted builtins
            namespace["__builtins__"] = SCRIPT_BUILTINS

        return namespace

    def _wrap_script(self, script: str) -> str:
        """Wrap script in async function to support await."""
        # Indent all lines
        indented = "\n".join("    " + line if line.strip() else line for line in script.split("\n"))

        return f"""
async def __script_main__():
    result = None
{indented}
    globals()['result'] = result

__script_result__ = __script_main__()
"""

    def _process_result(self, result: Any) -> Any:
        """Process result, truncating large lists."""
        if isinstance(result, list) and len(result) > MAX_RESULT_ITEMS:
            return {
                "total": len(result),
                "sample": result[:SAMPLE_SIZE],
                "truncated": True,
                "warning": f"Result list has {len(result)} items. "
                f"Showing first {SAMPLE_SIZE}. Process data in script to return smaller results.",
            }
        return result

    def _format_error(self, exc: Exception, script: str) -> str:
        """Format exception with line number from original script."""
        tb = traceback.extract_tb(exc.__traceback__)

        # Find the frame in our script
        script_line: int | None = None
        for frame in reversed(tb):
            if frame.filename == "<script>" and frame.lineno is not None:
                # Adjust for wrapper: subtract 3 lines (async def, result=None, blank)
                script_line = frame.lineno - 3
                break

        error_type = type(exc).__name__
        error_msg = str(exc)

        if script_line and script_line > 0:
            # Get the offending line
            lines = script.split("\n")
            if 0 < script_line <= len(lines):
                offending_line = lines[script_line - 1].strip()
                return f"Line {script_line}: {error_type}: {error_msg}\n  > {offending_line}"
            return f"Line {script_line}: {error_type}: {error_msg}"

        return f"{error_type}: {error_msg}"
