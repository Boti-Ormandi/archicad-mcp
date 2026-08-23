"""Unit tests for Pydantic models."""

from typing import Any

import pytest
from pydantic import ValidationError

from archicad_mcp.models import (
    ArchicadInstance,
    CommandDoc,
    DocSearchResult,
    ScriptResult,
)


class TestMultipleInstancesTapirVersions:
    """Instances on different add-on releases report differing versions."""

    def test_instances_may_differ_in_tapir_version(self) -> None:
        first = ArchicadInstance(
            port=19723,
            project_name="A",
            project_path=None,
            project_type="solo",
            archicad_version="29",
            is_tapir_available=True,
            tapir_version="1.5.8",
        )
        second = ArchicadInstance(
            port=19724,
            project_name="B",
            project_path=None,
            project_type="solo",
            archicad_version="28",
            is_tapir_available=True,
            tapir_version="1.4.0",
        )
        assert {first.tapir_version, second.tapir_version} == {"1.5.8", "1.4.0"}


class TestArchicadInstance:
    """Tests for ArchicadInstance model."""

    def test_valid_solo_project(self) -> None:
        """Valid solo project instance."""
        instance = ArchicadInstance(
            port=19723,
            project_name="My Project",
            project_path="C:/Projects/test.pln",
            project_type="solo",
            archicad_version="27.0.0",
            is_tapir_available=True,
        )
        assert instance.port == 19723
        assert instance.project_type == "solo"

    def test_valid_teamwork_project(self) -> None:
        """Valid teamwork project instance."""
        instance = ArchicadInstance(
            port=19724,
            project_name="Team Project",
            project_path=None,
            project_type="teamwork",
            archicad_version="27.0.0",
            is_tapir_available=False,
        )
        assert instance.project_type == "teamwork"
        assert instance.project_path is None

    def test_valid_untitled_project(self) -> None:
        """Valid untitled project instance."""
        instance = ArchicadInstance(
            port=19723,
            project_name="Untitled",
            project_path=None,
            project_type="untitled",
            archicad_version="26.0.0",
            is_tapir_available=True,
        )
        assert instance.project_type == "untitled"

    def test_invalid_project_type(self) -> None:
        """Invalid project_type raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            ArchicadInstance(
                port=19723,
                project_name="Test",
                project_path=None,
                project_type="invalid",  # type: ignore[arg-type]
                archicad_version="27.0.0",
                is_tapir_available=True,
            )
        assert "project_type" in str(exc_info.value)

    def test_tapir_version_defaults_to_null_and_serializes_additively(self) -> None:
        """tapir_version is optional, defaults to null, and always serializes."""
        instance = ArchicadInstance(
            port=19723,
            project_name="Test",
            project_path=None,
            project_type="solo",
            archicad_version="29",
            is_tapir_available=True,
        )
        assert instance.tapir_version is None
        payload: dict[str, Any] = dict(instance.model_dump())
        assert payload["tapir_version"] is None

    def test_tapir_version_is_retained_when_reported(self) -> None:
        """An exactly reported Tapir version is retained verbatim."""
        instance = ArchicadInstance(
            port=19724,
            project_name="Test",
            project_path=None,
            project_type="solo",
            archicad_version="29",
            is_tapir_available=True,
            tapir_version="1.5.8",
        )
        assert instance.tapir_version == "1.5.8"
        assert instance.model_dump()["tapir_version"] == "1.5.8"


class TestScriptResult:
    """Tests for ScriptResult model."""

    def test_successful_result(self) -> None:
        """Successful script execution."""
        result = ScriptResult(
            success=True,
            result={"count": 42, "elements": ["a", "b"]},
            stdout="Processing complete\n",
            error=None,
            execution_time_ms=150,
        )
        assert result.success is True
        assert result.result is not None
        assert result.result["count"] == 42
        assert result.error is None

    def test_failed_result(self) -> None:
        """Failed script execution."""
        result = ScriptResult(
            success=False,
            result=None,
            stdout="",
            error="Line 5: NameError: name 'foo' is not defined",
            execution_time_ms=10,
        )
        assert result.success is False
        assert result.result is None
        assert "NameError" in str(result.error)

    def test_result_can_be_any_type(self) -> None:
        """Result field accepts any JSON-serializable value."""
        # List
        r1 = ScriptResult(
            success=True, result=[1, 2, 3], stdout="", error=None, execution_time_ms=5
        )
        assert r1.result == [1, 2, 3]

        # String
        r2 = ScriptResult(success=True, result="done", stdout="", error=None, execution_time_ms=5)
        assert r2.result == "done"

        # None
        r3 = ScriptResult(success=True, result=None, stdout="", error=None, execution_time_ms=5)
        assert r3.result is None


class TestCommandDoc:
    """Tests for CommandDoc model."""

    def test_minimal_doc(self) -> None:
        """Minimal command documentation."""
        doc = CommandDoc(
            name="GetAllElements",
            api="tapir",
            category="Element Commands",
            description="Returns all elements in the project",
        )
        assert doc.name == "GetAllElements"
        assert doc.api == "tapir"
        assert doc.parameters_schema is None
        assert doc.examples == []

    def test_full_doc(self) -> None:
        """Full command documentation with all fields."""
        doc = CommandDoc(
            name="CreateColumns",
            api="tapir",
            category="Element Creation",
            description="Creates column elements",
            parameters_schema={
                "type": "object",
                "properties": {"columns": {"type": "array"}},
            },
            returns_schema={
                "type": "object",
                "properties": {"guids": {"type": "array"}},
            },
            examples=[
                {"input": {"columns": []}, "output": {"guids": []}},
            ],
        )
        assert doc.parameters_schema is not None
        assert len(doc.examples) == 1

    def test_invalid_api_type(self) -> None:
        """Invalid api type raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            CommandDoc(
                name="Test",
                api="invalid",  # type: ignore[arg-type]
                category="Test",
                description="Test",
            )
        assert "api" in str(exc_info.value)


class TestDocSearchResult:
    """Tests for DocSearchResult model."""

    def test_empty_result(self) -> None:
        """Empty search result."""
        result = DocSearchResult(total=0, commands=[])
        assert result.total == 0
        assert result.commands == []
        assert result.categories is None

    def test_with_results(self) -> None:
        """Search result with commands."""
        doc = CommandDoc(
            name="Test",
            api="builtin",
            category="Test",
            description="Test command",
        )
        result = DocSearchResult(
            total=1,
            commands=[doc],
            categories=["Test", "Other"],
        )
        assert result.total == 1
        assert len(result.commands) == 1
        assert result.categories == ["Test", "Other"]
