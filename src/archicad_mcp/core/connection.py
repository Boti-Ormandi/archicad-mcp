"""Connection to a single Archicad instance."""

import aiohttp

from archicad_mcp.core.errors import (
    ArchicadConnectionError,
    CommandError,
    TapirNotAvailableError,
)


class ArchicadConnection:
    """Connection to a single Archicad instance.

    Handles both built-in Archicad API (API.*) and Tapir add-on commands.
    """

    def __init__(
        self,
        port: int,
        session: aiohttp.ClientSession,
        info: dict[str, object],
    ) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.session = session
        self.project_name = str(info.get("projectName", "Unknown"))
        self.project_path = info.get("projectPath")
        self.version = str(info.get("version", "Unknown"))
        self.is_teamwork = bool(info.get("isTeamwork", False))
        # Use tapirAvailable from probing if provided, otherwise None (unknown)
        tapir_val = info.get("tapirAvailable")
        self._tapir_available: bool | None = tapir_val if isinstance(tapir_val, bool) else None

    async def execute(
        self,
        command: str,
        parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Execute a command, auto-routing to built-in or Tapir API.

        Args:
            command: Command name. "API.*" for built-in, otherwise Tapir.
            parameters: Command parameters.

        Returns:
            Command result as dict.
        """
        parameters = parameters or {}
        if command.startswith("API."):
            return await self._execute_builtin(command, parameters)
        return await self._execute_tapir(command, parameters)

    async def _execute_builtin(
        self,
        command: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        """Execute built-in Archicad API command."""
        payload = {"command": command, "parameters": parameters}

        try:
            async with self.session.post(self.url, json=payload) as resp:
                data: dict[str, object] = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise ArchicadConnectionError(
                f"Cannot connect to Archicad on port {self.port}",
                details={"port": self.port, "error": str(e)},
                suggestion="Ensure Archicad is running with JSON API enabled",
            ) from e

        if not data.get("succeeded"):
            error = data.get("error", {})
            if isinstance(error, dict):
                raise CommandError(
                    str(error.get("message", "Command failed")),
                    details={"command": command, "code": error.get("code")},
                    suggestion=self._suggest_fix(error.get("code")),
                )
            raise CommandError(
                "Command failed",
                details={"command": command},
            )

        result = data.get("result", {})
        return result if isinstance(result, dict) else {}

    async def _execute_tapir(
        self,
        command: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        """Execute Tapir add-on command."""
        # Check Tapir availability on first use
        if self._tapir_available is False:
            raise TapirNotAvailableError(
                "Tapir add-on is not installed",
                details={"port": self.port, "command": command},
                suggestion=(
                    "Install Tapir from "
                    "https://github.com/ENZYME-APD/tapir-archicad-automation/releases"
                ),
            )

        payload = {
            "command": "API.ExecuteAddOnCommand",
            "parameters": {
                "addOnCommandId": {
                    "commandNamespace": "TapirCommand",
                    "commandName": command,
                },
                "addOnCommandParameters": parameters,
            },
        }

        try:
            async with self.session.post(self.url, json=payload) as resp:
                data: dict[str, object] = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise ArchicadConnectionError(
                f"Cannot connect to Archicad on port {self.port}",
                details={"port": self.port, "error": str(e)},
                suggestion="Ensure Archicad is running",
            ) from e

        if not data.get("succeeded"):
            error = data.get("error", {})
            error_msg = ""
            error_code = None
            if isinstance(error, dict):
                error_msg = str(error.get("message", ""))
                error_code = error.get("code")

            # Detect Tapir not installed: Archicad returns "not registered" errors
            # when the add-on namespace or add-on itself is missing.
            # Code 4010 = command not found (Tapir IS installed, just wrong command name)
            is_addon_missing = error_code != 4010 and "not registered" in error_msg.lower()
            if is_addon_missing:
                self._tapir_available = False
                raise TapirNotAvailableError(
                    "Tapir add-on is not installed",
                    details={"port": self.port, "command": command},
                    suggestion=(
                        "Install Tapir from "
                        "https://github.com/ENZYME-APD/tapir-archicad-automation/releases"
                    ),
                )

            # Command not found in Tapir (code 4010) - Tapir IS available
            if error_code == 4010:
                self._tapir_available = True
                raise CommandError(
                    f"Unknown Tapir command: {command}",
                    details={"command": command, "code": error_code},
                    suggestion="Check command name with get_docs(search='...')",
                )

            if isinstance(error, dict):
                raise CommandError(
                    str(error.get("message", "Command failed")),
                    details={"command": command, "code": error_code},
                )
            raise CommandError(
                "Command failed",
                details={"command": command},
            )

        # Unwrap Tapir response
        result = data.get("result", {})
        if not isinstance(result, dict):
            result = {}
        raw_addon = result.get("addOnCommandResponse", {})
        addon_response: dict[str, object] = raw_addon if isinstance(raw_addon, dict) else {}

        # Tapir signals errors via an "error" field in the response
        # (ErrorItem shape: just "error", or FailedExecutionResult: "error" + "success")
        addon_error = addon_response.get("error")
        if isinstance(addon_error, dict) and addon_error:
            raise CommandError(
                str(addon_error.get("message", "Tapir command failed")),
                details={"command": command, "code": addon_error.get("code")},
            )

        self._tapir_available = True
        return addon_response

    async def is_alive(self) -> bool:
        """Check if Archicad is responding."""
        try:
            await self._execute_builtin("API.IsAlive", {})
            return True
        except Exception:
            return False

    async def check_tapir(self) -> bool:
        """Check if Tapir add-on is available.

        Probes with GetAddOnVersion command which always exists in Tapir.
        Caches the result for future calls.
        """
        if self._tapir_available is not None:
            return self._tapir_available

        try:
            await self._execute_tapir("GetAddOnVersion", {})
            return True
        except TapirNotAvailableError:
            return False
        except CommandError:
            # Command error means Tapir is available but something else failed
            self._tapir_available = True
            return True

    def _suggest_fix(self, error_code: object) -> str:
        """Return actionable suggestion based on error code."""
        suggestions: dict[int, str] = {
            # Add known error codes here as we discover them
        }
        if isinstance(error_code, int):
            return suggestions.get(error_code, "Check command parameters")
        return "Check command parameters"
