"""Typed exceptions with structured info for AI consumption."""

from typing import Any


class ArchicadError(Exception):
    """Base error with structured info for AI consumption.

    All errors include:
    - message: Human-readable error description
    - details: Dict with context (port, command, etc.)
    - suggestion: Actionable fix suggestion for AI
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        suggestion: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.suggestion = suggestion

    def to_dict(self) -> dict[str, Any]:
        """Serialize error for tool response."""
        return {
            "type": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
            "suggestion": self.suggestion,
        }


class ArchicadConnectionError(ArchicadError):
    """Archicad not reachable on port."""

    pass


class CommandError(ArchicadError):
    """Command execution failed."""

    pass


class TapirNotAvailableError(ArchicadError):
    """Tapir add-on not installed."""

    pass


class ScriptError(ArchicadError):
    """Python script execution failed."""

    pass


class ScriptTimeoutError(ArchicadError):
    """Script execution timed out."""

    pass
