"""MCP server for Archicad automation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, TypeAlias, TypedDict, cast

import aiohttp
from mcp.server.mcpserver import Context, MCPServer
from pydantic import StrictFloat, StrictInt

from archicad_mcp.config import DEFAULT_SCRIPT_TIMEOUT_SECONDS, validate_script_timeout
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
from archicad_mcp.schemas.cache_store import read_cached_snapshot
from archicad_mcp.schemas.registry import (
    CapabilityView,
    ProviderSnapshot,
    ViewStatus,
    load_provider_snapshot,
)
from archicad_mcp.schemas.semver import compare_semver
from archicad_mcp.schemas.updater import (
    PackagedTapir,
    auto_update_enabled,
    load_packaged_tapir,
    offline_mode_enabled,
    run_update_check,
)
from archicad_mcp.scripting import ScriptExecutor

logger = logging.getLogger(__name__)


class ServerState(TypedDict):
    """Resources shared by all tools for one server lifetime."""

    session: aiohttp.ClientSession
    manager: ConnectionManager
    executor: ScriptExecutor
    schemas: CapabilityView
    coordinator: SchemaCoordinator
    property_cache: PropertyCache


Ctx: TypeAlias = Context[ServerState, Any]


_NATIVE_PROVIDER_VERSION = "2.0.0"
_PACKAGED_TARGET_IDENTITY = "schema-bundle:native:2.0.0"

EXECUTE_SCRIPT_DESCRIPTION = (
    "Run the body of an async Python function against the selected Archicad port. archicad and port "
    "are injected, and top-level await works. Use archicad.command for native commands and "
    "archicad.tapir for Tapir commands; use get_docs to inspect their schemas. Assign a "
    "JSON-compatible value to result; stdout and stderr are captured in ScriptResult. Execution uses "
    "a local_user worker but is not hostile-code isolation: scripts can make destructive model or "
    "system changes. The default timeout is 300 seconds; null disables it. Timeout or cancellation "
    "stops the worker but cannot undo completed effects or guarantee that spawned processes stop."
)


def _load_native_snapshot() -> ProviderSnapshot:
    schema_package = files("archicad_mcp.schemas")
    return load_provider_snapshot(
        schema_package.joinpath("builtin.json").read_bytes(),
        provider="native",
        provider_version=_NATIVE_PROVIDER_VERSION,
        distribution="packaged builtin.json",
        provenance=("package:archicad_mcp.schemas/builtin.json",),
    )


def select_active_tapir_snapshot(packaged: PackagedTapir) -> tuple[ProviderSnapshot, str | None]:
    """Choose the starting Tapir snapshot: newest valid cache, else packaged.

    A cached snapshot wins only when its strict SemVer version is newer than
    the packaged floor; equal ties always select the packaged snapshot. Cache
    corruption is returned as its stable error code so callers can surface it
    without breaking packaged startup.
    """

    result = read_cached_snapshot()
    if (
        result.cached is not None
        and compare_semver(result.cached.version, packaged.identity.version) > 0
    ):
        return result.cached.snapshot, None
    return packaged.snapshot, result.error_code


def _has_observed_version(version: str) -> bool:
    return version.strip().lower() not in {"", "none", "unknown"}


def _build_capability_view(
    manager: ConnectionManager,
    native: ProviderSnapshot,
    tapir: ProviderSnapshot,
) -> CapabilityView:
    connections = [connection for _, connection in sorted(manager.connections.items())]
    tapir_connection = next(
        (connection for connection in connections if connection._tapir_available is True),
        None,
    )
    if tapir_connection is not None:
        target_identity = (
            tapir_connection.version
            if _has_observed_version(tapir_connection.version)
            else next(
                (
                    connection.version
                    for connection in connections
                    if _has_observed_version(connection.version)
                ),
                _PACKAGED_TARGET_IDENTITY,
            )
        )
        return CapabilityView(
            native=native,
            tapir=tapir,
            target_identity=target_identity,
            status=ViewStatus.TAPIR_AVAILABLE,
        )

    unknown_connection = next(
        (connection for connection in connections if connection._tapir_available is None),
        None,
    )
    if unknown_connection is not None or not connections:
        target_identity = (
            unknown_connection.version
            if unknown_connection is not None and _has_observed_version(unknown_connection.version)
            else next(
                (
                    connection.version
                    for connection in connections
                    if _has_observed_version(connection.version)
                ),
                _PACKAGED_TARGET_IDENTITY,
            )
        )
        return CapabilityView(
            native=native,
            tapir=tapir,
            target_identity=target_identity,
            status=ViewStatus.COMPATIBILITY_UNKNOWN,
        )

    target_identity = next(
        (
            connection.version
            for connection in connections
            if _has_observed_version(connection.version)
        ),
        _PACKAGED_TARGET_IDENTITY,
    )
    return CapabilityView(
        native=native,
        tapir=None,
        target_identity=target_identity,
        status=ViewStatus.TAPIR_UNAVAILABLE,
    )


class SchemaCoordinator:
    """Coordinate live discovery with the retained direct-update Tapir snapshot.

    One async lock serializes projection so refresh and update acceptance can
    never regress each other. At most one automatic update task exists per
    server lifespan; it runs the accepted foundation outside the projection
    lock and reprojects its accepted snapshot against the latest manager
    observations under the lock. Close cancels and drains that task, so no
    late cache or view mutation survives shutdown.
    """

    def __init__(
        self,
        manager: ConnectionManager,
        session: aiohttp.ClientSession,
        packaged: PackagedTapir,
        native_snapshot: ProviderSnapshot,
        tapir_snapshot: ProviderSnapshot,
        view: CapabilityView,
        publish: Callable[[CapabilityView], None],
    ) -> None:
        self.manager = manager
        self.session = session
        self._packaged = packaged
        self._native_snapshot = native_snapshot
        self._tapir_snapshot = tapir_snapshot
        self._view = view
        self._publish = publish
        self._lock = asyncio.Lock()
        self._update_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False

    @property
    def view(self) -> CapabilityView:
        """Return the current complete projected view."""
        return self._view

    @property
    def native_snapshot(self) -> ProviderSnapshot:
        """Return the retained native provider snapshot."""
        return self._native_snapshot

    @property
    def tapir_snapshot(self) -> ProviderSnapshot:
        """Return the retained Tapir provider snapshot, including when hidden."""
        return self._tapir_snapshot

    @property
    def update_task(self) -> asyncio.Task[None] | None:
        """Return the scheduled lifespan update task, if any."""
        return self._update_task

    async def refresh(self) -> list[ArchicadInstance]:
        """Refresh connections and publish a complete view before returning instances."""
        async with self._lock:
            await self.manager.refresh()
            projected = _build_capability_view(
                self.manager,
                self._native_snapshot,
                self._tapir_snapshot,
            )
            if projected.revision != self._view.revision:
                self._view = projected
                self._publish(projected)
            return self.manager.get_instances()

    def schedule_startup_update(self) -> None:
        """Schedule the single nonblocking automatic update check for this lifespan.

        Offline mode and disabled auto-update skip scheduling without touching
        the cache; TTL, lease, and all network work stay inside the foundation.
        Discovery refresh never calls this.
        """

        if self._closing or self._closed or self._update_task is not None:
            return
        try:
            offline = offline_mode_enabled()
            auto_enabled = auto_update_enabled()
        except ValueError as exc:
            logger.warning("Schema update environment invalid; startup check skipped (%s)", exc)
            return
        if offline or not auto_enabled:
            return
        self._update_task = asyncio.create_task(
            self._run_startup_update(),
            name="archicad-mcp-schema-update",
        )

    async def _run_startup_update(self) -> None:
        """Run one bounded check, then reproject the accepted snapshot once current."""

        accepted: list[ProviderSnapshot] = []
        try:
            await run_update_check(self._packaged, self.session, on_accepted=accepted.append)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Automatic schema update failed (%s)", type(exc).__name__)
            return
        if not accepted:
            return
        async with self._lock:
            if self._closing or self._closed:
                return
            self._tapir_snapshot = accepted[-1]
            projected = _build_capability_view(
                self.manager,
                self._native_snapshot,
                self._tapir_snapshot,
            )
            self._view = projected
            self._publish(projected)

    async def close(self) -> None:
        """Cancel and drain the one per-lifespan update task exactly once.

        Every concurrent caller awaits the same shared drain operation, so no
        caller can return while product-owned update work could still mutate
        the cache or view afterwards. A caller cancelled mid-close keeps
        absorbing further cancellation until that shared drain finishes, then
        propagates the cancellation.
        """

        close_op = self._close_task
        if close_op is None:
            self._closing = True
            close_op = self._close_task = asyncio.create_task(
                self._finish_close(),
                name="archicad-mcp-schema-close",
            )
        first_cancellation: asyncio.CancelledError | None = None
        while not close_op.done():
            try:
                await asyncio.shield(close_op)
            except asyncio.CancelledError as exc:
                if first_cancellation is None:
                    first_cancellation = exc
        if first_cancellation is not None:
            raise first_cancellation

    async def _finish_close(self) -> None:
        """Run the single shared terminal drain for this coordinator."""

        task = self._update_task
        self._update_task = None
        try:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "Schema update task stopped during close (%s)",
                        type(exc).__name__,
                    )
        finally:
            self._closed = True


def _browse_all(view: CapabilityView) -> list[dict[str, Any]]:
    total = view.native.command_count + (view.tapir.command_count if view.tapir is not None else 0)
    if total == 0:
        return []
    return cast(list[dict[str, Any]], view.browse(total)["capabilities"])


def _resolve_capability_id(view: CapabilityView, requested: str) -> str | None:
    if requested.startswith(("native:", "tapir:")):
        return requested if view.document_sha256(requested) is not None else None

    for provider in ("native", "tapir"):
        capability_id = f"{provider}:{requested}"
        if view.document_sha256(capability_id) is not None:
            return capability_id
    return None


def _exhaustive_browse_route() -> dict[str, str]:
    return {
        "overview": "get_docs()",
        "category": "get_docs(category='<category>')",
        "note": "Search is intent-ranked; browse every category from the overview for exhaustive discovery.",
    }


def _not_found(view: CapabilityView, requested: str) -> dict[str, Any]:
    search_result = view.search(requested)
    ranked = cast(list[dict[str, Any]], search_result["results"])
    suggestion: dict[str, Any] = {
        "search": requested,
        "exhaustive_browse": _exhaustive_browse_route(),
    }
    if ranked:
        suggestion["capability_id"] = ranked[0]["id"]
        suggestion["name"] = ranked[0]["name"]
    return {
        "query": {"command": requested},
        "found": False,
        "error": "capability_not_found",
        "missing": [requested],
        "suggestion": suggestion,
    }


# =============================================================================
# Lifespan - manages shared resources
# =============================================================================
@asynccontextmanager
async def lifespan(_: MCPServer[ServerState]) -> AsyncIterator[ServerState]:
    """Initialize and clean up resources shared by registered tools."""
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300),
        connector=aiohttp.TCPConnector(keepalive_timeout=60, limit=20),
    ) as session:
        manager = ConnectionManager(session)
        executor = ScriptExecutor()
        property_cache = PropertyCache()

        await manager.scan_and_connect()
        packaged = load_packaged_tapir()
        native_snapshot = _load_native_snapshot()
        tapir_snapshot, cache_error = select_active_tapir_snapshot(packaged)
        if cache_error is not None:
            logger.warning(
                "User cache schema ignored; serving the packaged baseline (%s)", cache_error
            )
        schemas = _build_capability_view(manager, native_snapshot, tapir_snapshot)
        state: ServerState
        coordinator = SchemaCoordinator(
            manager,
            session,
            packaged,
            native_snapshot,
            tapir_snapshot,
            schemas,
            lambda updated: state.__setitem__("schemas", updated),
        )
        state = {
            "session": session,
            "manager": manager,
            "executor": executor,
            "schemas": schemas,
            "coordinator": coordinator,
            "property_cache": property_cache,
        }

        # The only automatic check of this server lifespan; startup never waits
        # for it and later discovery refresh never schedules another one.
        coordinator.schedule_startup_update()
        try:
            yield state
        finally:
            await coordinator.close()


# =============================================================================
# Tool 1: List Archicad Instances
# =============================================================================
async def list_instances(ctx: Ctx) -> list[ArchicadInstance]:
    """
    Find all running Archicad instances.

    Scans ports 19723-19743 for Archicad's JSON API.
    Returns instance info including port, project name, version.
    Use the 'port' value in other tools to target a specific instance.
    """
    coordinator: SchemaCoordinator = ctx.request_context.lifespan_context["coordinator"]
    return await coordinator.refresh()


# =============================================================================
# Tool 3: Get Command Documentation
# =============================================================================
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

    LIVE VIEW FILTERING:
      - No live instance: compatibility_unknown; packaged native and Tapir docs are available
      - Tapir observed: tapir_available; native and Tapir docs are available
      - Reachable native-only instances: tapir_unavailable; Tapir capabilities are omitted
      - The live process filters the validated packaged/cached registry; it does not supply schemas

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
    view: CapabilityView = ctx.request_context.lifespan_context["schemas"]

    if command:
        capability_id = _resolve_capability_id(view, command)
        if capability_id is None:
            return _not_found(view, command)
        document = view.get(capability_id)
        if document is None:
            return _not_found(view, command)
        return document

    if commands:
        resolved: list[tuple[str, str]] = []
        missing: list[str] = []
        for requested in commands:
            capability_id = _resolve_capability_id(view, requested)
            if capability_id is None:
                missing.append(requested)
            else:
                resolved.append((requested, capability_id))

        unique_ids = list(dict.fromkeys(capability_id for _, capability_id in resolved))
        batch = view.get_many(unique_ids)
        documents_by_id = {
            str(document["id"]): document
            for document in cast(list[dict[str, Any]], batch["documents"])
        }
        documents = [documents_by_id[capability_id] for _, capability_id in resolved]
        return {
            "view_revision": batch["view_revision"],
            "documents": documents,
            "missing": missing,
        }

    if category:
        matches = [record for record in _browse_all(view) if record["category"] == category]
        response: dict[str, Any] = {
            "query": {"category": category},
            "category": category,
            "total": len(matches),
            "capability_names": [record["name"] for record in matches],
            "capabilities": [
                {
                    "id": record["id"],
                    "provider": record["provider"],
                    "name": record["name"],
                    "description": record["description"],
                }
                for record in matches
            ],
        }
        if not matches:
            response["suggestion"] = "Use get_docs() to browse all categories."
        return response

    if search:
        result = view.search(search)
        result["exhaustive_browse"] = _exhaustive_browse_route()
        return result

    summary = view.summary()
    provider_counts = cast(dict[str, int], summary["provider_counts"])
    categories = cast(list[dict[str, Any]], summary["categories"])
    return {
        **summary,
        "total_commands": summary["total"],
        "builtin_commands": provider_counts["native"],
        "tapir_commands": provider_counts["tapir"],
        "category_counts": {
            str(category_summary["name"]): int(category_summary["count"])
            for category_summary in categories
        },
        "tip": "Use get_docs(category='...') to browse every capability in a category.",
    }


# =============================================================================
# Tool 4: Get Properties
# =============================================================================
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


async def execute_script(
    ctx: Ctx,
    port: int,
    script: str,
    timeout_seconds: StrictInt | StrictFloat | None = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
) -> ScriptResult:
    """Execute a Python script against a running Archicad instance."""
    state = ctx.request_context.lifespan_context
    state["manager"].get(port)
    timeout = validate_script_timeout(timeout_seconds)
    return await state["executor"].run(script, port, timeout)


def create_server() -> MCPServer[ServerState]:
    """Build the native MCP SDK v2 server with static tool metadata."""
    server = MCPServer(
        "Archicad MCP",
        instructions="Archicad automation through JSON API commands and Python scripts",
        lifespan=lifespan,
    )
    server.add_tool(list_instances)
    server.add_tool(get_docs)
    server.add_tool(get_properties)
    server.add_tool(execute_script, description=EXECUTE_SCRIPT_DESCRIPTION)
    return server


mcp = create_server()


# =============================================================================
# Entry Point
# =============================================================================
def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
