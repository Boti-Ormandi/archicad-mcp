"""Dynamic docstring generation from Archicad command schemas."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# =============================================================================
# Common $ref Resolution
# =============================================================================

REF_RESOLUTIONS = {
    "#/Elements": "[{elementId: {guid}}]",
    "#/ElementId": "{guid}",
    "#/ElementIds": "[{elementId: {guid}}]",
    "#/Coordinate2D": "{x, y}",
    "#/Coordinate3D": "{x, y, z}",
    "#/ExecutionResult": "{success: bool}",
    "#/PolyArc": "{begIndex, endIndex, arcAngle}",
    "#/Holes2D": "[{polygonCoordinates: [{x, y}]}]",
    "#/PropertyId": "{guid}",
    "#/PropertyIds": "[{propertyId: {guid}}]",
    "#/ClassificationId": "{classificationSystemId, classificationItemId}",
}


def _schema_to_compact(
    schema: dict[str, Any],
    common_schemas: dict[str, Any] | None = None,
    depth: int = 0,
) -> str:
    """Convert a JSON schema to compact representation.

    Args:
        schema: JSON schema dict
        common_schemas: Tapir common schema definitions for $ref resolution.
            When provided, unresolved $refs are expanded from these definitions
            automatically, so no manual REF_RESOLUTIONS entry is needed.
        depth: Current nesting depth (to limit recursion)

    Returns:
        Compact string representation like "{x, y, z}" or "[{elementId}]"
    """
    if depth > 3:
        return "..."

    # Handle $ref first
    if "$ref" in schema:
        ref: str = schema["$ref"]
        # 1) Explicit overrides for common patterns
        if ref in REF_RESOLUTIONS:
            return REF_RESOLUTIONS[ref]
        # 2) Auto-resolve from common_schemas (zero maintenance)
        if common_schemas:
            ref_name = ref.lstrip("#/")
            # Handle #/$defs/Name format (built-in API)
            if ref_name.startswith("$defs/"):
                ref_name = ref_name[6:]
            if ref_name in common_schemas:
                return _schema_to_compact(common_schemas[ref_name], common_schemas, depth)
        # 3) Fallback: bare type name
        return ref.replace("#/$defs/", "").replace("#/", "")

    schema_type = schema.get("type")

    if schema_type == "object":
        props = schema.get("properties", {})
        if not props:
            return "{}"

        parts = []

        for name, prop_schema in props.items():
            prop_value = _schema_to_compact(prop_schema, common_schemas, depth + 1)
            parts.append(f"{name}: {prop_value}" if prop_value != "..." else name)

        return "{" + ", ".join(parts) + "}"

    elif schema_type == "array":
        items = schema.get("items", {})
        item_compact = _schema_to_compact(items, common_schemas, depth + 1)
        return f"[{item_compact}]"

    elif schema_type == "string":
        # Check for enum
        if "enum" in schema:
            return "|".join(f'"{v}"' for v in schema["enum"][:3])
        return "str"

    elif schema_type == "number":
        return "num"

    elif schema_type == "integer":
        return "int"

    elif schema_type == "boolean":
        return "bool"

    # oneOf/anyOf - just take first option
    elif "oneOf" in schema or "anyOf" in schema:
        options = schema.get("oneOf") or schema.get("anyOf", [])
        if options:
            return _schema_to_compact(options[0], common_schemas, depth + 1)

    return "any"


def generate_compact_schema(
    cmd_name: str,
    cmd_data: dict[str, Any],
    common_schemas: dict[str, Any] | None = None,
) -> str | None:
    """Generate compact schema representation for a command.

    Args:
        cmd_name: Command name (e.g., "CreateColumns")
        cmd_data: Command schema data
        common_schemas: Tapir common schema definitions for $ref resolution.

    Returns:
        Compact representation string, or None if generation fails.
        Example:
            CreateColumns(columnsData: [{coordinates: {x, y, z}}])
              -> {elements: [{elementId: {guid}}]}
              Creates Column elements.
    """
    try:
        # Build parameter signature
        params_schema = cmd_data.get("parameters", {})
        if params_schema and params_schema.get("properties"):
            props = params_schema["properties"]
            param_parts = []
            for pname, pschema in props.items():
                pcompact = _schema_to_compact(pschema, common_schemas)
                param_parts.append(f"{pname}: {pcompact}")
            params_str = ", ".join(param_parts)
        else:
            params_str = ""

        # Build return signature
        returns_schema = cmd_data.get("returns", {})
        returns_str = (
            _schema_to_compact(returns_schema, common_schemas) if returns_schema else "void"
        )

        # Get description (truncate if too long)
        desc = cmd_data.get("description", "")
        if len(desc) > 80:
            desc = desc[:77] + "..."

        # Format output
        lines = [f"{cmd_name}({params_str})"]
        lines.append(f"  -> {returns_str}")
        if desc:
            lines.append(f"  {desc}")

        return "\n".join(lines)

    except Exception as e:
        logger.warning(f"Failed to generate compact schema for {cmd_name}: {e}")
        return None
