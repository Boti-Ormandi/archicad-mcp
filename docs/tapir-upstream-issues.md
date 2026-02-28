# Tapir Upstream Issues

Issues discovered in Tapir that should be reported/fixed upstream. These are bugs or missing features in the Tapir add-on itself, not our MCP wrapper.

**Repository**: https://github.com/ENZYME-APD/tapir-archicad-automation

**References**:
archicad-api-devkit
multiconn_archicad

---

## Issue 1: Slab Level Parameter Doubles at Story Boundaries

### Status: PR SUBMITTED - [#348](https://github.com/ENZYME-APD/tapir-archicad-automation/pull/348)

### Severity: HIGH

### Description
When creating a slab with `CreateSlabs`, the `level` parameter behaves incorrectly at story boundaries. If `level` equals or exceeds a story's level, the slab is assigned to that story AND the level value is added to the story level, effectively doubling the Z coordinate.

### Steps to Reproduce

```python
# Story structure: Story 0 = 0m, Story 1 = 3m, Story 2 = 6m

# Test 1: level = 2.9 (below Story 1)
CreateSlabs(slabsData=[{
    "level": 2.9,
    "polygonCoordinates": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
}])
# Result: floorIndex=0, zCoordinate=2.9 (CORRECT - absolute Z)

# Test 2: level = 3.0 (equals Story 1 level)
CreateSlabs(slabsData=[{
    "level": 3.0,
    "polygonCoordinates": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
}])
# Result: floorIndex=1, zCoordinate=6.0 (BUG - story level + slab level = 3+3)

# Test 3: level = 3.1 (above Story 1 level)
CreateSlabs(slabsData=[{
    "level": 3.1,
    "polygonCoordinates": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
}])
# Result: floorIndex=1, zCoordinate=6.1 (BUG - 3 + 3.1 = 6.1)
```

### Expected Behavior
| Input level | Expected floorIndex | Expected zCoordinate |
|-------------|---------------------|----------------------|
| 2.9 | 0 | 2.9 |
| 3.0 | 1 | 3.0 |
| 3.1 | 1 | 3.1 |

### Actual Behavior
| Input level | Actual floorIndex | Actual zCoordinate |
|-------------|-------------------|---------------------|
| 2.9 | 0 | 2.9 |
| 3.0 | 1 | **6.0** (doubled!) |
| 3.1 | 1 | **6.1** (3 + 3.1) |

### Root Cause Analysis

**File**: `ElementCreationCommands.cpp`, lines 282-286

**Buggy code**:
```cpp
GS::Optional<GS::ObjectState> CreateSlabsCommand::SetTypeSpecificParameters (...) const
{
    parameters.Get ("level", element.slab.level);  // Sets to absolute value (e.g., 3.0)
    const auto floorIndexAndOffset = GetFloorIndexAndOffset (element.slab.level, stories);
    element.header.floorInd = floorIndexAndOffset.first;  // Assigns to Story 1
    // BUG: element.slab.level is still 3.0, not converted to offset!
    // ACAPI interprets slab.level as RELATIVE to story, so: 3 (story) + 3 (level) = 6
```

**Correct pattern** (from CreateColumnsCommand, line 164):
```cpp
element.header.floorInd = floorIndexAndOffset.first;
element.column.bottomOffset = floorIndexAndOffset.second;  // Uses OFFSET, not absolute
```

**Also correct** (from CreateObjectsCommand, line 740):
```cpp
element.object.level = floorIndexAndOffset.second;  // Uses OFFSET
```

### Proposed Fix

```cpp
// In CreateSlabsCommand::SetTypeSpecificParameters (line 284-286)
double inputLevel;
parameters.Get ("level", inputLevel);
const auto floorIndexAndOffset = GetFloorIndexAndOffset (inputLevel, stories);
element.header.floorInd = floorIndexAndOffset.first;
element.slab.level = floorIndexAndOffset.second;  // FIX: Use offset, not absolute
```

**One-line change**: Line 284 should store to a temp variable, then line 286 (after) should set `element.slab.level = floorIndexAndOffset.second`

### Detailed Trace

`GetFloorIndexAndOffset(zPos, stories)` in `CommandBase.cpp:341-357`:
```cpp
// Returns {storyIndex, zPos - storyLevel}
// For zPos=3.0, stories at 0m, 3m, 6m:
// → Returns {1, 0.0} (Story 1, offset 0)
```

**Current behavior** with `level=3.0`:
1. `element.slab.level = 3.0` (raw input)
2. `GetFloorIndexAndOffset(3.0)` returns `{1, 0.0}`
3. `element.header.floorInd = 1` (Story 1, at 3m)
4. `element.slab.level` stays `3.0` (not updated!)
5. ACAPI creates slab at: Story 1 level (3m) + slab.level (3m) = **6m**

**Fixed behavior** with `level=3.0`:
1. `inputLevel = 3.0`
2. `GetFloorIndexAndOffset(3.0)` returns `{1, 0.0}`
3. `element.header.floorInd = 1`
4. `element.slab.level = 0.0` (the offset!)
5. ACAPI creates slab at: Story 1 level (3m) + slab.level (0m) = **3m** ✓

### Workaround
Use `level = targetZ - 0.01` to stay just below the story boundary:
```python
# To place slab at z=3m, use level=2.99
CreateSlabs(slabsData=[{"level": 2.99, ...}])
```

### Affected Commands
- `CreateSlabs` - confirmed
- Possibly `CreateMeshes`, `CreateZones` - untested

---

## Issue 2: Missing CreateWalls Command

### Severity: HIGH

### Description
`CreateWalls` is not implemented despite walls being the most fundamental BIM element. Tapir can query walls (`GetElementsByType`, `GetDetailsOfElements`) but cannot create them.

### Current State

**Implemented element creation commands:**
- CreateColumns
- CreateSlabs
- CreateZones
- CreateObjects
- CreateMeshes
- CreatePolylines
- CreateLabels

**NOT implemented:**
- CreateWalls
- CreateBeams
- CreateRoofs
- CreateDoors/Windows

### Technical Analysis

The native Archicad API fully supports wall creation via `API_WallType`:

```cpp
// From archicad-api-devkit
struct API_WallType {
    // Geometry
    API_Coord begC, endC;        // Wall endpoints
    double angle;                 // For arc walls

    // Height
    double height;
    double bottomOffset, topOffset;

    // Thickness
    double thickness, thickness1; // thickness1 for trapezoid walls

    // Wall type
    short geometryType;          // APIWtyp_Normal, APIWtyp_Trapez, APIWtyp_Poly

    // Materials
    API_OverriddenAttribute refMat, oppMat, sidMat;

    // ... more fields
};
```

Wall geometry types requiring different parameters:
1. **Straight (APIWtyp_Normal)**: begC, endC, height, thickness
2. **Trapezoid (APIWtyp_Trapez)**: + thickness1 (varying thickness)
3. **Polygonal (APIWtyp_Poly)**: + polygonOutline coordinates

### Proposed Implementation

```cpp
// In ElementCreationCommands.hpp
class CreateWallsCommand : public CreateElementsCommandBase {
    virtual GS::String GetName() const override { return "CreateWalls"; }
    virtual API_ElemTypeID GetElemTypeID() const override { return API_WallID; }
    virtual GSErrCode SetTypeSpecificParameters(
        API_Element& element,
        API_ElementMemo& memo,
        const GS::ObjectState& parameters) const override;
};
```

Parameters schema:
```json
{
  "wallsData": [{
    "geometryType": "Straight|Trapezoid|Polygonal",
    "begCoordinate": {"x": 0, "y": 0},
    "endCoordinate": {"x": 5, "y": 0},
    "height": 3.0,
    "thickness": 0.3,
    "thickness1": 0.2,           // For Trapezoid only
    "polygonCoordinates": [...], // For Polygonal only
    "bottomOffset": 0
  }]
}
```

### Complexity Estimate
- Schema definition: ~50 lines
- SetTypeSpecificParameters: ~150 lines (handle 3 geometry types)
- Tests: ~100 lines
- Total: ~300 lines of code

---

## Issue 3: Silent Fallback for Invalid Element/Attribute Types

### Status: PR SUBMITTED - [#349](https://github.com/ENZYME-APD/tapir-archicad-automation/pull/349)

**Detailed analysis**: See `docs/tapir-invalid-elementtype-pr.md`

### Severity: MEDIUM

### Description
Commands with type parameters (`elementType`, `attributeType`, `connectedElementType`) silently return wrong results for invalid/misspelled values instead of errors.

### Affected Commands (4 total)

| Command | Parameter | File | Line | Fallback Behavior |
|---------|-----------|------|------|-------------------|
| GetElementsByType | elementType | ElementCommands.cpp | 64 | Returns ALL elements |
| GetFavoritesByType | elementType | FavoritesCommands.cpp | 54 | Returns all favorites |
| GetConnectedElements | connectedElementType | ElementCommands.cpp | 1297 | Returns all connected |
| GetAttributesByType | attributeType | AttributeCommands.cpp | 116 | Returns empty/all attributes |

### Steps to Reproduce
```python
# Invalid type - returns ALL elements instead of error
GetElementsByType(elementType="Bananas")
# Returns: ALL 28 elements (no error)

# Wrong case (types are case-sensitive)
GetElementsByType(elementType="wall")  # lowercase
# Returns: ALL 28 elements (no error)

# Attribute types have same issue
GetAttributesByType(attributeType="surface")  # should be "Surface"
# Returns: empty or wrong results
```

### Root Cause

**Conversion functions return fallback values:**
```cpp
// CommandBase.cpp:453-528
API_ElemTypeID GetElementTypeFromNonLocalizedName (const GS::UniString& typeStr)
{
    if (typeStr == "Wall") return API_WallID;
    if (typeStr == "Column") return API_ColumnID;
    // ... 70 more types ...
    return API_ZombieElemID;  // ANY unrecognized string → fallback
}

// AttributeCommands.cpp:17-44
static API_AttrTypeID ConvertAttributeTypeStringToID (const GS::UniString& typeStr)
{
    if (typeStr == "Layer") return API_LayerID;
    // ... more types ...
    return API_ZombieAttrID;  // ANY unrecognized string → fallback
}
```

**No validation after conversion:**
```cpp
// ElementCommands.cpp:61-76
API_ElemTypeID elemType = API_ZombieElemID;
if (parameters.Get ("elementType", elementTypeStr)) {
    elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
    // NO VALIDATION - API_ZombieElemID means "all elements"
}
ACAPI_Element_GetElemList (elemType, &elemList, filterFlags);
```

### Additional Bug Found: Line 150 Ignored Return Value

In `GetElementsByType::Execute`, line 150 ignores the helper's return value:
```cpp
if (!databasesParameterExists || databases.IsEmpty ()) {
    GetElementsFromCurrentDatabase (parameters, elements);  // Return value IGNORED!
}
```
If `ACAPI_Element_GetElemList` fails inside the helper, the error is silently swallowed.

### Proposed Fix: Option C (Refactor Helper)

Chosen approach after analysis of 4 options:
1. Refactor helper signature: `GetElementsFromCurrentDatabase(elemType, filterFlags, elements)`
2. Move validation to Execute function
3. Check helper return value (fixes line 150 bug)
4. Error messages include invalid value and case-sensitivity note

```cpp
// In Execute:
if (parameters.Get ("elementType", elementTypeStr)) {
    elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
    if (elemType == API_ZombieElemID) {
        return CreateErrorResponse (APIERR_BADPARS,
            "Invalid elementType '" + elementTypeStr + "'. Element types are case-sensitive.");
    }
}
```

### Files to Modify
- `ElementCommands.cpp` - GetElementsByType (refactor helper + Execute), GetConnectedElements
- `FavoritesCommands.cpp` - GetFavoritesByType
- `AttributeCommands.cpp` - GetAttributesByType

---

## Issue 4: Undocumented Unit System (Meters)

### Severity: LOW

### Description
All Tapir coordinate and dimension parameters use **meters**, but this is not documented. Users coming from Archicad (which uses project units, often mm) naturally assume mm.

### Examples
```python
# User expects mm (common in architecture)
CreateColumns(columnsData=[{"coordinates": {"x": 5000, "y": 3000, "z": 0}}])
# Creates column at x=5000m, y=3000m (5km away!)

# Correct usage (meters)
CreateColumns(columnsData=[{"coordinates": {"x": 5, "y": 3, "z": 0}}])
# Creates column at x=5m, y=3m
```

### Affected Parameters
- All coordinate values (x, y, z)
- All dimensions (height, thickness, level, offset)
- All geometry (polygonCoordinates, begC, endC)

### Proposed Fix
Add clear documentation to each command schema:
```json
{
  "description": "X coordinate in METERS (not mm)"
}
```

Or add a note section to command documentation.

---

## Issue 5: Surface/Material Assignment Not Exposed

### Severity: MEDIUM

### Description
Cannot change surface materials on elements via Tapir. The native API supports this (`refMat`, `oppMat`, `sidMat` fields) but `SetDetailsOfElements` only implements geometry fields.

### User Request Example
"Change all walls to use Brick surface on the exterior"

### Current Limitation
```python
# GetDetailsOfElements returns geometry but not materials
details = GetDetailsOfElements(elements=[...])
# Returns: begCoordinate, endCoordinate, height, thickness
# Does NOT return: refMat, oppMat, sidMat

# SetDetailsOfElements cannot set materials
SetDetailsOfElements(elementsWithDetails=[{
    "elementId": {"guid": "..."},
    "details": {"refMat": ???}  # Not supported
}])
```

### Native API Support
```cpp
// API_WallType has material override fields
struct API_WallType {
    API_OverriddenAttribute refMat;  // Reference side material
    API_OverriddenAttribute oppMat;  // Opposite side material
    API_OverriddenAttribute sidMat;  // Side (edge) material
};
```

### Proposed Implementation
Extend `GetDetailsOfElements` and `SetDetailsOfElements` to include material fields:

```json
{
  "details": {
    "refMat": {"attributeIndex": 15, "overridden": true},
    "oppMat": {"attributeIndex": 15, "overridden": true},
    "sidMat": {"attributeIndex": 8, "overridden": true}
  }
}
```

---

## Issue 6: GetElementsByType Filter Behavior Undocumented

### Severity: LOW

### Description
The `filters` parameter behavior is not clearly documented. It's an array of strings, not objects.

### Incorrect (what users guess)
```python
GetElementsByType(
    elementType="Wall",
    filters=[{"type": "layer", "value": "A-WALL"}]  # WRONG
)
```

### Correct
```python
GetElementsByType(
    elementType="Wall",
    filters=["IsEditable", "OnActualFloor"]  # Simple string array
)
```

### Valid Filter Values
- IsEditable
- IsVisibleByLayer
- IsVisibleByRenovation
- IsVisibleIn3D
- OnActualFloor
- OnActualLayout
- InMyWorkspace
- HasAccessRight

---

## Issue 7: Invalid Filter Values Silently Ignored

### Severity: LOW

### Description
Invalid filter strings in `GetElementsByType` and similar commands are silently ignored. Unlike invalid element types (Issue 3), this causes partial degradation rather than completely wrong results.

### Related To
Discovered during Issue 3 investigation. Different severity justifies separate handling.

### Steps to Reproduce
```python
# Invalid filter - silently ignored
GetElementsByType(elementType="Wall", filters=["typo"])
# Returns: ALL walls (filter had no effect, no error)

# Mixed valid/invalid - valid ones apply, invalid ignored
GetElementsByType(elementType="Wall", filters=["IsEditable", "typo"])
# Returns: Editable walls only (IsEditable applied, "typo" silently ignored)
```

### Root Cause
```cpp
// ElementCommands.cpp:17-44
static API_ElemFilterFlags ConvertFilterStringToFlag (const GS::UniString& filter)
{
    if (filter == "IsEditable") return APIFilt_IsEditable;
    if (filter == "IsVisibleByLayer") return APIFilt_OnVisLayer;
    // ... more valid filters ...
    return APIFilt_None;  // Invalid filter → 0 (no filtering)
}

// Usage - OR with 0 does nothing
for (const GS::UniString& filter : filters) {
    filterFlags |= ConvertFilterStringToFlag (filter);  // Invalid = |= 0 = ignored
}
```

### Inconsistency
`FilterElements` command validates filters and errors if none are valid (line 1775-1777):
```cpp
if (filterFlags == APIFilt_None) {
    return CreateErrorResponse (APIERR_BADPARS, "Invalid or missing filters!");
}
```

But `GetElementsByType` doesn't validate - filters are optional there.

### Expected Behavior
Either:
1. Error on invalid filter names (strict)
2. Warning in response that some filters were unrecognized (lenient)

### Proposed Fix
Not fixing in Issue 3 PR - different severity and would expand scope.

---

## Issue 8: Required Parameters Not Enforced at Runtime

### Severity: LOW

### Description
JSON schema marks parameters like `elementType` as `required`, but code allows missing values and falls back to "all elements" behavior.

### Related To
Discovered during Issue 3 investigation. Different bug class (schema validation vs value validation).

### Steps to Reproduce
```python
# Schema says elementType is required, but this works:
# (assuming Archicad's JSON schema validation doesn't enforce enums/required)

# Missing elementType falls back to API_ZombieElemID = all elements
GetElementsByType(filters=["IsEditable"])  # No elementType
# Returns: ALL editable elements (not an error)
```

### Root Cause
```cpp
// ElementCommands.cpp:61-65
API_ElemTypeID elemType = API_ZombieElemID;  // Default = "all"
GS::UniString elementTypeStr;
if (parameters.Get ("elementType", elementTypeStr)) {
    // Only runs if parameter exists
    elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
}
// If missing: elemType stays API_ZombieElemID
```

Schema definition (GetElementsByType, line 117-118):
```json
"required": ["elementType"]
```

### Impact
- Scripts that accidentally omit `elementType` get all elements instead of error
- GitHub Issue #220 shows Archicad validates structure but not enum/required constraints

### Why Not Fixing in Issue 3 PR
1. Different bug class (missing vs invalid)
2. Could break scripts relying on "all elements" fallback behavior
3. Schema enforcement is broader architectural question
4. Fixing might require changes across many commands

---

## Summary Table

| # | Issue | Severity | Type | Status |
|---|-------|----------|------|--------|
| 1 | Slab level doubles at story boundary | HIGH | Bug | **PR #348** |
| 2 | Missing CreateWalls | HIGH | Feature gap | Open |
| 3 | Silent invalid element/attribute type fallback | MEDIUM | Bug | **PR #349** |
| 4 | Undocumented meter units | LOW | Documentation | Open |
| 5 | Surface assignment not exposed | MEDIUM | Feature gap | Open |
| 6 | Filter behavior undocumented | LOW | Documentation | Open |
| 7 | Invalid filter values silently ignored | LOW | Bug | Open (related to #3) |
| 8 | Required parameters not enforced | LOW | Bug | Open (related to #3) |

---

## PR Strategy

### Completed
- [x] **Slab level bug fix** - PR #348 submitted
- [x] **Invalid type validation** - PR #349 submitted

### Quick Wins (Good First PRs)
1. **Documentation PRs** - Add unit documentation, filter examples
2. **Issues 7 & 8** - Could be addressed now, but lower priority

### Medium Effort
3. **Surface assignment** - Extend existing Get/SetDetails commands

### Larger Contribution
4. **CreateWalls** - New command, but follows existing patterns

### Next Steps
1. Wait for PR #348 and #349 review/merge
2. Add CreateWalls (significant contribution)
