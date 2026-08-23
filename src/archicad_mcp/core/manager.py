"""Manages connections to multiple Archicad instances."""

import asyncio
import logging
from typing import cast

import aiohttp

from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.errors import ArchicadConnectionError
from archicad_mcp.models import ArchicadInstance

logger = logging.getLogger(__name__)

PORT_RANGE = range(19723, 19744)


class ConnectionManager:
    """Manages connections to multiple Archicad instances.

    Scans ports 19723-19743 for running Archicad instances and maintains
    connections to them.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.connections: dict[int, ArchicadConnection] = {}

    async def scan_and_connect(self) -> None:
        """Scan all ports and connect to active instances."""
        tasks = [self._probe_port(port) for port in PORT_RANGE]
        await asyncio.gather(*tasks, return_exceptions=True)

    def _drop_connection(self, port: int) -> None:
        """Remove a stale connection after an explicit probe failure."""
        if self.connections.pop(port, None) is not None:
            logger.info("Lost Archicad on port %d", port)

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
                    self._drop_connection(port)
                    return
                raw_data = await resp.json(content_type=None)
                if type(raw_data) is not dict:
                    self._drop_connection(port)
                    return
                data = cast(dict[str, object], raw_data)
                if data.get("succeeded") is not True:
                    self._drop_connection(port)
                    return
                raw_result = data.get("result")
                if type(raw_result) is not dict:
                    self._drop_connection(port)
                    return
                product_info = cast(dict[str, object], raw_result)
                if (
                    set(product_info) != {"version", "buildNumber", "languageCode"}
                    or type(product_info["version"]) is not int
                    or type(product_info["buildNumber"]) is not int
                    or type(product_info["languageCode"]) is not str
                ):
                    self._drop_connection(port)
                    return
                info = await self._get_full_info(port, product_info)
                was_new = port not in self.connections
                self.connections[port] = ArchicadConnection(port, self.session, info)
                if was_new:
                    logger.info(
                        "Found Archicad on port %d (%s)",
                        port,
                        info.get("projectName", "Unknown"),
                    )
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError):
            # A timeout, transport error, or malformed product response means the
            # previous connection cannot be retained as a live instance.
            self._drop_connection(port)
        except Exception:
            # The scan is a best-effort discovery boundary. Any unexpected malformed
            # response is treated as a failed product probe rather than stale state.
            self._drop_connection(port)

    @staticmethod
    def _tapir_payload(command: str) -> dict[str, object]:
        return {
            "command": "API.ExecuteAddOnCommand",
            "parameters": {
                "addOnCommandId": {
                    "commandNamespace": "TapirCommand",
                    "commandName": command,
                },
                "addOnCommandParameters": {},
            },
        }

    async def _post_probe(self, port: int, command: str) -> dict[str, object]:
        async with self.session.post(
            f"http://127.0.0.1:{port}",
            json=self._tapir_payload(command),
            timeout=aiohttp.ClientTimeout(total=2.0),
        ) as resp:
            if resp.status != 200:
                raise ValueError("probe status")
            raw_data = await resp.json(content_type=None)
            if type(raw_data) is not dict:
                raise ValueError("malformed probe response")
            return cast(dict[str, object], raw_data)

    @staticmethod
    def _exact_error(value: object) -> tuple[int, str] | None:
        if type(value) is not dict:
            return None
        error = cast(dict[str, object], value)
        if set(error) != {"code", "message"}:
            return None
        code = error["code"]
        message = error["message"]
        if type(code) is not int or type(message) is not str:
            return None
        return code, message

    async def _probe_tapir(self, port: int) -> tuple[bool | None, str | None]:
        """Probe Tapir independently without weakening the required product probe.

        Returns availability plus the add-on version retained only for exact
        documented response containers at every layer; any extra or missing
        key, or wrong type, proves at most unknown availability with no
        version.
        """
        try:
            data = await self._post_probe(port, "GetAddOnVersion")
        except Exception:
            return None, None

        succeeded = data.get("succeeded")
        if succeeded is False:
            if set(data) != {"succeeded", "error"}:
                return None, None
            error = self._exact_error(data["error"])
            if error is None:
                return None, None
            code, message = error
            is_missing = code != 4010 and "not registered" in message.lower()
            return not is_missing, None
        if succeeded is not True or set(data) != {"succeeded", "result"}:
            return None, None

        raw_result = data["result"]
        if type(raw_result) is not dict:
            return None, None
        result = cast(dict[str, object], raw_result)
        if set(result) != {"addOnCommandResponse"}:
            return None, None
        raw_addon = result["addOnCommandResponse"]
        if type(raw_addon) is not dict:
            return None, None
        addon = cast(dict[str, object], raw_addon)
        if set(addon) == {"version"}:
            version = addon["version"]
            if type(version) is str and bool(version):
                return True, version
            return None, None
        if set(addon) == {"error"}:
            return (True if self._exact_error(addon["error"]) is not None else None), None
        if set(addon) == {"success", "error"}:
            available = (
                True
                if addon["success"] is False and self._exact_error(addon["error"]) is not None
                else None
            )
            return available, None
        return None, None

    async def _get_project_info(self, port: int) -> dict[str, object]:
        """Return validated project metadata or safe defaults after Tapir is known present."""
        defaults: dict[str, object] = {
            "projectName": "Unknown",
            "isTeamwork": False,
        }
        try:
            data = await self._post_probe(port, "GetProjectInfo")
            if data.get("succeeded") is not True:
                return defaults
            raw_result = data.get("result")
            if type(raw_result) is not dict:
                return defaults
            result = cast(dict[str, object], raw_result)
            raw_project = result.get("addOnCommandResponse")
            if type(raw_project) is not dict:
                return defaults
            project = cast(dict[str, object], raw_project)
            if not {"isUntitled", "isTeamwork"}.issubset(project) or not set(project).issubset(
                {"isUntitled", "isTeamwork", "projectLocation", "projectPath", "projectName"}
            ):
                return defaults
            is_untitled = project["isUntitled"]
            is_teamwork = project["isTeamwork"]
            project_name = project.get("projectName")
            project_path = project.get("projectPath")
            if (
                type(is_untitled) is not bool
                or type(is_teamwork) is not bool
                or (
                    project_name is not None and (type(project_name) is not str or not project_name)
                )
                or (
                    project_path is not None and (type(project_path) is not str or not project_path)
                )
            ):
                return defaults
            return {
                "projectName": "Untitled" if project_name is None else project_name,
                "projectPath": project_path,
                "isTeamwork": is_teamwork,
            }
        except Exception:
            return defaults

    async def _get_full_info(
        self,
        port: int,
        product_info: dict[str, object],
    ) -> dict[str, object]:
        """Get validated product info plus independently probed optional metadata."""
        info: dict[str, object] = {
            "version": str(product_info["version"]),
            "buildNumber": product_info["buildNumber"],
            "languageCode": product_info["languageCode"],
            "projectName": "Unknown",
            "isTeamwork": False,
        }
        tapir_available, tapir_version = await self._probe_tapir(port)
        info["tapirAvailable"] = tapir_available
        info["tapirVersion"] = tapir_version
        if tapir_available is True:
            info.update(await self._get_project_info(port))
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
                    tapir_version=conn.tapir_version,
                )
            )
        return instances
