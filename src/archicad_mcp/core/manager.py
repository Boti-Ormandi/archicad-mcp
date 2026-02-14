"""Manages connections to multiple Archicad instances."""

import asyncio
import logging

import aiohttp

from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.errors import ArchicadConnectionError
from archicad_mcp.models import ArchicadInstance

logger = logging.getLogger(__name__)

PORT_RANGE = range(19723, 19744)


class ConnectionManager:
    """Manages connections to multiple Archicad instances.

    Scans ports 19723-19744 for running Archicad instances and maintains
    connections to them.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.connections: dict[int, ArchicadConnection] = {}

    async def scan_and_connect(self) -> None:
        """Scan all ports and connect to active instances."""
        tasks = [self._probe_port(port) for port in PORT_RANGE]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe_port(self, port: int) -> None:
        """Check if Archicad is running on port and get info."""
        url = f"http://127.0.0.1:{port}"

        try:
            async with self.session.post(
                url,
                json={"command": "API.GetProductInfo", "parameters": {}},
                timeout=aiohttp.ClientTimeout(total=1.0),
            ) as resp:
                if resp.status != 200:
                    self.connections.pop(port, None)
                    return
                data: dict[str, object] = await resp.json(content_type=None)
                if data.get("succeeded"):
                    result = data.get("result", {})
                    if isinstance(result, dict):
                        info = await self._get_full_info(port, result)
                        was_new = port not in self.connections
                        self.connections[port] = ArchicadConnection(port, self.session, info)
                        if was_new:
                            logger.info(
                                "Found Archicad on port %d (%s)",
                                port,
                                info.get("projectName", "Unknown"),
                            )
        except (TimeoutError, aiohttp.ClientError):
            # Port not responding, remove if was connected
            if self.connections.pop(port, None):
                logger.info("Lost Archicad on port %d", port)

    async def _get_full_info(
        self,
        port: int,
        product_info: dict[str, object],
    ) -> dict[str, object]:
        """Get complete instance info (product + project)."""
        info: dict[str, object] = {"version": product_info.get("version")}

        # Try to get project info via Tapir (may fail if not installed or no project)
        try:
            async with self.session.post(
                f"http://127.0.0.1:{port}",
                json={
                    "command": "API.ExecuteAddOnCommand",
                    "parameters": {
                        "addOnCommandId": {
                            "commandNamespace": "TapirCommand",
                            "commandName": "GetProjectInfo",
                        },
                        "addOnCommandParameters": {},
                    },
                },
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as resp:
                data: dict[str, object] = await resp.json(content_type=None)
                if data.get("succeeded"):
                    result = data.get("result", {})
                    if isinstance(result, dict):
                        proj = result.get("addOnCommandResponse", {})
                        if isinstance(proj, dict):
                            info["projectName"] = proj.get("projectName", "Untitled")
                            info["projectPath"] = proj.get("projectPath")
                            info["isTeamwork"] = proj.get("isTeamwork", False)
                            info["tapirAvailable"] = True
        except Exception:
            info["projectName"] = "Unknown"
            info["tapirAvailable"] = False

        return info

    async def refresh(self) -> None:
        """Re-scan all ports."""
        await self.scan_and_connect()

    def get(self, port: int) -> ArchicadConnection:
        """Get connection by port, raise if not found."""
        if port not in self.connections:
            raise ArchicadConnectionError(
                f"No Archicad instance on port {port}",
                details={"port": port, "active_ports": list(self.connections.keys())},
                suggestion="Use list_instances to find available ports",
            )
        return self.connections[port]

    def get_instances(self) -> list[ArchicadInstance]:
        """Get info for all connected instances."""
        instances = []
        for conn in self.connections.values():
            # Determine project type
            if conn.is_teamwork:
                project_type = "teamwork"
            elif conn.project_name in ("Unknown", "Untitled"):
                project_type = "untitled"
            else:
                project_type = "solo"

            instances.append(
                ArchicadInstance(
                    port=conn.port,
                    project_name=conn.project_name,
                    project_path=str(conn.project_path) if conn.project_path else None,
                    project_type=project_type,  # type: ignore[arg-type]
                    archicad_version=conn.version,
                    is_tapir_available=conn._tapir_available is True,
                )
            )
        return instances
