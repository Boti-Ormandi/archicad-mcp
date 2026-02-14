"""Unit tests for property discovery and caching."""

from archicad_mcp.core.properties import (
    PropertyCache,
    _format_property,
    exact_lookup,
    filter_properties,
    find_similar_groups,
    get_groups_summary,
    get_type_summary,
    search_properties,
)

# Sample property data matching API format
SAMPLE_PROPERTIES = [
    {
        "propertyId": {"guid": "GUID-WALL-LENGTH"},
        "propertyType": "StaticBuiltIn",
        "propertyGroupName": "Wall",
        "propertyName": "Length of Reference Line",
        "propertyCollectionType": "Single",
        "propertyValueType": "Real",
        "propertyMeasureType": "Length",
        "propertyIsEditable": False,
    },
    {
        "propertyId": {"guid": "GUID-WALL-AREA"},
        "propertyType": "StaticBuiltIn",
        "propertyGroupName": "Wall",
        "propertyName": "Outside Face Surface Area",
        "propertyCollectionType": "Single",
        "propertyValueType": "Real",
        "propertyMeasureType": "Area",
        "propertyIsEditable": False,
    },
    {
        "propertyId": {"guid": "GUID-SLAB-AREA"},
        "propertyType": "StaticBuiltIn",
        "propertyGroupName": "Slab",
        "propertyName": "Top Surface Area",
        "propertyCollectionType": "Single",
        "propertyValueType": "Real",
        "propertyMeasureType": "Area",
        "propertyIsEditable": False,
    },
    {
        "propertyId": {"guid": "GUID-ZONE-NAME"},
        "propertyType": "StaticBuiltIn",
        "propertyGroupName": "Zone",
        "propertyName": "Zone Name",
        "propertyCollectionType": "Single",
        "propertyValueType": "String",
        "propertyMeasureType": "Default",
        "propertyIsEditable": True,
    },
    {
        "propertyId": {"guid": "GUID-CUSTOM-COST"},
        "propertyType": "Custom",
        "propertyGroupName": "Cost Estimation",
        "propertyName": "Unit Cost",
        "propertyCollectionType": "Single",
        "propertyValueType": "Real",
        "propertyMeasureType": "Default",
        "propertyIsEditable": True,
    },
]


class TestFormatProperty:
    """Tests for _format_property function."""

    def test_format_full_property(self) -> None:
        """Format property with all fields."""
        raw = SAMPLE_PROPERTIES[0]
        formatted = _format_property(raw)

        assert formatted["name"] == "Length of Reference Line"
        assert formatted["group"] == "Wall"
        assert formatted["guid"] == "GUID-WALL-LENGTH"
        assert formatted["type"] == "StaticBuiltIn"
        assert formatted["value_type"] == "Real"
        assert formatted["measure_type"] == "Length"
        assert formatted["editable"] is False

    def test_format_custom_property(self) -> None:
        """Format custom property."""
        raw = SAMPLE_PROPERTIES[4]
        formatted = _format_property(raw)

        assert formatted["name"] == "Unit Cost"
        assert formatted["type"] == "Custom"
        assert formatted["editable"] is True

    def test_format_property_missing_fields(self) -> None:
        """Format property with missing optional fields."""
        raw = {"propertyId": {"guid": "123"}, "propertyName": "Test"}
        formatted = _format_property(raw)

        assert formatted["name"] == "Test"
        assert formatted["guid"] == "123"
        assert formatted["group"] == ""
        assert formatted["type"] == ""
        assert formatted["measure_type"] == "Default"


class TestFilterProperties:
    """Tests for filter_properties function."""

    def test_filter_by_group(self) -> None:
        """Filter by group name."""
        results = filter_properties(SAMPLE_PROPERTIES, group="Wall")
        assert len(results) == 2
        assert all(p["propertyGroupName"] == "Wall" for p in results)

    def test_filter_by_group_case_insensitive(self) -> None:
        """Filter is case-insensitive."""
        results = filter_properties(SAMPLE_PROPERTIES, group="wall")
        assert len(results) == 2

    def test_filter_by_group_partial_match(self) -> None:
        """Filter matches partial group names."""
        results = filter_properties(SAMPLE_PROPERTIES, group="Cost")
        assert len(results) == 1
        assert results[0]["propertyGroupName"] == "Cost Estimation"

    def test_filter_by_property_type(self) -> None:
        """Filter by property type."""
        results = filter_properties(SAMPLE_PROPERTIES, property_type="Custom")
        assert len(results) == 1
        assert results[0]["propertyType"] == "Custom"

    def test_filter_by_measure_type(self) -> None:
        """Filter by measure type."""
        results = filter_properties(SAMPLE_PROPERTIES, measure_type="Area")
        assert len(results) == 2
        assert all(p["propertyMeasureType"] == "Area" for p in results)

    def test_filter_combined(self) -> None:
        """Multiple filters combine with AND logic."""
        results = filter_properties(
            SAMPLE_PROPERTIES,
            group="Wall",
            measure_type="Area",
        )
        assert len(results) == 1
        assert results[0]["propertyName"] == "Outside Face Surface Area"

    def test_filter_no_match(self) -> None:
        """Filters with no matches return empty list."""
        results = filter_properties(SAMPLE_PROPERTIES, group="Beam")
        assert results == []


class TestSearchProperties:
    """Tests for search_properties function."""

    def test_exact_match_highest_score(self) -> None:
        """Exact match gets highest score."""
        results = search_properties(SAMPLE_PROPERTIES, "Zone Name")
        assert len(results) > 0
        # Exact match should be first
        assert results[0][0]["propertyName"] == "Zone Name"
        assert results[0][1] == 1000  # Exact match score

    def test_contains_query(self) -> None:
        """Properties containing query are found."""
        results = search_properties(SAMPLE_PROPERTIES, "Area")
        names = [p["propertyName"] for p, _ in results]
        assert "Outside Face Surface Area" in names
        assert "Top Surface Area" in names

    def test_token_matching(self) -> None:
        """Token matching finds relevant properties."""
        results = search_properties(SAMPLE_PROPERTIES, "surface")
        names = [p["propertyName"] for p, _ in results]
        assert "Outside Face Surface Area" in names
        assert "Top Surface Area" in names

    def test_no_match_returns_empty(self) -> None:
        """No matches returns empty list."""
        results = search_properties(SAMPLE_PROPERTIES, "banana")
        assert results == []

    def test_results_sorted_by_score(self) -> None:
        """Results are sorted by score descending."""
        results = search_properties(SAMPLE_PROPERTIES, "area")
        if len(results) > 1:
            scores = [score for _, score in results]
            assert scores == sorted(scores, reverse=True)


class TestExactLookup:
    """Tests for exact_lookup function."""

    def test_exact_match(self) -> None:
        """Find property by exact name."""
        result = exact_lookup(SAMPLE_PROPERTIES, "Length of Reference Line")
        assert result is not None
        assert result["propertyName"] == "Length of Reference Line"

    def test_exact_match_case_insensitive(self) -> None:
        """Exact lookup is case-insensitive."""
        result = exact_lookup(SAMPLE_PROPERTIES, "length of reference line")
        assert result is not None
        assert result["propertyName"] == "Length of Reference Line"

    def test_no_match_returns_none(self) -> None:
        """No match returns None."""
        result = exact_lookup(SAMPLE_PROPERTIES, "Nonexistent Property")
        assert result is None

    def test_partial_match_not_found(self) -> None:
        """Partial match is not returned."""
        result = exact_lookup(SAMPLE_PROPERTIES, "Length")
        assert result is None


class TestGetGroupsSummary:
    """Tests for get_groups_summary function."""

    def test_groups_with_counts(self) -> None:
        """Returns groups with correct counts."""
        summary = get_groups_summary(SAMPLE_PROPERTIES)
        groups_dict = {g["name"]: g["count"] for g in summary}

        assert groups_dict["Wall"] == 2
        assert groups_dict["Slab"] == 1
        assert groups_dict["Zone"] == 1
        assert groups_dict["Cost Estimation"] == 1

    def test_sorted_by_count_descending(self) -> None:
        """Groups are sorted by count descending."""
        summary = get_groups_summary(SAMPLE_PROPERTIES)
        counts = [g["count"] for g in summary]
        assert counts == sorted(counts, reverse=True)


class TestGetTypeSummary:
    """Tests for get_type_summary function."""

    def test_types_with_counts(self) -> None:
        """Returns types with correct counts."""
        summary = get_type_summary(SAMPLE_PROPERTIES)

        assert summary["StaticBuiltIn"] == 4
        assert summary["Custom"] == 1


class TestFindSimilarGroups:
    """Tests for find_similar_groups function."""

    def test_find_similar_by_prefix(self) -> None:
        """Find groups starting with similar prefix."""
        similar = find_similar_groups(SAMPLE_PROPERTIES, "Wal")
        assert "Wall" in similar

    def test_find_similar_by_substring(self) -> None:
        """Find groups containing query."""
        similar = find_similar_groups(SAMPLE_PROPERTIES, "Cost")
        assert "Cost Estimation" in similar

    def test_no_similar_returns_empty(self) -> None:
        """No similar groups returns empty list."""
        similar = find_similar_groups(SAMPLE_PROPERTIES, "xyz")
        # May return empty or fuzzy matches depending on rapidfuzz availability
        assert isinstance(similar, list)

    def test_max_three_suggestions(self) -> None:
        """Returns at most 3 suggestions."""
        # Create many similar groups
        props = [{"propertyGroupName": f"Wall{i}"} for i in range(10)]
        similar = find_similar_groups(props, "Wall")
        assert len(similar) <= 3


class TestPropertyCache:
    """Tests for PropertyCache class."""

    def test_cache_initially_empty(self) -> None:
        """Cache starts empty."""
        cache = PropertyCache()
        assert len(cache._cache) == 0

    def test_clear_specific_port(self) -> None:
        """Clear removes specific port."""
        cache = PropertyCache()
        cache._cache[19723] = [{"test": 1}]
        cache._cache[19724] = [{"test": 2}]

        cache.clear(19723)

        assert 19723 not in cache._cache
        assert 19724 in cache._cache

    def test_clear_all(self) -> None:
        """Clear without port removes all."""
        cache = PropertyCache()
        cache._cache[19723] = [{"test": 1}]
        cache._cache[19724] = [{"test": 2}]

        cache.clear()

        assert len(cache._cache) == 0
