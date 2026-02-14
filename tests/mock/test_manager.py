"""Mock tests for ConnectionManager."""

import aiohttp
import pytest
from aioresponses import aioresponses

from archicad_mcp.core.errors import ArchicadConnectionError
from archicad_mcp.core.manager import PORT_RANGE, ConnectionManager


@pytest.fixture
async def session() -> aiohttp.ClientSession:
    """Create aiohttp session for tests."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def manager(session: aiohttp.ClientSession) -> ConnectionManager:
    """Create manager instance for tests."""
    return ConnectionManager(session)


class TestConnectionManagerInit:
    """Tests for manager initialization."""

    def test_starts_with_no_connections(self, manager: ConnectionManager) -> None:
        """Manager starts with empty connections dict."""
        assert manager.connections == {}


class TestPortScanning:
    """Tests for port scanning."""

    async def test_finds_running_instance(self, manager: ConnectionManager) -> None:
        """Scanner finds Archicad on active port."""
        with aioresponses() as m:
            # Mock GetProductInfo response
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {"version": "27.0.0"},
                },
            )
            # Mock GetProjectInfo (Tapir) response
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "projectName": "Test Project",
                            "projectPath": "C:/test.pln",
                            "isTeamwork": False,
                        }
                    },
                },
            )
            # Mock all other ports as failing
            for port in PORT_RANGE:
                if port != 19723:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.scan_and_connect()

            assert 19723 in manager.connections
            conn = manager.connections[19723]
            assert conn.project_name == "Test Project"
            assert conn.version == "27.0.0"

    async def test_handles_no_instances(self, manager: ConnectionManager) -> None:
        """Scanner handles no running Archicad instances."""
        with aioresponses() as m:
            # Mock all ports as failing
            for port in PORT_RANGE:
                m.post(
                    f"http://127.0.0.1:{port}",
                    exception=aiohttp.ClientError("Connection refused"),
                )

            await manager.scan_and_connect()

            assert manager.connections == {}

    async def test_handles_tapir_not_installed(self, manager: ConnectionManager) -> None:
        """Scanner works even when Tapir is not installed."""
        with aioresponses() as m:
            # Mock GetProductInfo success
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {"version": "27.0.0"},
                },
            )
            # Mock GetProjectInfo failure (Tapir not installed)
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": False,
                    "error": {"message": "AddOn is not registered"},
                },
            )
            # Mock other ports
            for port in PORT_RANGE:
                if port != 19723:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.scan_and_connect()

            assert 19723 in manager.connections
            conn = manager.connections[19723]
            assert conn.project_name == "Unknown"

    async def test_multiple_instances(self, manager: ConnectionManager) -> None:
        """Scanner finds multiple running instances."""
        with aioresponses() as m:
            # Mock two running instances
            for port in [19723, 19724]:
                m.post(
                    f"http://127.0.0.1:{port}",
                    payload={
                        "succeeded": True,
                        "result": {"version": "27.0.0"},
                    },
                )
                m.post(
                    f"http://127.0.0.1:{port}",
                    payload={
                        "succeeded": True,
                        "result": {
                            "addOnCommandResponse": {
                                "projectName": f"Project {port}",
                                "projectPath": f"C:/project{port}.pln",
                                "isTeamwork": False,
                            }
                        },
                    },
                )

            # Mock other ports as failing
            for port in PORT_RANGE:
                if port not in [19723, 19724]:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.scan_and_connect()

            assert len(manager.connections) == 2
            assert 19723 in manager.connections
            assert 19724 in manager.connections


class TestGetConnection:
    """Tests for getting connections."""

    async def test_get_existing_connection(self, manager: ConnectionManager) -> None:
        """get() returns existing connection."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {"version": "27.0.0"}},
            )
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {"addOnCommandResponse": {"projectName": "Test"}},
                },
            )
            for port in PORT_RANGE:
                if port != 19723:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.scan_and_connect()
            conn = manager.get(19723)

            assert conn.port == 19723

    def test_get_nonexistent_raises(self, manager: ConnectionManager) -> None:
        """get() raises ArchicadConnectionError for unknown port."""
        with pytest.raises(ArchicadConnectionError) as exc_info:
            manager.get(19999)

        assert "19999" in str(exc_info.value)
        assert exc_info.value.details["port"] == 19999
        assert "list_instances" in exc_info.value.suggestion


class TestGetInstances:
    """Tests for getting instance info."""

    async def test_returns_archicad_instances(self, manager: ConnectionManager) -> None:
        """get_instances() returns list of ArchicadInstance models."""
        with aioresponses() as m:
            m.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {"version": "27.0.0"}},
            )
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {
                        "addOnCommandResponse": {
                            "projectName": "Test Project",
                            "projectPath": "C:/test.pln",
                            "isTeamwork": False,
                        }
                    },
                },
            )
            for port in PORT_RANGE:
                if port != 19723:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.scan_and_connect()
            instances = manager.get_instances()

            assert len(instances) == 1
            inst = instances[0]
            assert inst.port == 19723
            assert inst.project_name == "Test Project"
            assert inst.project_type == "solo"
            assert inst.archicad_version == "27.0.0"

    def test_empty_when_no_connections(self, manager: ConnectionManager) -> None:
        """get_instances() returns empty list when no connections."""
        instances = manager.get_instances()
        assert instances == []


class TestRefresh:
    """Tests for refresh functionality."""

    async def test_refresh_rescans_ports(self, manager: ConnectionManager) -> None:
        """refresh() rescans all ports."""
        with aioresponses() as m:
            # First scan - no instances
            for port in PORT_RANGE:
                m.post(
                    f"http://127.0.0.1:{port}",
                    exception=aiohttp.ClientError("Connection refused"),
                )

            await manager.scan_and_connect()
            assert len(manager.connections) == 0

            # Second scan - one instance appears
            m.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {"version": "27.0.0"}},
            )
            m.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": True,
                    "result": {"addOnCommandResponse": {"projectName": "New"}},
                },
            )
            for port in PORT_RANGE:
                if port != 19723:
                    m.post(
                        f"http://127.0.0.1:{port}",
                        exception=aiohttp.ClientError("Connection refused"),
                    )

            await manager.refresh()
            assert len(manager.connections) == 1
