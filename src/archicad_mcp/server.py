"""FastMCP server for Archicad automation."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypeAlias

import aiohttp
from mcp.server.fastmcp import Context, FastMCP

from archicad_mcp.config import format_file_access_docs, load_config
from archicad_mcp.core import ArchicadError, ConnectionManager, PropertyCache
from archicad_mcp.core.properties import (
    _format_property,
    exact_lookup,
    filter_properties,
    find_similar_groups,
    get_groups_summary,
    get_type_summary,
    search_properties,
)
from archicad_mcp.models import ArchicadInstance, ScriptResult
from archicad_mcp.schemas import SchemaCache, generate_execute_script_docs
from archicad_mcp.scripting import ScriptExecutor

# Context is generic over (Session, LifespanContext, Request) - use Any for all
Ctx: TypeAlias = Context[Any, Any, Any]

logger = logging.getLogger(__name__)

# =============================================================================
# Security Configuration (loaded at module level)
# =============================================================================
security_config = load_config()


# =============================================================================
# Lifespan - manages shared resources
# =============================================================================
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Initialize and cleanup shared resources."""
    # Create shared HTTP session with connection pooling
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300),
        connector=aiohttp.TCPConnector(
            keepalive_timeout=60,
            limit=20,  # Max concurrent connections
        ),
    )

    # Initialize managers
    manager = ConnectionManager(session)
    executor = ScriptExecutor()
    schemas = SchemaCache()
    property_cache = PropertyCache()

    # Startup: scan for Archicad instances
    await manager.scan_and_connect()

    # Load schemas: try live Tapir first, fall back to embedded cache
    schemas.load_embedded()  # Load cached as baseline
    for conn in manager.connections.values():
        if conn._tapir_available is True and await schemas.load_from_tapir(conn):
            break  # Successfully loaded live schemas

    # Generate dynamic execute_script docstring from loaded schemas
    file_access_docs = format_file_access_docs(security_config)
    execute_script_description = generate_execute_script_docs(schemas, file_access_docs)

    # Dynamically register execute_script with generated description
    @server.tool(description=execute_script_description)  # type: ignore[untyped-decorator]
    async def execute_script(
        ctx: Ctx,
        port: int,
        script: str,
        timeout_seconds: int | None = None,
    ) -> ScriptResult:
        mgr: ConnectionManager = ctx.request_context.lifespan_context["manager"]
        exe: ScriptExecutor = ctx.request_context.lifespan_context["executor"]
        cfg = ctx.request_context.lifespan_context["security_config"]
        conn = mgr.get(port)
        return await exe.run(script, conn, timeout_seconds, cfg)

    yield {
        "session": session,
        "manager": manager,
        "executor": executor,
        "schemas": schemas,
        "security_config": security_config,
        "property_cache": property_cache,
    }

    # Shutdown
    await session.close()


mcp = FastMCP(
    "Archicad MCP",
    instructions="Powerful Archicad automation via JSON API and Python scripting",
    lifespan=lifespan,
)


# =============================================================================
# Tool 1: List Archicad Instances
# =============================================================================
@mcp.tool()  # type: ignore[untyped-decorator]
async def list_instances(ctx: Ctx) -> list[ArchicadInstance]:
    """
    Find all running Archicad instances.

    Scans ports 19723-19744 for Archicad's JSON API.
    Returns instance info including port, project name, version.
    Use the 'port' value in other tools to target a specific instance.
    """
    manager: ConnectionManager = ctx.request_context.lifespan_context["manager"]
    schemas: SchemaCache = ctx.request_context.lifespan_context["schemas"]
    await manager.refresh()

    # Self-heal: if no schemas loaded and Tapir is available, regenerate and save
    if not schemas.commands:
        for conn in manager.connections.values():
            if conn._tapir_available is True and await schemas.load_from_tapir(conn):
                logger.info(
                    "Schemas regenerated from Tapir and saved to disk. "
                    "Restart the MCP server to load full command documentation."
                )
                break

    return manager.get_instances()


# =============================================================================
# Tool 3: Get Command Documentation
# =============================================================================
@mcp.tool()  # type: ignore[untyped-decorator]
async def get_docs(
    ctx: Ctx,
    search: str | None = None,
    command: str | None = None,
    commands: list[str] | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """
    Get documentation for Archicad commands.

    USAGE:
      get_docs()                              # Overview: categories with counts
      get_docs(category="Element Commands")   # Browse: commands in a category
      get_docs(search="...")                  # Find commands by keyword
      get_docs(command="CommandName")         # Full schema for one command
      get_docs(commands=["A", "B"])           # Full schemas for multiple

    DISCOVERY WORKFLOW:
      1. get_docs() -> see categories
      2. get_docs(category="...") -> see command names
      3. get_docs(command="...") -> full schema

    SEARCH FEATURES:
      - Searches across: names, descriptions, parameters, examples, notes
      - Element types: "wall" -> suggests GetElementsByType(elementType="Wall")
      - Partial match: "prop" finds property commands
      - Typo tolerant: "proprty" -> property commands
      - Multi-word: "create slab" finds CreateSlabs

    Args:
        search: Search query (e.g., "wall", "create slab", "property")
        command: Exact command name for full schema
        commands: List of command names for full schemas
        category: Category name to list all commands in it

    Examples:
        get_docs()                              # Overview
        get_docs(category="Element Commands")   # Browse category
        get_docs(search="wall")                 # Commands for walls
        get_docs(search="create")               # Creation commands
        get_docs(command="CreateSlabs")          # Full schema for CreateSlabs
    """
    schemas: SchemaCache = ctx.request_context.lifespan_context["schemas"]

    if command:
        result = schemas.get_command(command)
        if result is None:
            similar = schemas.find_similar_commands(command)
            return {
                "query": {"command": command},
                "error": f"Command '{command}' not found",
                "suggestion": (
                    f"Similar: {similar}" if similar else "Use get_docs() to browse commands"
                ),
            }
        return result
    elif commands:
        return schemas.get_commands(commands)
    elif category:
        return schemas.get_category(category)
    elif search:
        return schemas.search(search)
    else:
        return schemas.get_summary()


# =============================================================================
# Tool 4: Get Properties
# =============================================================================
@mcp.tool()  # type: ignore[untyped-decorator]
async def get_properties(
    ctx: Ctx,
    port: int,
    search: str | None = None,
    group: str | None = None,
    property_type: str | None = None,
    measure_type: str | None = None,
    property: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """
    Search and discover Archicad element properties.

    Properties are attributes like area, length, volume that you query on elements.
    Use this to find property GUIDs needed for GetPropertyValuesOfElements.

    WHEN TO USE:
      - "Get wall areas" → get_properties(search="area", group="Wall")
      - "What properties can I query on zones?" → get_properties(group="Zone")
      - "Find custom/user-defined properties" → get_properties(property_type="Custom")

    MODES:
      get_properties(port)                         # Overview of all groups
      get_properties(port, search="length")        # Search by keyword
      get_properties(port, group="Wall")           # All properties for element type
      get_properties(port, property="Length of Reference Line")  # Exact lookup

    Args:
        port: Archicad instance port (from list_instances)
        search: Search property names (e.g., "area", "length", "surface")
        group: Filter by group/element type (e.g., "Wall", "Zone", "Geometry")
        property_type: Filter by type: "StaticBuiltIn", "DynamicBuiltIn", "Custom"
        measure_type: Filter by unit: "Length", "Area", "Volume", "Angle"
        property: Exact property name lookup (returns single match with GUID)
        limit: Max results (default 50, max 200)

    Returns:
        Properties with GUIDs ready for GetPropertyValuesOfElements.

    NOTE: For command documentation (API schemas), use get_docs instead.
    """
    manager: ConnectionManager = ctx.request_context.lifespan_context["manager"]
    cache: PropertyCache = ctx.request_context.lifespan_context["property_cache"]
    conn = manager.get(port)

    # Clamp limit
    limit = max(1, min(limit, 200))

    # Fetch properties (cached)
    try:
        all_props = await cache.get_properties(conn)
    except ArchicadError as e:
        return {"error": str(e), "suggestion": "Ensure Tapir add-on is installed"}

    # Mode 1: Exact lookup by property name
    if property:
        match = exact_lookup(all_props, property)
        if match:
            formatted = _format_property(match)
            return {
                "query": {"property": property},
                "found": True,
                "property": formatted,
                "usage": {
                    "description": "Use GetPropertyValuesOfElements to query this property",
                    "example": (
                        f"await archicad.tapir('GetPropertyValuesOfElements', "
                        f"{{'elements': elements, 'properties': [{{'propertyId': {{'guid': '{formatted['guid']}'}}}}]}})"
                    ),
                },
            }
        # Not found - suggest similar
        similar = find_similar_groups(all_props, property)
        return {
            "query": {"property": property},
            "found": False,
            "suggestion": f"Property not found. Try search: get_properties(port, search='{property.split()[0]}')"
            + (f" Similar groups: {similar}" if similar else ""),
        }

    # Mode 2: Overview (no filters)
    has_filter = search or group or property_type or measure_type
    if not has_filter:
        return {
            "total_properties": len(all_props),
            "groups": get_groups_summary(all_props),
            "property_types": get_type_summary(all_props),
            "tip": "Use search, group, or measure_type to filter properties",
        }

    # Mode 3: Search/filter
    filtered = filter_properties(
        all_props,
        group=group,
        property_type=property_type,
        measure_type=measure_type,
    )

    # Check if group filter matched nothing
    if group and not filtered:
        similar = find_similar_groups(all_props, group)
        suggestion = (
            f"Did you mean: {similar}?" if similar else "Use get_properties() to see all groups."
        )
        return {
            "query": {"group": group},
            "total": 0,
            "properties": [],
            "suggestion": suggestion,
        }

    # Apply search if provided
    if search:
        scored = search_properties(filtered, search)
        results = [p for p, _ in scored[:limit]]
        total = len(scored)
    else:
        results = filtered[:limit]
        total = len(filtered)
    formatted_results = [_format_property(p) for p in results]

    response: dict[str, Any] = {
        "query": {
            k: v
            for k, v in [
                ("search", search),
                ("group", group),
                ("property_type", property_type),
                ("measure_type", measure_type),
                ("limit", limit),
            ]
            if v is not None
        },
        "total": total,
        "showing": len(formatted_results),
        "properties": formatted_results,
    }

    # Add usage hint
    if formatted_results:
        first_guid = formatted_results[0]["guid"]
        response["usage"] = {
            "description": "Use GetPropertyValuesOfElements to query these properties",
            "example": (
                f"await archicad.tapir('GetPropertyValuesOfElements', "
                f"{{'elements': [...], 'properties': [{{'propertyId': {{'guid': '{first_guid}'}}}}]}})"
            ),
        }

    # Add tip if truncated
    if total > len(formatted_results):
        response["tip"] = (
            f"{total} results truncated to {len(formatted_results)}. Add filters to narrow."
        )

    return response


# =============================================================================
# Entry Point
# =============================================================================
def main() -> None:
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
