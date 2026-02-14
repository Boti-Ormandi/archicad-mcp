"""Unit tests for ScriptExecutor."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from archicad_mcp.scripting.executor import ScriptExecutor


@pytest.fixture
def mock_connection() -> MagicMock:
    """Create a mock ArchicadConnection."""
    conn = MagicMock()
    conn.port = 19723
    conn.execute = AsyncMock(return_value={"elements": []})
    return conn


@pytest.fixture
def executor() -> ScriptExecutor:
    """Create a ScriptExecutor instance."""
    return ScriptExecutor()


class TestBasicExecution:
    """Tests for basic script execution."""

    async def test_simple_script_returns_result(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Simple script setting result works."""
        script = "result = 42"
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 42
        assert res.error is None

    async def test_script_with_computation(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script can perform computations."""
        script = """
x = 10
y = 20
result = x + y
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 30

    async def test_script_without_result_returns_none(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script without result assignment returns None."""
        script = "x = 42"
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result is None

    async def test_dict_result(self, executor: ScriptExecutor, mock_connection: MagicMock) -> None:
        """Script can return dict."""
        script = 'result = {"count": 5, "items": [1, 2, 3]}'
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == {"count": 5, "items": [1, 2, 3]}


class TestAsyncExecution:
    """Tests for async/await support."""

    async def test_await_archicad_api(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script can await archicad API calls."""
        mock_connection.execute = AsyncMock(
            return_value={"elements": [{"guid": "abc"}, {"guid": "def"}]}
        )

        script = """
response = await archicad.tapir("GetElementsByType", {"elementType": "Wall"})
elements = response.get("elements", [])
result = len(elements)
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 2

    async def test_multiple_awaits(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script can have multiple await calls."""
        call_count = 0

        async def mock_execute(cmd: str, params: dict) -> dict:
            nonlocal call_count
            call_count += 1
            return {"elements": [{"guid": f"elem-{call_count}"}]}

        mock_connection.execute = mock_execute

        script = """
walls_resp = await archicad.tapir("GetElementsByType", {"elementType": "Wall"})
walls = walls_resp.get("elements", [])
columns_resp = await archicad.tapir("GetElementsByType", {"elementType": "Column"})
columns = columns_resp.get("elements", [])
result = len(walls) + len(columns)
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 2
        assert call_count == 2


class TestStdoutCapture:
    """Tests for stdout capture."""

    async def test_print_captured(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Print statements are captured in stdout."""
        script = """
print("Hello")
print("World")
result = 42
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert "Hello" in res.stdout
        assert "World" in res.stdout

    async def test_print_with_formatting(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Print with formatting works."""
        script = """
name = "test"
count = 5
print(f"Found {count} items in {name}")
result = count
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert "Found 5 items in test" in res.stdout


class TestErrorHandling:
    """Tests for error handling."""

    async def test_syntax_error_reports_line(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Syntax errors report line number."""
        script = """x = 1
y = 2
if True
    z = 3
"""
        res = await executor.run(script, mock_connection)

        assert res.success is False
        assert "Syntax error" in res.error
        assert "line" in res.error.lower()

    async def test_runtime_error_reports_line(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Runtime errors report line number."""
        script = """x = 1
y = 0
z = x / y
result = z
"""
        res = await executor.run(script, mock_connection)

        assert res.success is False
        assert "ZeroDivisionError" in res.error
        assert "Line" in res.error

    async def test_name_error(self, executor: ScriptExecutor, mock_connection: MagicMock) -> None:
        """Undefined variable error is caught."""
        script = "result = undefined_var"
        res = await executor.run(script, mock_connection)

        assert res.success is False
        assert "NameError" in res.error

    async def test_key_error(self, executor: ScriptExecutor, mock_connection: MagicMock) -> None:
        """KeyError is caught."""
        script = """
d = {"a": 1}
result = d["missing"]
"""
        res = await executor.run(script, mock_connection)

        assert res.success is False
        assert "KeyError" in res.error


class TestTimeout:
    """Tests for timeout handling."""

    async def test_timeout_exceeded(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script exceeding timeout returns error."""
        script = """
import asyncio
await asyncio.sleep(10)
result = "done"
"""
        res = await executor.run(script, mock_connection, timeout_seconds=1)

        assert res.success is False
        assert "timed out" in res.error.lower()
        assert "1 seconds" in res.error

    async def test_no_timeout_allows_completion(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Script completes when no timeout set."""
        script = "result = 'completed'"
        res = await executor.run(script, mock_connection, timeout_seconds=None)

        assert res.success is True
        assert res.result == "completed"


class TestResultTruncation:
    """Tests for large result truncation."""

    async def test_small_list_not_truncated(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Lists under 500 items are not truncated."""
        script = "result = list(range(100))"
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == list(range(100))

    async def test_large_list_truncated(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Lists over 500 items are truncated with metadata."""
        script = "result = list(range(1000))"
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert isinstance(res.result, dict)
        assert res.result["total"] == 1000
        assert res.result["truncated"] is True
        assert len(res.result["sample"]) == 50
        assert "warning" in res.result

    async def test_dict_not_truncated(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Dict results are not truncated."""
        script = 'result = {"key": "value", "count": 1000}'
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == {"key": "value", "count": 1000}


class TestAllowedModules:
    """Tests for allowed module access."""

    async def test_json_module(self, executor: ScriptExecutor, mock_connection: MagicMock) -> None:
        """json module is available."""
        script = """
import json
result = json.dumps({"a": 1})
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == '{"a": 1}'

    async def test_math_module(self, executor: ScriptExecutor, mock_connection: MagicMock) -> None:
        """math module is available."""
        script = """
import math
result = math.sqrt(16)
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 4.0

    async def test_path_available(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Path is directly available."""
        script = """
p = Path("test/file.txt")
result = str(p.name)
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == "file.txt"

    async def test_itertools_module(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """itertools module is available."""
        script = """
import itertools
result = list(itertools.islice(range(100), 5))
"""
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == [0, 1, 2, 3, 4]


class TestPortVariable:
    """Tests for port variable access."""

    async def test_port_available(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """port variable is available in script."""
        script = "result = port"
        res = await executor.run(script, mock_connection)

        assert res.success is True
        assert res.result == 19723


class TestExecutionTime:
    """Tests for execution time tracking."""

    async def test_execution_time_recorded(
        self, executor: ScriptExecutor, mock_connection: MagicMock
    ) -> None:
        """Execution time is recorded in milliseconds."""
        script = "result = 1"
        res = await executor.run(script, mock_connection)

        assert res.execution_time_ms >= 0
        assert isinstance(res.execution_time_ms, int)
