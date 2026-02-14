"""Unit tests for error classes."""

import pytest

from archicad_mcp.core.errors import (
    ArchicadConnectionError,
    ArchicadError,
    CommandError,
    ScriptError,
    ScriptTimeoutError,
    TapirNotAvailableError,
)


class TestArchicadError:
    """Tests for base ArchicadError class."""

    def test_basic_creation(self) -> None:
        """Error can be created with just a message."""
        err = ArchicadError("Something went wrong")
        assert err.message == "Something went wrong"
        assert err.details == {}
        assert err.suggestion == ""

    def test_with_details(self) -> None:
        """Error can include details dict."""
        err = ArchicadError(
            "Connection failed",
            details={"port": 19723, "timeout": 5.0},
        )
        assert err.details == {"port": 19723, "timeout": 5.0}

    def test_with_suggestion(self) -> None:
        """Error can include actionable suggestion."""
        err = ArchicadError(
            "Connection failed",
            suggestion="Check if Archicad is running",
        )
        assert err.suggestion == "Check if Archicad is running"

    def test_to_dict_serialization(self) -> None:
        """to_dict() returns structured error for AI."""
        err = ArchicadError(
            "Test error",
            details={"key": "value"},
            suggestion="Try this fix",
        )
        result = err.to_dict()

        assert result["type"] == "ArchicadError"
        assert result["message"] == "Test error"
        assert result["details"] == {"key": "value"}
        assert result["suggestion"] == "Try this fix"

    def test_inherits_from_exception(self) -> None:
        """ArchicadError is a proper Exception."""
        err = ArchicadError("Test")
        assert isinstance(err, Exception)
        assert str(err) == "Test"


class TestSpecificErrors:
    """Tests for specific error subclasses."""

    @pytest.mark.parametrize(
        ("error_class", "expected_type"),
        [
            (ArchicadConnectionError, "ArchicadConnectionError"),
            (CommandError, "CommandError"),
            (TapirNotAvailableError, "TapirNotAvailableError"),
            (ScriptError, "ScriptError"),
            (ScriptTimeoutError, "ScriptTimeoutError"),
        ],
    )
    def test_subclass_type_in_to_dict(
        self,
        error_class: type[ArchicadError],
        expected_type: str,
    ) -> None:
        """Each subclass reports its own type in to_dict()."""
        err = error_class("Test message")
        assert err.to_dict()["type"] == expected_type

    def test_all_inherit_from_archicad_error(self) -> None:
        """All error classes inherit from ArchicadError."""
        for error_class in [
            ArchicadConnectionError,
            CommandError,
            TapirNotAvailableError,
            ScriptError,
            ScriptTimeoutError,
        ]:
            err = error_class("Test")
            assert isinstance(err, ArchicadError)

    def test_connection_error_example(self) -> None:
        """ArchicadConnectionError with typical usage."""
        err = ArchicadConnectionError(
            "Cannot connect to Archicad on port 19723",
            details={"port": 19723, "error": "Connection refused"},
            suggestion="Ensure Archicad is running with JSON API enabled",
        )
        d = err.to_dict()
        assert d["type"] == "ArchicadConnectionError"
        assert "19723" in d["message"]
        assert d["details"]["port"] == 19723

    def test_tapir_not_available_example(self) -> None:
        """TapirNotAvailableError with typical usage."""
        err = TapirNotAvailableError(
            "Tapir add-on is not installed",
            details={"port": 19723, "command": "GetProjectInfo"},
            suggestion="Install Tapir from https://github.com/ENZYME-APD/tapir-archicad-automation/releases",
        )
        d = err.to_dict()
        assert d["type"] == "TapirNotAvailableError"
        assert "github.com" in d["suggestion"]
