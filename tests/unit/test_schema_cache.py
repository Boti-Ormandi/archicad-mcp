"""Unit tests for SchemaCache."""

import pytest

from archicad_mcp.schemas.cache import SchemaCache


@pytest.fixture
def cache() -> SchemaCache:
    """Create and load a SchemaCache instance."""
    c = SchemaCache()
    c.load_embedded()
    return c


class TestLoadEmbedded:
    """Tests for schema loading."""

    def test_loads_commands(self, cache: SchemaCache) -> None:
        """Should load commands from embedded JSON files."""
        assert len(cache.commands) > 0

    def test_loads_tapir_commands(self, cache: SchemaCache) -> None:
        """Should load Tapir commands with correct API tag."""
        assert "CreateColumns" in cache.commands
        assert cache.commands["CreateColumns"]["api"] == "tapir"

    def test_loads_builtin_commands(self, cache: SchemaCache) -> None:
        """Should load built-in API commands with correct API tag."""
        assert "API.GetAllElements" in cache.commands
        assert cache.commands["API.GetAllElements"]["api"] == "builtin"

    def test_builds_categories(self, cache: SchemaCache) -> None:
        """Should build category list from loaded commands."""
        assert len(cache.categories) > 0
        assert "Element Commands" in cache.categories

    def test_loads_element_types(self, cache: SchemaCache) -> None:
        """Should load element types from Tapir schema."""
        assert len(cache.element_types) > 0
        assert "Wall" in cache.element_types
        assert "Column" in cache.element_types

    def test_idempotent(self, cache: SchemaCache) -> None:
        """Should only load once even if called multiple times."""
        initial_count = len(cache.commands)
        cache.load_embedded()
        cache.load_embedded()
        assert len(cache.commands) == initial_count


class TestGetCommand:
    """Tests for single command lookup."""

    def test_returns_command_schema(self, cache: SchemaCache) -> None:
        """Should return full schema for existing command."""
        result = cache.get_command("CreateColumns")
        assert result is not None
        assert result["name"] == "CreateColumns"
        assert result["api"] == "tapir"
        assert "parameters" in result

    def test_returns_none_for_missing(self, cache: SchemaCache) -> None:
        """Should return None for non-existent command."""
        result = cache.get_command("NonExistentCommand")
        assert result is None

    def test_includes_enriched_fields(self, cache: SchemaCache) -> None:
        """Should include parameters and returns for commands with schemas."""
        result = cache.get_command("CreateColumns")
        assert result is not None
        assert "parameters" in result
        assert "returns" in result
        # Example is optional, only present in enriched commands
        # assert "example" in result


class TestGetCommands:
    """Tests for batch command lookup."""

    def test_returns_multiple_commands(self, cache: SchemaCache) -> None:
        """Should return schemas for multiple commands."""
        result = cache.get_commands(["CreateColumns", "CreateSlabs"])
        assert len(result["commands"]) == 2
        names = [c["name"] for c in result["commands"]]
        assert "CreateColumns" in names
        assert "CreateSlabs" in names

    def test_includes_not_found(self, cache: SchemaCache) -> None:
        """Should include not_found list for missing commands."""
        result = cache.get_commands(["CreateColumns", "FakeCommand"])
        assert len(result["commands"]) == 1
        assert "not_found" in result
        assert "FakeCommand" in result["not_found"]

    def test_no_not_found_when_all_exist(self, cache: SchemaCache) -> None:
        """Should not include not_found when all commands exist."""
        result = cache.get_commands(["CreateColumns", "CreateSlabs"])
        assert "not_found" not in result


class TestSearch:
    """Tests for search functionality."""

    def test_finds_by_name(self, cache: SchemaCache) -> None:
        """Should find commands by name match."""
        result = cache.search("Column")
        assert result["total"] > 0
        names = [c["name"] for c in result["results"]]
        assert "CreateColumns" in names

    def test_finds_by_description(self, cache: SchemaCache) -> None:
        """Should find commands by description match."""
        result = cache.search("property values")
        assert result["total"] > 0
        # Should find GetPropertyValuesOfElements or SetPropertyValuesOfElements
        names = [c["name"] for c in result["results"]]
        assert any("Property" in n for n in names)

    def test_case_insensitive(self, cache: SchemaCache) -> None:
        """Should be case-insensitive."""
        result_upper = cache.search("COLUMN")
        result_lower = cache.search("column")
        assert result_upper["total"] == result_lower["total"]

    def test_limits_results(self, cache: SchemaCache) -> None:
        """Should limit results to default limit."""
        result = cache.search("e")  # Very broad search
        assert len(result["results"]) <= 20  # Default limit is 20

    def test_returns_brief_info(self, cache: SchemaCache) -> None:
        """Should return brief info, not full schema."""
        result = cache.search("CreateColumns")
        cmd = result["results"][0]
        assert "name" in cmd
        assert "description" in cmd
        assert "has_details" in cmd
        # Should not include full parameters in search results
        assert "parameters" not in cmd or cmd.get("has_details") is True

    def test_prioritizes_name_matches(self, cache: SchemaCache) -> None:
        """Should prioritize name matches over description matches."""
        result = cache.search("CreateColumns")
        # First result should be exact name match
        assert result["results"][0]["name"] == "CreateColumns"


class TestGetCategory:
    """Tests for category filtering."""

    def test_returns_category_commands(self, cache: SchemaCache) -> None:
        """Should return all commands in category."""
        result = cache.get_category("Element Commands")
        assert result["category"] == "Element Commands"
        assert result["total"] > 0
        # All returned commands should be in Element Commands
        for cmd in result["commands"]:
            full_cmd = cache.get_command(cmd["name"])
            assert full_cmd is not None
            assert full_cmd["category"] == "Element Commands"

    def test_sorted_by_name(self, cache: SchemaCache) -> None:
        """Should sort commands by name."""
        result = cache.get_category("Element Commands")
        names = [c["name"] for c in result["commands"]]
        assert names == sorted(names)

    def test_empty_for_unknown_category(self, cache: SchemaCache) -> None:
        """Should return empty list for unknown category."""
        result = cache.get_category("NonExistent Category")
        assert result["total"] == 0
        assert len(result["commands"]) == 0


class TestGetSummary:
    """Tests for summary generation."""

    def test_returns_total_count(self, cache: SchemaCache) -> None:
        """Should return total command count."""
        result = cache.get_summary()
        assert result["total_commands"] > 0
        assert result["total_commands"] == len(cache.commands)

    def test_returns_api_counts(self, cache: SchemaCache) -> None:
        """Should return counts per API."""
        result = cache.get_summary()
        assert "tapir_commands" in result
        assert "builtin_commands" in result
        assert result["tapir_commands"] + result["builtin_commands"] == result["total_commands"]

    def test_returns_category_counts(self, cache: SchemaCache) -> None:
        """Should return counts per category."""
        result = cache.get_summary()
        assert "categories" in result
        assert isinstance(result["categories"], dict)
        assert "Element Commands" in result["categories"]

    def test_returns_element_types(self, cache: SchemaCache) -> None:
        """Should return element types list."""
        result = cache.get_summary()
        assert "element_types" in result
        assert "Wall" in result["element_types"]

    def test_includes_tip(self, cache: SchemaCache) -> None:
        """Should include usage tip."""
        result = cache.get_summary()
        assert "tip" in result


class TestAutoLoad:
    """Tests for automatic loading."""

    def test_auto_loads_on_get_command(self) -> None:
        """Should auto-load schemas when accessing commands."""
        cache = SchemaCache()
        # Don't call load_embedded
        result = cache.get_command("CreateColumns")
        assert result is not None

    def test_auto_loads_on_search(self) -> None:
        """Should auto-load schemas when searching."""
        cache = SchemaCache()
        result = cache.search("Column")
        assert result["total"] > 0

    def test_auto_loads_on_summary(self) -> None:
        """Should auto-load schemas when getting summary."""
        cache = SchemaCache()
        result = cache.get_summary()
        assert result["total_commands"] > 0
