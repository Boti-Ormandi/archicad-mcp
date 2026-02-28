# Dynamic Context Generation Plan

**Status: IMPLEMENTED**

Files:
- `src/archicad_mcp/schemas/docgen.py` - Compact schema + docstring generation
- `src/archicad_mcp/schemas/cache.py` - Schema caching with live/embedded fallback
- `src/archicad_mcp/server.py` - Dynamic tool registration in lifespan
- `src/archicad_mcp/scripting/api.py` - Simplified to `tapir()` + `command()` only

## Problem Statement

Current state:
- `execute_script` docstring documents 12 wrapper methods with **wrong parameter names**
- Example code in docstring uses **incorrect schemas** (e.g., `{"position": {...}}` instead of `{"coordinates": {...}}`)
- `create_walls()` references a command that **doesn't exist** in Tapir
- AI must call `get_docs()` 1-2 times just to learn correct API usage
- Wrapper methods add almost no value over raw `tapir()`/`command()` calls

## Solution Overview

1. **Remove buggy wrapper methods** - Keep only `tapir()` and `command()` raw access
2. **Generate docstring dynamically** at startup from live Tapir schemas
3. **Include compact schemas** for Tier 1 + Tier 2 commands (~46 commands, ~1500 tokens)
4. **Make clear** that additional commands available via `get_docs()`

## Command Tiers

### Tier 1: Essential (22 commands) - Always in context
Daily use commands for typical architect workflows.

**Element Queries:**
- GetElementsByType, GetAllElements, GetSelectedElements
- GetDetailsOfElements, FilterElements

**Element Modification:**
- SetDetailsOfElements, ChangeSelectionOfElements
- HighlightElements, DeleteElements, MoveElements

**Element Creation:**
- CreateColumns, CreateSlabs, CreateZones, CreateObjects
- CreateMeshes, CreatePolylines, CreateLabels

**Properties:**
- GetPropertyValuesOfElements, SetPropertyValuesOfElements

**Project:**
- GetProjectInfo, GetStories, GetAttributesByType

### Tier 2: Useful (24 commands) - Always in context
Weekly use commands for more advanced workflows.

**Spatial & Relationships:**
- Get3DBoundingBoxes, GetConnectedElements, GetZoneBoundaries, GetCollisions
- GetSubelementsOfHierarchicalElements

**Classifications:**
- GetClassificationsOfElements, SetClassificationsOfElements

**GDL Parameters:**
- GetGDLParametersOfElements, SetGDLParametersOfElements

**Attributes:**
- CreateLayers, CreateBuildingMaterials, CreateSurfaces, CreateComposites
- GetLayerCombinations, CreateLayerCombinations
- GetBuildingMaterialPhysicalProperties

**Properties (Advanced):**
- GetAllProperties, CreatePropertyGroups, CreatePropertyDefinitions

**Project (Advanced):**
- SetStories, GetHotlinks, GetGeoLocation, OpenProject, IFCFileOperation

### Tier 3: Specialized (19 commands) - Via get_docs() only
Occasional use commands for specific workflows.

- Favorites: GetFavoritesByType, ApplyFavoritesToElementDefaults, CreateFavoritesFromElements
- Issues/BCF: GetIssues, CreateIssue, AttachElementsToIssue, etc.
- Teamwork: TeamworkSend, TeamworkReceive, ReserveElements, ReleaseElements
- Publishing: PublishPublisherSet, UpdateDrawings, GetViewSettings, SetViewSettings
- Library: GetLibraries, ReloadLibraries
- Images: GetElementPreviewImage, GetRoomImage, GetFavoritePreviewImage

## Implementation Tasks

### Task 1: Remove wrapper methods from api.py
**File:** `src/archicad_mcp/scripting/api.py`

Remove these methods:
- `get_elements()`
- `get_selected()`
- `get_properties()`
- `set_properties()`
- `highlight()`
- `create_columns()`
- `create_walls()`
- `create_slabs()`
- `create_zones()`
- `create_objects()`
- `get_project_info()`
- `get_stories()`

Keep only:
- `command(name, params)` - Official API access
- `tapir(name, params)` - Tapir command access

### Task 2: Create compact schema generator
**New file:** `src/archicad_mcp/schemas/docgen.py`

Functions:
- `generate_compact_schema(cmd_name, cmd_data) -> str` - Convert full schema to compact format
- `generate_docstring(schemas: SchemaCache, tier1: list, tier2: list) -> str` - Generate full docstring

Compact format example:
```
CreateColumns(columnsData: [{coordinates: {x, y, z}}])
  -> {elements: [{elementId: {guid}}]}
  Creates Column elements based on the given parameters.
```

### Task 3: Modify SchemaCache to expose schema generation
**File:** `src/archicad_mcp/schemas/cache.py`

Add method:
- `generate_execute_script_docs(tier1_commands, tier2_commands) -> str`

This generates the dynamic portion of the docstring from live schemas.

### Task 4: Update server.py to use dynamic docstring
**File:** `src/archicad_mcp/server.py`

Changes:
- Move `EXECUTE_SCRIPT_DESCRIPTION` generation to after schema loading
- Include dynamically generated command docs
- Add clear note about `get_docs()` for additional commands

### Task 5: Update execute_script docstring structure
**File:** `src/archicad_mcp/server.py`

New docstring structure:
```
SCRIPT NAMESPACE
================

Raw API Access (use these):
  await archicad.tapir(name, params)    # Tapir commands
  await archicad.command(name, params)  # Official API (prefix with "API.")

COMMON TAPIR COMMANDS
=====================
[Dynamically generated from live schemas - Tier 1 + Tier 2]

CreateColumns(columnsData: [{coordinates: {x, y, z}}])
  -> {elements: [{elementId: {guid}}]}
  Creates Column elements.

GetElementsByType(elementType: str)
  -> {elements: [{elementId: {guid}}]}
  Get all elements of specified type.

... [~46 commands total]

MORE COMMANDS
=============
Use get_docs(search="...") to find additional commands.
Use get_docs(command="CommandName") for full schema.

Categories available: Element Commands, Property Commands,
Attribute Commands, Project Commands, Issue Management,
Teamwork, Navigator, Favorites, Library, Revision Management

ELEMENT TYPES
=============
Wall, Column, Beam, Slab, Roof, Shell, Morph, Door, Window,
Skylight, Object, Zone, Stair, Railing, CurtainWall, Mesh

AVAILABLE MODULES
=================
json, csv, math, pathlib, re, datetime, itertools, functools, collections, openpyxl

[File access docs]

RETURNING DATA
==============
Set `result = <value>` to return data.
```

### Task 6: Handle missing schemas gracefully
**File:** `src/archicad_mcp/schemas/docgen.py`

If live schema loading fails:
- Fall back to cached schemas
- If command not in cache, skip it (don't crash)
- Log warning for missing commands

### Task 7: Add common_schemas resolution for $ref
**File:** `src/archicad_mcp/schemas/docgen.py`

When generating compact format, resolve common `$ref` patterns:
- `#/Elements` -> `[{elementId: {guid}}]`
- `#/ElementId` -> `{guid}`
- `#/Coordinate2D` -> `{x, y}`
- `#/Coordinate3D` -> `{x, y, z}`

Use `common_schemas` from cached `tapir.json` (already saved by `_save_tapir_cache()`).

### Task 8: Update tests to use raw API
**File:** `tests/unit/test_executor.py`

Replace wrapper method calls:
```python
# Before
elements = await archicad.get_elements("Wall")

# After
result = await archicad.tapir("GetElementsByType", {"elementType": "Wall"})
elements = result.get("elements", [])
```

### Task 9: Use dynamic tool registration in lifespan
**File:** `src/archicad_mcp/server.py`

Changes:
- Remove `@mcp.tool` decorator from `execute_script`
- Remove static `EXECUTE_SCRIPT_DESCRIPTION`
- In lifespan, after schema loading:
  ```python
  description = generate_execute_script_docs(schemas)
  server.add_tool(execute_script, description=description)
  ```

This ensures the tool description is ALWAYS generated from the best available schemas (live or cached).

## Task Order (Dependencies)

```
Task 2 (docgen.py) + Task 7 ($ref resolution)
    ↓
Task 3 (SchemaCache method)
    ↓
Task 5 (docstring structure)
    ↓
Task 4 + Task 9 (server.py - dynamic registration)
    ↓
Task 1 (remove wrappers from api.py)
    ↓
Task 8 (update tests)
    ↓
Task 6 (graceful fallback - can be done anytime)
```

## Token Budget

| Component | Tokens |
|-----------|--------|
| Header + raw access docs | ~100 |
| Tier 1 commands (22 × 35) | ~770 |
| Tier 2 commands (24 × 35) | ~840 |
| Footer (more commands note, element types, modules) | ~150 |
| File access docs | ~100 |
| **Total** | **~1960** |

Current docstring: ~500 tokens
New docstring: ~2000 tokens (+1500 tokens, acceptable)

## Testing

1. **Unit test:** Compact schema generation produces valid format
2. **Unit test:** Docstring generation includes all Tier 1+2 commands
3. **Integration test:** With live Archicad, schemas load and docstring generates
4. **Manual test:** AI can use generated docs to write correct scripts

## Rollback Plan

If dynamic generation causes issues:
- Keep current static `tapir.json` as fallback
- `generate_execute_script_docs()` returns static fallback on error
- Log errors but don't crash

## Files Changed

| File | Change |
|------|--------|
| `src/archicad_mcp/scripting/api.py` | Remove wrapper methods, keep only `tapir()` + `command()` |
| `src/archicad_mcp/schemas/docgen.py` | New file - compact schema + docstring generation |
| `src/archicad_mcp/schemas/cache.py` | Add `generate_docs()` method, sync cache loading |
| `src/archicad_mcp/schemas/__init__.py` | Export new functions |
| `src/archicad_mcp/server.py` | Use generated docstring, load cache at module level |
| `tests/unit/test_executor.py` | Update to use `tapir()` instead of wrappers |

## Critical Implementation Notes

### Dynamic Tool Registration (Preferred Solution)
FastMCP supports `mcp.add_tool(fn, description=...)` for runtime registration.

**Solution:**
- Do NOT use `@mcp.tool` decorator for `execute_script`
- In lifespan, after loading schemas, call `mcp.add_tool(execute_script, description=generated_docs)`
- Schemas are ALWAYS live if Archicad is running at startup

**Benefits:**
- No "one restart behind" - always current
- No sync loading hack needed
- Cleaner architecture

**Fallback:** If Archicad not running at startup, use cached `tapir.json`

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    # ... setup manager, executor ...
    await manager.scan_and_connect()

    # Load schemas - live if possible, cached otherwise
    schemas = SchemaCache()
    schemas.load_embedded()  # Baseline from cache
    for conn in manager.connections.values():
        if conn._tapir_available is True:
            await schemas.load_from_tapir(conn)
            break

    # Generate and register with LIVE description
    description = generate_execute_script_docs(schemas)
    server.add_tool(execute_script_fn, description=description)

    yield {"manager": manager, "schemas": schemas, ...}
```

### Tests use wrapper methods
File `tests/unit/test_executor.py` uses:
- `archicad.get_elements("Wall")`
- etc.

**Solution:** Update tests to use `archicad.tapir("GetElementsByType", {"elementType": "Wall"})`

### Element types currently hardcoded
In `cache.py:311-312`, element types are hardcoded.

**Solution:** Extract from common_schemas `ElementType` enum if available,
or keep hardcoded list (element types are stable).

## Audit: Other Tools

Checked if other tools could benefit from dynamic registration:

| Tool | Dynamic Needed? | Reason |
|------|-----------------|--------|
| `list_instances` | No | Simple description, no schemas needed |
| `execute_command` | No | Generic examples, static works fine |
| `execute_script` | **Yes** | Needs 46 command schemas in context |
| `get_docs` | No | Discovery tool, static description fine |

**Conclusion:** Only `execute_script` needs dynamic registration.

## Open Questions

1. Should we include Official API commands too, or just Tapir?
   - Recommendation: Just Tapir for now, Official API via get_docs()

2. Should compact format show full nested schema or simplified?
   - Recommendation: Simplified with common patterns resolved

3. What if Archicad isn't running at startup?
   - Use cached schemas from last successful run
