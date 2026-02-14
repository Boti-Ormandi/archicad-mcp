"""API object exposed to scripts as 'archicad'."""

from typing import Any

from archicad_mcp.core.connection import ArchicadConnection


class ArchicadAPI:
    """API object exposed to scripts as 'archicad'.

    Provides raw command access to Archicad's JSON API.
    Use tapir() for Tapir commands and command() for built-in API commands.
    All methods are async and should be called with await.

    For command schemas and parameters, use get_docs() tool.
    """

    def __init__(self, connection: ArchicadConnection) -> None:
        self._conn = connection

    async def command(self, name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute any built-in API command (API.*).

        Args:
            name: Command name. "API." prefix is added if missing.
            parameters: Command parameters.

        Returns:
            Command result as dict.
        """
        parameters = parameters or {}
        if not name.startswith("API."):
            name = f"API.{name}"
        return await self._conn.execute(name, parameters)

    async def tapir(self, name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute any Tapir command.

        Args:
            name: Tapir command name (without API. prefix).
            parameters: Command parameters.

        Returns:
            Unwrapped Tapir response as dict.
        """
        parameters = parameters or {}
        return await self._conn.execute(name, parameters)
