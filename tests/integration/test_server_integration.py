"""Integration tests for MCP server tools.

These tests require a running Archicad instance with Tapir add-on.
Run with: pytest -m integration
Skip with: pytest -m "not integration"
"""

import aiohttp
import pytest

from archicad_mcp.core import ConnectionManager
from archicad_mcp.models import ArchicadInstance

pytestmark = pytest.mark.integration


async def archicad_available() -> int | None:
    """Check if Archicad is running and return port, or None if not available."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
        for port in range(19723, 19744):
            try:
                async with session.post(
                    f"http://127.0.0.1:{port}",
                    json={"command": "API.IsAlive", "parameters": {}},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("succeeded"):
                            return port
            except (aiohttp.ClientError, TimeoutError):
                continue
    return None


@pytest.fixture
async def manager() -> ConnectionManager:
    """Create ConnectionManager with real session."""
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    mgr = ConnectionManager(session)
    await mgr.scan_and_connect()
    yield mgr
    await session.close()


@pytest.fixture
async def skip_if_no_archicad():
    """Skip test if no Archicad instance is running."""
    port = await archicad_available()
    if port is None:
        pytest.skip("No Archicad instance running")
    return port


class TestListInstances:
    """Integration tests for list_instances functionality."""

    async def test_finds_running_instance(
        self, manager: ConnectionManager, skip_if_no_archicad: int
    ) -> None:
        """list_instances finds at least one running Archicad."""
        instances = manager.get_instances()

        assert len(instances) >= 1
        assert any(i.port == skip_if_no_archicad for i in instances)

    async def test_instance_has_required_fields(
        self, manager: ConnectionManager, skip_if_no_archicad: int
    ) -> None:
        """Returned instances have all required fields."""
        instances = manager.get_instances()
        instance = instances[0]

        assert isinstance(instance, ArchicadInstance)
        assert instance.port > 0
        assert instance.project_name  # Not empty
        assert instance.archicad_version  # Not empty
        assert instance.project_type in ("solo", "teamwork", "untitled")
