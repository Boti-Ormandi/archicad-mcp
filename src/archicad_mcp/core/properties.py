"""Property discovery and caching for Archicad elements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from archicad_mcp.core.connection import ArchicadConnection


def _normalize(text: str) -> str:
    """Normalize text for case-insensitive comparison."""
    return text.lower().strip()


def _format_property(prop: dict[str, Any]) -> dict[str, Any]:
    """Format raw API property to output format."""
    prop_id = prop.get("propertyId", {})
    return {
        "name": prop.get("propertyName", ""),
        "group": prop.get("propertyGroupName", ""),
        "guid": prop_id.get("guid", "") if isinstance(prop_id, dict) else "",
        "type": prop.get("propertyType", ""),
        "value_type": prop.get("propertyValueType", ""),
        "measure_type": prop.get("propertyMeasureType", "Default"),
        "editable": prop.get("propertyIsEditable", False),
    }


class PropertyCache:
    """Caches property definitions per Archicad instance.

    Properties don't change during a session, so we fetch once and cache.
    """

    def __init__(self) -> None:
        self._cache: dict[int, list[dict[str, Any]]] = {}  # port -> properties

    async def get_properties(self, conn: ArchicadConnection) -> list[dict[str, Any]]:
        """Get all properties for the connection, caching on first call.

        Args:
            conn: Archicad connection

        Returns:
            List of raw property dicts from GetAllProperties
        """
        if conn.port not in self._cache:
            result = await conn.execute("GetAllProperties", {})
            props: list[dict[str, Any]] = result.get("properties", [])  # type: ignore[assignment]
            self._cache[conn.port] = props
        return self._cache[conn.port]

    def clear(self, port: int | None = None) -> None:
        """Clear cache for a specific port or all ports."""
        if port is not None:
            self._cache.pop(port, None)
        else:
            self._cache.clear()


def filter_properties(
    properties: list[dict[str, Any]],
    *,
    group: str | None = None,
    property_type: str | None = None,
    measure_type: str | None = None,
) -> list[dict[str, Any]]:
    """Filter properties by group, type, and measure.

    Args:
        properties: List of raw properties from API
        group: Filter by group name (case-insensitive, partial match)
        property_type: Filter by type (StaticBuiltIn, DynamicBuiltIn, Custom)
        measure_type: Filter by measure (Length, Area, Volume, Angle, Default)

    Returns:
        Filtered list of properties
    """
    results = properties

    if group:
        group_lower = _normalize(group)
        results = [p for p in results if group_lower in _normalize(p.get("propertyGroupName", ""))]

    if property_type:
        results = [p for p in results if p.get("propertyType") == property_type]

    if measure_type:
        results = [p for p in results if p.get("propertyMeasureType", "Default") == measure_type]

    return results


def search_properties(
    properties: list[dict[str, Any]],
    query: str,
) -> list[tuple[dict[str, Any], int]]:
    """Search properties by name with scoring.

    Args:
        properties: List of raw properties from API
        query: Search query

    Returns:
        List of (property, score) tuples, sorted by score descending
    """
    query_lower = _normalize(query)
    query_tokens = query_lower.split()
    scored: list[tuple[dict[str, Any], int]] = []

    for prop in properties:
        name = prop.get("propertyName", "")
        name_lower = _normalize(name)

        # Exact match gets highest score
        if name_lower == query_lower:
            scored.append((prop, 1000))
            continue

        # Contains full query
        if query_lower in name_lower:
            score = 500 + (100 - len(name_lower))  # Prefer shorter names
            scored.append((prop, score))
            continue

        # Token matching
        token_score = 0
        for token in query_tokens:
            if token in name_lower:
                token_score += 100
            elif any(word.startswith(token) for word in name_lower.split()):
                token_score += 50

        if token_score > 0:
            scored.append((prop, token_score))
            continue

        # Fuzzy matching for typo tolerance (optional, only if rapidfuzz available)
        try:
            from rapidfuzz import fuzz

            ratio = fuzz.partial_ratio(query_lower, name_lower)
            if ratio >= 75:
                scored.append((prop, int(ratio) // 2))
        except ImportError:
            pass

    # Sort by score descending
    scored.sort(key=lambda x: -x[1])
    return scored


def exact_lookup(
    properties: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    """Find property by exact name (case-insensitive).

    Args:
        properties: List of raw properties from API
        name: Exact property name to find

    Returns:
        Matching property or None
    """
    name_lower = _normalize(name)
    for prop in properties:
        if _normalize(prop.get("propertyName", "")) == name_lower:
            return prop
    return None


def get_groups_summary(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Get summary of property groups with counts.

    Args:
        properties: List of raw properties from API

    Returns:
        List of {name, count} sorted by count descending
    """
    counts: dict[str, int] = {}
    for prop in properties:
        group = prop.get("propertyGroupName", "Other")
        counts[group] = counts.get(group, 0) + 1

    return sorted(
        [{"name": name, "count": count} for name, count in counts.items()],
        key=lambda x: -x["count"],
    )


def get_type_summary(properties: list[dict[str, Any]]) -> dict[str, int]:
    """Get summary of property types with counts.

    Args:
        properties: List of raw properties from API

    Returns:
        Dict of type -> count
    """
    counts: dict[str, int] = {}
    for prop in properties:
        ptype = prop.get("propertyType", "Unknown")
        counts[ptype] = counts.get(ptype, 0) + 1
    return counts


def find_similar_groups(
    properties: list[dict[str, Any]],
    query: str,
) -> list[str]:
    """Find groups similar to query for suggestions.

    Args:
        properties: List of raw properties from API
        query: Group name that didn't match

    Returns:
        List of similar group names
    """
    query_lower = _normalize(query)
    groups = {prop.get("propertyGroupName", "") for prop in properties}

    suggestions = []
    for group in groups:
        group_lower = _normalize(group)
        if query_lower in group_lower or group_lower.startswith(query_lower[:3]):
            suggestions.append(group)

    # Fuzzy match if rapidfuzz available
    if not suggestions:
        try:
            from rapidfuzz import fuzz

            for group in groups:
                if fuzz.ratio(query_lower, _normalize(group)) >= 70:
                    suggestions.append(group)
        except ImportError:
            pass

    return sorted(suggestions)[:3]
