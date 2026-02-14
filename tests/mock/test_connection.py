"""Mock tests for ArchicadConnection."""

import aiohttp
import pytest
from aioresponses import aioresponses

from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.errors import (
    ArchicadConnectionError,
    CommandError,
    TapirNotAvailableError,
)


@pytest.fixture
async def session() -> aiohttp.ClientSession:
    """Create aiohttp session for tests."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def connection(session: aiohttp.ClientSession) -> ArchicadConnection:
    """Create connection instance for tests."""
    return ArchicadConnection(
        port=19723,
        session=session,
        info={
            "projectName": "Test Project",
            "projectPath": "C:/test.pln",
            "version": "27.0.0",
            "isTeamwork": False,
        },
    )


class TestArchicadConnectionInit:
    """Tests for connection initialization."""

    def test_stores_port_and_url(self, connection: ArchicadConnection) -> None:
        """Connection stores port and builds URL."""
        assert connection.port == 19723
        assert connection.url == "http://127.0.0.1:19723"

    def test_stores_project_info(self, connection: ArchicadConnection) -> None:
        """Connection stores project information."""
        assert connection.project_name == "Test Project"
        assert connection.project_path == "C:/test.pln"
        assert connection.version == "27.0.0"
        assert connection.is_teamwork is False

    def test_tapir_available_initially_unknown(self, connection: ArchicadConnection) -> None:
        """Tapir availability is unknown until first use."""
        assert connection._tapir_available is None


class TestBuiltinApiExecution:
    """Tests for built-in API command execution."""

    async def test_successful_command(self, connection: ArchicadConnection) -> None:
        """Successful built-in API command returns result."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {"elements": [{"guid": "abc-123"}]},
                },
            )

            result = await connection.execute("API.GetAllElements", {"elementType": "Wall"})

            assert result == {"elements": [{"guid": "abc-123"}]}

    async def test_command_error(self, connection: ArchicadConnection) -> None:
        """Failed command raises CommandError."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": False,
                    "error": {"code": 400, "message": "Invalid parameters"},
                },
            )

            with pytest.raises(CommandError) as exc_info:
                await connection.execute("API.GetAllElements", {})

            assert "Invalid parameters" in str(exc_info.value)
            assert exc_info.value.details["code"] == 400

    async def test_connection_error(self, connection: ArchicadConnection) -> None:
        """Network error raises ArchicadConnectionError."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                exception=aiohttp.ClientError("Connection refused"),
            )

            with pytest.raises(ArchicadConnectionError) as exc_info:
                await connection.execute("API.GetAllElements", {})

            assert "19723" in str(exc_info.value)
            assert exc_info.value.suggestion != ""


class TestTapirExecution:
    """Tests for Tapir command execution."""

    async def test_successful_tapir_command(self, connection: ArchicadConnection) -> None:
        """Successful Tapir command returns unwrapped result."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "success": True,
                            "projectName": "My Project",
                            "projectPath": "C:/project.pln",
                        }
                    },
                },
            )

            result = await connection.execute("GetProjectInfo", {})

            assert result["projectName"] == "My Project"
            assert connection._tapir_available is True

    async def test_tapir_not_installed(self, connection: ArchicadConnection) -> None:
        """Tapir not installed raises TapirNotAvailableError."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": False,
                    "error": {
                        "code": 8000,
                        "message": "AddOn is not registered",
                    },
                },
            )

            with pytest.raises(TapirNotAvailableError) as exc_info:
                await connection.execute("GetProjectInfo", {})

            assert "github.com" in exc_info.value.suggestion
            assert connection._tapir_available is False

    async def test_tapir_cached_unavailable(self, connection: ArchicadConnection) -> None:
        """Once Tapir is known unavailable, skip HTTP call."""
        connection._tapir_available = False

        # No mock needed - should raise without making request
        with pytest.raises(TapirNotAvailableError):
            await connection.execute("GetProjectInfo", {})

    async def test_tapir_command_error(self, connection: ArchicadConnection) -> None:
        """Tapir FailedExecutionResult (success=false + error) raises CommandError."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "success": False,
                            "error": {"message": "Invalid element type"},
                        }
                    },
                },
            )

            with pytest.raises(CommandError) as exc_info:
                await connection.execute("GetElementsByType", {"type": "Invalid"})

            assert "Invalid element type" in str(exc_info.value)

    async def test_tapir_error_item_without_success_field(
        self, connection: ArchicadConnection
    ) -> None:
        """Tapir ErrorItem (error only, no success field) raises CommandError."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "error": {
                                "code": -2130313112,
                                "message": "Invalid elementType 'wall'.",
                            },
                        }
                    },
                },
            )

            with pytest.raises(CommandError) as exc_info:
                await connection.execute("GetElementsByType", {"elementType": "wall"})

            assert "Invalid elementType" in str(exc_info.value)
            assert exc_info.value.details["code"] == -2130313112


class TestIsAlive:
    """Tests for is_alive health check."""

    async def test_is_alive_success(self, connection: ArchicadConnection) -> None:
        """is_alive returns True when Archicad responds."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {}},
            )

            assert await connection.is_alive() is True

    async def test_is_alive_failure(self, connection: ArchicadConnection) -> None:
        """is_alive returns False when Archicad doesn't respond."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                exception=aiohttp.ClientError("Connection refused"),
            )

            assert await connection.is_alive() is False


class TestCommandRouting:
    """Tests for automatic command routing."""

    async def test_api_prefix_routes_to_builtin(self, connection: ArchicadConnection) -> None:
        """Commands starting with API. use built-in API (returns raw result)."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {"version": "27.0.0"}},
            )

            # Built-in API returns result directly
            result = await connection.execute("API.GetProductInfo", {})
            assert result == {"version": "27.0.0"}

    async def test_no_prefix_routes_to_tapir(self, connection: ArchicadConnection) -> None:
        """Commands without API. prefix use Tapir (unwraps response)."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "success": True,
                            "projectName": "Test",
                        }
                    },
                },
            )

            # Tapir returns unwrapped addOnCommandResponse
            result = await connection.execute("GetProjectInfo", {})
            assert result["projectName"] == "Test"
            assert connection._tapir_available is True
