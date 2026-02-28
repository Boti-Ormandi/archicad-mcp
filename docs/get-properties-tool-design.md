# get_properties Tool Design

**Status: IMPLEMENTED** (2026-02-04)

Files:
- `src/archicad_mcp/core/properties.py` - PropertyCache + search/filter logic
- `src/archicad_mcp/server.py` - Tool registration
- `tests/unit/test_properties.py` - 29 unit tests

## Purpose

Enable AI to discover and search Archicad properties for use with `GetPropertyValuesOfElements`.

Properties are element attributes like area, length, volume, surface - the data architects most commonly ask about.

---

## Data Overview

From a typical Archicad project:

| Metric | Count |
|--------|-------|
| Total properties | 1802 |
| StaticBuiltIn | 1495 |
| DynamicBuiltIn | 139 |
| Custom (user-defined) | 168 |
| Property groups | 91 |

**Property Types:**
- `StaticBuiltIn` - Standard Archicad properties, stable across projects
- `DynamicBuiltIn` - Calculated/system properties
- `Custom` - User-defined properties (project-specific)

**Measure Types:**
- Length: 443 properties
- Area: 167 properties
- Volume: 48 properties
- Angle: 36 properties
- Default (no unit): 1108 properties

**Value Types:**
- Real: 892
- String: 522
- Integer: 278
- Boolean: 108

---

## AI Scenarios

### Scenario 1: "Get wall lengths"
```
get_properties(search="length", group="Wall")
→ Returns: Length of Reference Line, Length of Wall Inside Face, etc. with GUIDs
```

### Scenario 2: "What area properties exist for slabs?"
```
get_properties(group="Slab", measure_type="Area")
→ Returns: Top Surface Area, Bottom Surface Area, etc.
```

### Scenario 3: "All zone properties"
```
get_properties(group="Zone")
→ Returns: all 46 zone properties
```

### Scenario 4: "Find user-defined properties"
```
get_properties(property_type="Custom")
→ Returns: all 168 custom properties
```

### Scenario 5: "Get exact property by name"
```
get_properties(search="Length of Reference Line")
→ Returns: exact match with GUID
```

### Scenario 6: "Properties related to surface/material"
```
get_properties(search="surface")
→ Returns: Inside Face Surface, Outside Face Surface, Surface Area, etc.
```

### Scenario 7: "What can I query on a beam?"
```
get_properties(group="Beam")
→ Returns: all 77 beam properties
```

### Scenario 8: "Overview of property groups"
```
get_properties()  # no args
→ Returns: summary of all groups with counts
```

### Scenario 9: "Get GUID for specific property I already know"
```
get_properties(property="Length of Reference Line")
→ Returns: exact property with GUID, ready to use
```

---

## Property Groups

Element-based groups (map to element types):
- Wall (75), Column (84), Beam (77), Slab (40), Roof (52), Zone (46)
- Shell (39), Mesh, Stair, Railing, Morph
- Window/Door (44), Opening (28), Skylight
- Curtain Wall + sub-components (Frame, Panel, Junction, Accessory)
- Object/Lamp

Conceptual groups:
- Geometry (117), Positioning (41), Construction (28)
- General Parameters (99), ID and Categories (33)
- Floor Plan and Section (75), Surface and Materials (43)
- Structural Analytical Model (145), Structural Link (34)

Other:
- IFC, MEP, Components, Design Options
- Material groups: THERMAL, MECHANICAL, OPTICAL, CONCRETE, STEEL, WOOD
- Custom expression groups: COST OF STRUCTURE, BEAM PURCHASE LENGTH, etc.

---

## Tool Description (what AI sees)

```python
@mcp.tool()
async def get_properties(
    ctx: Context,
    port: int,
    search: str | None = None,
    group: str | None = None,
    property_type: str | None = None,
    measure_type: str | None = None,
    property: str | None = None,
    limit: int = 50,
) -> dict:
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
```

---

## Argument Alignment with get_docs

| Pattern | get_docs | get_properties |
|---------|----------|----------------|
| Free text search | `search` | `search` |
| Exact lookup | `command` | `property` |
| Multiple exact | `commands` | - (not needed) |
| Filters | - | `group`, `property_type`, `measure_type` |
| Limit | - | `limit` |
| Port | - | `port` (required - live data) |

**Why get_properties has more filters:**
- 1800+ properties vs ~137 commands
- Properties have natural dimensions (group, measure type, built-in vs custom)
- Without filters, results would always be overwhelming

**Why get_properties needs port but get_docs doesn't:**
- Commands are static (from Tapir schema)
- Properties include user-defined ones (project-specific)

---

## Tool Arguments

### `port` (required)
Archicad instance port. Required because properties include user-defined ones that vary per project.

### `search` (optional)
Free-text search across property names.
- Case-insensitive
- Partial matching (same approach as get_docs)
- Examples: "length", "area", "surface", "volume"

### `group` (optional)
Filter by property group name.
- Case-insensitive matching
- Partial matching for convenience ("wall" matches "Wall")
- Examples: "Wall", "Zone", "Geometry", "Beam"

### `property_type` (optional)
Filter by property type.
- Values: `"StaticBuiltIn"`, `"DynamicBuiltIn"`, `"Custom"`
- Most common use: finding user-defined properties

### `measure_type` (optional)
Filter by measurement unit type.
- Values: `"Length"`, `"Area"`, `"Volume"`, `"Angle"`, `"Default"`
- Useful for: "all area properties", "all length properties"

### `property` (optional)
Exact property name lookup.
- Returns single property with GUID
- Case-insensitive exact match
- Analogous to `command` param in get_docs
- Example: `property="Length of Reference Line"`

### `limit` (optional, default 50)
Maximum number of properties to return.
- Default: 50
- Max: 200 (to prevent token overflow)

**Why limit but no offset?**

AI doesn't paginate - it **narrows**. When AI gets "443 results, showing 50", the correct behavior is to add filters (e.g., `group="Wall"`), not request page 2.

Pagination implies stateful iteration which adds complexity without benefit for AI use cases.

---

## Response Format

### When no filters (overview mode):
```json
{
  "total_properties": 1802,
  "groups": [
    {"name": "Wall", "count": 75},
    {"name": "Column", "count": 84},
    ...
  ],
  "property_types": {
    "StaticBuiltIn": 1495,
    "DynamicBuiltIn": 139,
    "Custom": 168
  },
  "tip": "Use search, group, or measure_type to filter properties"
}
```

### When filtered (search mode):
```json
{
  "query": {"search": "length", "group": "Wall", "limit": 50},
  "total": 7,
  "showing": 7,
  "properties": [
    {
      "name": "Length of Reference Line",
      "group": "Wall",
      "guid": "736276CC-0825-4738-A2E8-CDD740C7F635",
      "type": "StaticBuiltIn",
      "value_type": "Real",
      "measure_type": "Length",
      "editable": false
    },
    {
      "name": "Length of the Wall at the Center",
      "group": "Wall",
      "guid": "6651C8DE-502E-47F0-9A96-671A3C5255F2",
      ...
    }
  ],
  "usage": {
    "description": "Use GetPropertyValuesOfElements to query these properties",
    "example": "await archicad.tapir('GetPropertyValuesOfElements', {'elements': [...], 'properties': [{'propertyId': {'guid': '736276CC-...'}}]})"
  }
}
```

### When exact lookup (property param):
```json
{
  "query": {"property": "Length of Reference Line"},
  "found": true,
  "property": {
    "name": "Length of Reference Line",
    "group": "Wall",
    "guid": "736276CC-0825-4738-A2E8-CDD740C7F635",
    "type": "StaticBuiltIn",
    "value_type": "Real",
    "measure_type": "Length",
    "editable": false
  },
  "usage": {
    "description": "Use GetPropertyValuesOfElements to query this property",
    "example": "await archicad.tapir('GetPropertyValuesOfElements', {'elements': walls, 'properties': [{'propertyId': {'guid': '736276CC-0825-4738-A2E8-CDD740C7F635'}}]})"
  }
}
```

---

## Implementation Approach

### Data Source
```python
props = await archicad.tapir("GetAllProperties")
```

Returns list of:
```python
{
    "propertyId": {"guid": "..."},
    "propertyType": "StaticBuiltIn",
    "propertyGroupName": "Wall",
    "propertyName": "Length of Reference Line",
    "propertyCollectionType": "Single",
    "propertyValueType": "Real",
    "propertyMeasureType": "Length",
    "propertyIsEditable": false
}
```

### Search Implementation
Use same approach as `get_docs` search:
1. Build index from property names and groups
2. Use fuzzy matching with threshold
3. Score by relevance (exact match > partial > fuzzy)

### Caching Strategy
Properties don't change during a session. Cache on first call per port:
```python
class PropertyCache:
    def __init__(self):
        self._cache: dict[int, list[dict]] = {}  # port -> properties

    async def get_properties(self, conn: ArchicadConnection) -> list[dict]:
        if conn.port not in self._cache:
            result = await conn.execute("GetAllProperties", {})
            self._cache[conn.port] = result.get("properties", [])
        return self._cache[conn.port]
```

### Filter Logic
```python
def filter_properties(
    properties: list[dict],
    search: str | None,
    group: str | None,
    property_type: str | None,
    measure_type: str | None,
) -> list[dict]:
    results = properties

    if group:
        results = [p for p in results
                   if group.lower() in p["propertyGroupName"].lower()]

    if property_type:
        results = [p for p in results
                   if p["propertyType"] == property_type]

    if measure_type:
        results = [p for p in results
                   if p.get("propertyMeasureType") == measure_type]

    if search:
        results = fuzzy_search(results, search, key="propertyName")

    return results
```

---

## Integration with Other Tools

### get_docs hint
When `get_docs` search matches element type + attribute word but finds no commands:
```json
{
  "results": [...],
  "hint": "For element properties like area/length/volume, use get_properties(search='length', group='Wall')"
}
```

### execute_script docstring
Add to COMMON PATTERNS section:
```
# Get property values
props = await archicad.tapir("GetPropertyValuesOfElements", {
    "elements": elements,
    "properties": [{"propertyId": {"guid": "YOUR-GUID-HERE"}}]
})

# Find property GUIDs using: get_properties(search="...", group="...")
```

---

## Edge Cases

### No matches
```json
{
  "query": {"search": "banana", "group": "Wall"},
  "total": 0,
  "properties": [],
  "suggestions": [
    "Try broader search terms",
    "Check available groups with get_properties()"
  ]
}
```

### Results exceed limit
Truncate and guide AI to narrow search:
```json
{
  "total": 443,
  "showing": 50,
  "properties": [...first 50...],
  "tip": "443 results truncated. Add 'group' filter to narrow (e.g., group='Wall')"
}
```

### Invalid group name
Return empty with suggestions:
```json
{
  "query": {"group": "Walls"},
  "total": 0,
  "suggestion": "Did you mean 'Wall'? Use get_properties() to see all groups."
}
```

---

## File Structure

```
src/archicad_mcp/
├── server.py              # Add get_properties tool registration
├── properties/
│   ├── __init__.py
│   ├── cache.py           # PropertyCache class
│   └── search.py          # Search and filter logic
```

Or simpler, add to existing:
```
src/archicad_mcp/
├── server.py              # Add get_properties tool
├── core/
│   └── properties.py      # PropertyCache + search logic
```

---

## Open Questions

1. **Should we pre-load properties at startup?**
   - Pro: Faster first query
   - Con: Requires Archicad connection at startup (might not be ready)
   - Recommendation: Lazy load on first call, cache after

2. **Should built-in properties be cached statically?**
   - Pro: Works without connection for common queries
   - Con: GUIDs might vary across Archicad versions
   - Recommendation: Start with live-only, add static cache later if needed

3. **Truncation limit for results?**
   - DECIDED: Default 50, max 200, configurable via `limit` parameter
   - No `offset` - AI should narrow search, not paginate

4. **Should we include property descriptions?**
   - Archicad properties don't have descriptions in API
   - Could add curated descriptions for top 50 properties
   - Recommendation: Skip for v1, add later if needed
