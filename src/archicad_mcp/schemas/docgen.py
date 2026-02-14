"""Dynamic docstring generation from Archicad command schemas."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from archicad_mcp.schemas.cache import SchemaCache

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


def _get_element_types(schemas: SchemaCache) -> list[str]:
    """Extract element types from schema data."""
    return list(schemas.common_schemas.get("ElementType", {}).get("enum", []))


def _get_element_filters(schemas: SchemaCache) -> list[str]:
    """Extract element filters from schema data, with fallback."""
    enum = schemas.common_schemas.get("ElementFilter", {}).get("enum", [])
    if enum:
        return list(enum)
    return [
        "IsEditable",
        "IsVisibleByLayer",
        "IsVisibleByRenovation",
        "IsVisibleIn3D",
        "OnActualFloor",
        "OnActualLayout",
        "InMyWorkspace",
        "HasAccessRight",
    ]


def generate_execute_script_docs(
    schemas: SchemaCache,
    file_access_docs: str = "",
) -> str:
    """Generate the full execute_script tool docstring.

    Generates compact signatures for all Tapir and built-in API commands,
    grouped by category. Element types and filters are derived from schema data.

    Args:
        schemas: Loaded SchemaCache with command schemas
        file_access_docs: Pre-formatted file access documentation string

    Returns:
        Complete docstring for execute_script tool.
    """
    lines = [
        "Execute Python script with full Archicad API access.",
        "",
        "SCRIPT NAMESPACE",
        "================",
        "",
        "archicad object (async methods - use await):",
        "  await archicad.tapir(name, params)    # Tapir commands",
        '  await archicad.command(name, params)  # Built-in API (prefix with "API.")',
        "",
        "TAPIR COMMANDS",
        "==============",
        "",
    ]

    # Group tapir commands by category
    by_category: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for cmd_name, cmd_data in schemas.commands.items():
        if cmd_data.get("api") != "tapir":
            continue
        cat = cmd_data.get("category", "Uncategorized")
        by_category.setdefault(cat, []).append((cmd_name, cmd_data))

    # Sort categories, then commands within each
    generated = 0
    for cat in sorted(by_category):
        cmds = sorted(by_category[cat], key=lambda x: x[0])
        for cmd_name, cmd_data in cmds:
            compact = generate_compact_schema(cmd_name, cmd_data, schemas.common_schemas)
            if compact:
                lines.append(compact)
                lines.append("")
                generated += 1

    logger.info(f"Generated docs for {generated} Tapir commands")

    # Group built-in API commands by category
    builtin_by_category: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for cmd_name, cmd_data in schemas.commands.items():
        if cmd_data.get("api") != "builtin":
            continue
        cat = cmd_data.get("category", "Uncategorized")
        builtin_by_category.setdefault(cat, []).append((cmd_name, cmd_data))

    if builtin_by_category:
        lines.extend(
            [
                "BUILT-IN API COMMANDS",
                "=====================",
                'Call via: await archicad.command("API.CommandName", params)',
                "",
            ]
        )
        builtin_generated = 0
        for cat in sorted(builtin_by_category):
            cmds = sorted(builtin_by_category[cat], key=lambda x: x[0])
            for cmd_name, cmd_data in cmds:
                compact = generate_compact_schema(cmd_name, cmd_data, schemas.builtin_defs)
                if compact:
                    lines.append(compact)
                    lines.append("")
                    builtin_generated += 1
        logger.info(f"Generated docs for {builtin_generated} built-in commands")

    # Derive element types and filters from schema
    element_types = _get_element_types(schemas)
    element_filters = _get_element_filters(schemas)

    lines.extend(
        [
            "ELEMENT TYPES",
            "=============",
            ", ".join(element_types),
            "",
            'IMPORTANT: Element types are CASE-SENSITIVE. Use "Wall" not "wall".',
            "Invalid types return an error.",
            "",
            "ELEMENT FILTERS",
            "===============",
            "Use with GetElementsByType, GetAllElements, FilterElements:",
            '  filters: ["IsEditable", "OnActualFloor"]  # Array of strings, NOT objects!',
            "",
            "Valid filters: " + ", ".join(element_filters),
            "",
            "AVAILABLE MODULES (pre-imported, `import x` also works)",
            "=================",
            "json, csv, math, pathlib (Path), re, datetime, itertools, functools, collections, statistics, copy, io, openpyxl",
            "",
        ]
    )

    # Add file access docs if provided
    if file_access_docs:
        lines.append(file_access_docs)
        lines.append("")

    lines.extend(
        [
            "RETURNING DATA",
            "==============",
            "Set `result = <value>` to return data. Large lists (>500 items) are auto-truncated.",
            "",
            "COMMON PATTERNS",
            "===============",
            "# Get and filter elements",
            'result = await archicad.tapir("GetElementsByType", {"elementType": "Wall"})',
            'walls = result.get("elements", [])',
            "",
            "# Export to file",
            "import json",
            'with open("C:/output.json", "w") as f:',
            "    json.dump(walls, f)",
            "",
            "# Create elements",
            'columns_data = [{"coordinates": {"x": i*5000, "y": 0, "z": 0}} for i in range(5)]',
            'result = await archicad.tapir("CreateColumns", {"columnsData": columns_data})',
            'created = result.get("elements", [])',
            "",
            "# Read property values (LOCALE WARNING: numeric values may use comma decimals)",
            '# Archicad returns locale-formatted strings: "1,00" not 1.0',
            '# Always normalize before arithmetic: float(value.replace(",", "."))',
            "",
            "Args:",
            "    port: Archicad instance port (from list_instances)",
            "    script: Python code to execute",
            "    timeout_seconds: Optional timeout in seconds (default: no timeout)",
        ]
    )

    return "\n".join(lines)
