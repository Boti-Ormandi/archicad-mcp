# Tapir PR: Validate Invalid Element/Attribute Types

## Status: PR Submitted - [#349](https://github.com/ENZYME-APD/tapir-archicad-automation/pull/349)

## Decision: Option C (Refactor Helper Signature)

After analysis, Option C is the cleanest solution:
- No double-parsing
- Fixes line 150 ignored return value bug
- Matches existing validation pattern (imageType in GetElementPreviewImageCommand)
- Error messages include the invalid input string

## Summary

Invalid or misspelled type parameter values silently return unexpected results instead of errors. This affects both `elementType` and `attributeType` parameters across multiple commands.

## Problem Statement

When calling commands with invalid type strings:

```python
# Element type examples - all silently return ALL elements:
GetElementsByType(elementType="Bananas")  # Invalid type
GetElementsByType(elementType="wall")     # Wrong case (should be "Wall")
GetElementsByType(elementType="WALL")     # Wrong case

# Attribute type examples - may return empty or all attributes:
GetAttributesByType(attributeType="surface")  # Wrong case (should be "Surface")
GetAttributesByType(attributeType="Material") # Wrong name (should be "Surface")
```

**Expected behavior**: Return an error with valid types listed.
**Actual behavior**: Returns all elements/empty results without any error indication.

## Impact

- **AI/Automation tools**: Cannot distinguish between "valid type with 0 results" and "invalid type"
- **Script debugging**: Silent failures cause hard-to-trace bugs
- **Case sensitivity**: Users don't know types are case-sensitive; common mistake is lowercase

## Root Cause Analysis

### Schema vs Runtime Validation Gap

The JSON schema defines enums for valid types:
```json
// CommonSchemaDefinitions.json:667-731
"ElementType": {
    "type": "string",
    "enum": ["Wall", "Column", "Beam", ...]
}

// CommonSchemaDefinitions.json:48-63
"AttributeType": {
    "type": "string",
    "enum": ["Layer", "Line", "Fill", "Surface", ...]
}
```

However, Archicad's JSON Schema validation only catches structural issues (wrong keys, missing required fields) but **does not enforce enum constraints at runtime** (see GitHub Issue #220).

### Code Pattern (Same in All Affected Commands)

```cpp
// Initialize to "all" fallback value
API_ElemTypeID elemType = API_ZombieElemID;  // or API_ZombieAttrID for attributes

// Convert string to type ID
GS::UniString typeStr;
if (parameters.Get ("elementType", typeStr)) {
    elemType = GetElementTypeFromNonLocalizedName (typeStr);  // Returns API_ZombieElemID for invalid
}

// NO VALIDATION - invalid types silently use fallback
ACAPI_Element_GetElemList (elemType, &elemList, filterFlags);  // ZombieElemID = all elements
```

### Conversion Functions Return Fallback Values

**`GetElementTypeFromNonLocalizedName`** (CommandBase.cpp:453-528):
```cpp
API_ElemTypeID GetElementTypeFromNonLocalizedName (const GS::UniString& typeStr)
{
    if (typeStr == "Wall") return API_WallID;
    if (typeStr == "Column") return API_ColumnID;
    // ... 70 more types ...
    return API_ZombieElemID;  // Line 527: ANY unrecognized string
}
```

**`ConvertAttributeTypeStringToID`** (AttributeCommands.cpp:17-44):
```cpp
static API_AttrTypeID ConvertAttributeTypeStringToID (const GS::UniString& typeStr)
{
    if (typeStr == "Layer") return API_LayerID;
    if (typeStr == "Surface") return API_MaterialID;
    // ... more types ...
    return API_ZombieAttrID;  // Line 43: ANY unrecognized string
}
```

## Affected Commands (Complete List)

| Command | Parameter | File | Line | Fallback Behavior |
|---------|-----------|------|------|-------------------|
| GetElementsByType | elementType | ElementCommands.cpp | 64 | Returns ALL elements |
| GetFavoritesByType | elementType | FavoritesCommands.cpp | 54 | Returns all favorites |
| GetConnectedElements | connectedElementType | ElementCommands.cpp | 1297 | Returns all connected |
| GetAttributesByType | attributeType | AttributeCommands.cpp | 116 | Returns empty/all attributes |

### Commands NOT Affected (Intentional Wildcard)

| Command | Why Not Affected |
|---------|------------------|
| GetAllElements | No type parameter |
| Element change notifications | `API_ZombieElemID` used intentionally (NotificationCommands.cpp:188) |

## Valid Types (Case-Sensitive)

### Element Types (72 total)

From `CommandBase.cpp:455-526`:

**Building Elements:**
Wall, Column, Beam, Slab, Roof, Shell, Morph, Mesh, Stair, Railing, CurtainWall, Opening

**Openings:**
Window, Door, Skylight

**Library Parts:**
Object, Lamp, Zone

**2D Elements:**
Line, PolyLine, Arc, Circle, Spline, Hatch, Hotspot

**Annotations:**
Text, Label, Dimension, RadialDimension, LevelDimension, AngleDimension

**Documentation:**
CutPlane, Detail, Elevation, InteriorElevation, Worksheet, Drawing, SectElem, Camera, CamSet, ChangeMarker, Picture

**Subelements:**
CurtainWallSegment, CurtainWallFrame, CurtainWallPanel, CurtainWallJunction, CurtainWallAccessory,
Riser, Tread, StairStructure,
RailingToprail, RailingHandrail, RailingRail, RailingPost, RailingInnerPost, RailingBaluster, RailingPanel, RailingSegment, RailingNode, RailingBalusterSet, RailingPattern, RailingToprailEnd, RailingHandrailEnd, RailingRailEnd, RailingToprailConnection, RailingHandrailConnection, RailingRailConnection, RailingEndFinish,
BeamSegment, ColumnSegment

**Other:**
Group, Hotlink

### Attribute Types (12 total)

From `AttributeCommands.cpp:19-42`:

Layer, Line, Fill, Composite, Surface, LayerCombination, ZoneCategory, Profile, PenTable, MEPSystem, OperationProfile, BuildingMaterial

## Code Structure Analysis

### GetElementsByType - Complex Case

```cpp
// ElementCommands.cpp:58-85 - HELPER FUNCTION (template, returns GSErrCode)
template <typename ListProxyType>
static GSErrCode GetElementsFromCurrentDatabase (const GS::ObjectState& parameters, ListProxyType& elementsListProxy)
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);  // Line 64
    }
    // ... uses elemType, returns GSErrCode
}

// ElementCommands.cpp:142-174 - EXECUTE FUNCTION
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");

    GS::Array<GS::ObjectState> databases;
    bool databasesParameterExists = parameters.Get ("databases", databases);
    if (!databasesParameterExists || databases.IsEmpty ()) {
        GetElementsFromCurrentDatabase (parameters, elements);  // Line 150 - RETURN VALUE IGNORED!
    } else {
        // ... multi-database case
        return GetElementsFromCurrentDatabase (parameters, elements);  // Line 158 - return value USED
    }
    return response;
}
```

**Key observations:**
- Helper parses `elementType` internally (line 64)
- Helper returns `GSErrCode`, not `GS::ObjectState`
- Line 150: return value is **ignored** (potential existing bug)
- Line 158: return value is used

### Other Commands - Simpler Cases

```cpp
// FavoritesCommands.cpp:49-74 - ALL LOGIC IN EXECUTE
GS::ObjectState GetFavoritesByTypeCommand::Execute (...) const
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);  // Line 54
    }
    // ... uses elemType directly
}

// ElementCommands.cpp:1289-1325 - ALL LOGIC IN EXECUTE
GS::ObjectState GetConnectedElementsCommand::Execute (...) const
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("connectedElementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);  // Line 1297
    }
    // ... uses elemType directly
}

// AttributeCommands.cpp:111-135 - ALL LOGIC IN EXECUTE
GS::ObjectState GetAttributesByTypeCommand::Execute (...) const
{
    GS::UniString typeStr;
    parameters.Get ("attributeType", typeStr);
    API_AttrTypeID typeID = ConvertAttributeTypeStringToID (typeStr);  // Line 116
    // ... uses typeID directly
}
```

### Existing Validation Pattern in Codebase

```cpp
// ElementCommands.cpp:2215-2224 - VALIDATE AND USE IN SAME BLOCK
if (parameters.Get ("imageType", imageTypeStr)) {
    if (imageTypeStr == "2D") {
        image.view = APIImage_Model2D;
    } else if (imageTypeStr == "Section") {
        image.view = APIImage_Section;
    } else if (imageTypeStr == "3D") {
        image.view = APIImage_Model3D;
    } else {
        return CreateErrorResponse (APIERR_BADPARS, "Invalid imageType parameter.");
    }
}
```

**Pattern:** Validate and assign in same if/else chain. No double parsing.

---

## Proposed Fix Options

### Option A: Validate in Execute, Double-Parse for GetElementsByType

Add validation at start of each `Execute` function. For `GetElementsByType`, this means parsing `elementType` twice (once to validate in Execute, once in helper).

**GetElementsByType (double-parse):**
```cpp
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    // VALIDATION - parses elementType
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        if (GetElementTypeFromNonLocalizedName (elementTypeStr) == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }

    // EXISTING CODE - helper parses elementType AGAIN internally
    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");
    // ...
    GetElementsFromCurrentDatabase (parameters, elements);  // Parses again at line 64
}
```

**Other commands (no double-parse, clean):**
```cpp
GS::ObjectState GetFavoritesByTypeCommand::Execute (...) const
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }
    // ... uses elemType
}
```

**Pros:**
- Minimal changes to existing code structure
- No function signature changes
- Simple to implement and review

**Cons:**
- Double-parsing for `GetElementsByType` (inefficient, unusual)
- Doesn't fix the ignored return value at line 150

---

### Option B: Validate in Helper, Fix Ignored Return Value

Modify helper to return error code on invalid type, AND fix line 150 to check return value.

```cpp
// MODIFIED HELPER - validates and returns error
template <typename ListProxyType>
static GSErrCode GetElementsFromCurrentDatabase (const GS::ObjectState& parameters, ListProxyType& elementsListProxy)
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return APIERR_BADPARS;  // NEW: return error
        }
    }
    // ... rest unchanged
}

// MODIFIED EXECUTE - check return value (fixes existing bug)
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");

    GS::Array<GS::ObjectState> databases;
    bool databasesParameterExists = parameters.Get ("databases", databases);
    if (!databasesParameterExists || databases.IsEmpty ()) {
        GSErrCode err = GetElementsFromCurrentDatabase (parameters, elements);
        if (err != NoError) {
            return CreateErrorResponse (err, "Failed to retrieve elements.");
        }
    } else {
        // ... multi-database case already checks return value
    }
    return response;
}
```

**Pros:**
- No double-parsing
- Fixes existing bug (ignored return value at line 150)
- Validation logic in one place (helper)

**Cons:**
- Error message is generic (helper returns code, caller adds message)
- Can't include the actual invalid string in error message easily
- Changes behavior of helper function (could affect other callers if any added later)

---

### Option C: Refactor Helper to Take Validated elemType ✓ SELECTED

Change helper signature to accept pre-validated `elemType` instead of parsing from parameters.

```cpp
// NEW HELPER SIGNATURE
template <typename ListProxyType>
static GSErrCode GetElementsFromCurrentDatabase (
    API_ElemTypeID elemType,
    API_ElemFilterFlags filterFlags,
    ListProxyType& elementsListProxy)
{
    GS::Array<API_Guid> elemList;
    GSErrCode err = ACAPI_Element_GetElemList (elemType, &elemList, filterFlags);
    // ...
}

// EXECUTE - validates and passes to helper
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    // Parse and validate elementType
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }

    // Parse filters
    API_ElemFilterFlags filterFlags = APIFilt_None;
    GS::Array<GS::UniString> filters;
    if (parameters.Get ("filters", filters)) {
        for (const GS::UniString& filter : filters) {
            filterFlags |= ConvertFilterStringToFlag (filter);
        }
    }

    // Call helper with validated values
    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");
    GetElementsFromCurrentDatabase (elemType, filterFlags, elements);
    // ...
}
```

**Pros:**
- Cleanest design - no double-parsing, clear separation
- Matches the validate-then-use pattern in codebase
- Helper becomes simpler (just does one thing)

**Cons:**
- Larger refactor - changes function signature
- Must update both call sites (line 150 and 158)
- Filter parsing also moves to Execute (more code moved)
- Bigger PR, more review burden

---

### Option D: Add New Validation Helper Function

Create a dedicated validation function, use before calling existing helper.

```cpp
// NEW FUNCTION in CommandBase.cpp
bool IsValidElementTypeName (const GS::UniString& typeStr)
{
    return GetElementTypeFromNonLocalizedName (typeStr) != API_ZombieElemID;
}

// USAGE - validate first, then call existing helper
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        if (!IsValidElementTypeName (elementTypeStr)) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }

    // Existing code unchanged - helper parses again
    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");
    GetElementsFromCurrentDatabase (parameters, elements);
    // ...
}
```

**Pros:**
- Reusable validation function
- Minimal changes to existing code
- Easy to add for future commands

**Cons:**
- Still double-parsing for GetElementsByType
- Adds new function to API

---

## Comparison Matrix

| Aspect | Option A | Option B | **Option C** | Option D |
|--------|----------|----------|----------|----------|
| Double-parsing | Yes (1 cmd) | No | **No** | Yes (1 cmd) |
| Lines changed | ~20 | ~15 | **~50** | ~25 |
| Signature changes | No | No | **Yes** | Yes (new fn) |
| Fixes line 150 bug | No | Yes | **Yes** | No |
| Error includes input | Yes | No | **Yes** | Yes |
| Matches existing pattern | Partial | Partial | **Yes** | Partial |
| Review complexity | Low | Medium | **Medium-High** | Low |

**Selected: Option C** - Cleanest architecture, fixes existing bug, matches codebase patterns.

---

## Questions Resolved

### Q1: Filter Validation - Out of Scope

**Problem:** Invalid filters (e.g., `filters: ["typo"]`) silently return `APIFilt_None` and are ignored.

**Analysis:**
- Different failure mode: partial degradation vs complete wrong result
- `filters: ["IsEditable", "typo"]` → `APIFilt_IsEditable | APIFilt_None` = valid filter applied, invalid ignored
- Filters are optional, so "no valid filter = no filtering" is defensible
- FilterElements already validates (line 1775-1777), creating command-specific precedent

**Decision:** Note in PR as related issue, don't fix. Would expand scope significantly.

### Q2: Required Parameters Not Enforced - Out of Scope

**Problem:** Schema marks `elementType` as required, but code allows missing parameter (falls back to all elements).

**Analysis:**
- Different bug class: schema enforcement vs invalid value handling
- Fixing would break scripts that omit the parameter and rely on "all elements" behavior
- Schema validation is a broader architectural question

**Decision:** Note in PR as related issue, don't fix. Different concern, could break existing scripts.

### Q3: Error Code - APIERR_BADPARS

**Analysis:** Consistent usage throughout codebase for both missing and invalid parameters:
```cpp
CreateErrorResponse (APIERR_BADPARS, "Invalid imageType parameter.")
CreateErrorResponse (APIERR_BADPARS, "Invalid format parameter.")
CreateErrorResponse (APIERR_BADPARS, "elementId is missing")
```

**Decision:** Use `APIERR_BADPARS` consistently.

### Q4: Line 150 Bug - Fix in Same PR

**Problem:** Return value from `GetElementsFromCurrentDatabase` is ignored at line 150.

**Decision:** Fix in same PR. Option C naturally addresses this by:
1. Moving validation to Execute (errors returned before helper called)
2. Checking helper return value after validation passes

## Implementation Details

### Error Response Pattern

Match existing pattern from GetElementPreviewImageCommand (line 2223):
```cpp
return CreateErrorResponse (APIERR_BADPARS, "Invalid imageType parameter.");
```

Our messages will include the invalid value for better debugging:
```cpp
return CreateErrorResponse (APIERR_BADPARS,
    "Invalid elementType '" + elementTypeStr + "'.");
```

Error code `APIERR_BADPARS` is used consistently throughout codebase for parameter validation failures.

## Testing

```python
# Test 1: Valid element type
GetElementsByType(elementType="Wall")
# Expected: Only walls returned

# Test 2: Invalid element type
GetElementsByType(elementType="Bananas")
# Expected: {"error": {"code": -2130313215, "message": "Invalid elementType 'Bananas'..."}}

# Test 3: Wrong case element type
GetElementsByType(elementType="wall")
# Expected: {"error": {"code": -2130313215, "message": "Invalid elementType 'wall'..."}}

# Test 4: Valid attribute type
GetAttributesByType(attributeType="Surface")
# Expected: All surface attributes

# Test 5: Invalid attribute type
GetAttributesByType(attributeType="surface")
# Expected: {"error": {"code": -2130313215, "message": "Invalid attributeType 'surface'..."}}

# Test 6: GetFavoritesByType validation
GetFavoritesByType(elementType="bananas")
# Expected: Error

# Test 7: GetConnectedElements validation
GetConnectedElements(elements=[...], connectedElementType="bananas")
# Expected: Error
```

## Related Issues

- **GitHub Issue #220**: Shows Archicad validates schema structure but not enum values
- **GitHub Issue #347**: Related filter behavior issues (different problem)
- **No existing issue** for invalid type fallback

## Implementation Plan (Option C)

### File Changes

**1. ElementCommands.cpp - GetElementsByType**

Refactor helper signature and move validation to Execute:

```cpp
// NEW HELPER SIGNATURE (line ~58)
template <typename ListProxyType>
static GSErrCode GetElementsFromCurrentDatabase (
    API_ElemTypeID elemType,
    API_ElemFilterFlags filterFlags,
    ListProxyType& elementsListProxy)
{
    GS::Array<API_Guid> elemList;
    GSErrCode err = ACAPI_Element_GetElemList (elemType, &elemList, filterFlags);
    if (err != NoError) {
        return err;
    }
    for (const API_Guid& elemGuid : elemList) {
        elementsListProxy (CreateElementIdObjectState (GetParentElemOfSectElem (elemGuid)));
    }
    return NoError;
}

// MODIFIED EXECUTE (line ~142)
GS::ObjectState GetElementsByTypeCommand::Execute (...) const
{
    // Parse and validate elementType
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }

    // Parse filters
    API_ElemFilterFlags filterFlags = APIFilt_None;
    GS::Array<GS::UniString> filters;
    if (parameters.Get ("filters", filters)) {
        for (const GS::UniString& filter : filters) {
            filterFlags |= ConvertFilterStringToFlag (filter);
        }
    }

    GS::ObjectState response;
    const auto& elements = response.AddList<GS::ObjectState> ("elements");

    GS::Array<GS::ObjectState> databases;
    bool databasesParameterExists = parameters.Get ("databases", databases);
    if (!databasesParameterExists || databases.IsEmpty ()) {
        GSErrCode err = GetElementsFromCurrentDatabase (elemType, filterFlags, elements);
        if (err != NoError) {
            return CreateErrorResponse (err, "Failed to retrieve elements.");
        }
    } else {
        // Multi-database case - validation already done, use validated values
        const auto& executionResultForDatabases = response.AddList<GS::ObjectState> ("executionResultForDatabases");
        const GS::Array<API_Guid> databaseIds = databases.Transform<API_Guid> (GetGuidFromDatabaseArrayItem);

        auto action = [&]() -> GSErrCode {
            return GetElementsFromCurrentDatabase (elemType, filterFlags, elements);
        };
        // ... rest unchanged
    }
    return response;
}
```

**2. FavoritesCommands.cpp - GetFavoritesByType (line ~49)**

Simple inline validation (no helper):

```cpp
GS::ObjectState GetFavoritesByTypeCommand::Execute (...) const
{
    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("elementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid elementType '" + elementTypeStr + "'.");
        }
    }
    // ... rest unchanged
}
```

**3. ElementCommands.cpp - GetConnectedElements (line ~1289)**

Simple inline validation:

```cpp
GS::ObjectState GetConnectedElementsCommand::Execute (...) const
{
    // ... elements parsing ...

    API_ElemTypeID elemType = API_ZombieElemID;
    GS::UniString elementTypeStr;
    if (parameters.Get ("connectedElementType", elementTypeStr)) {
        elemType = GetElementTypeFromNonLocalizedName (elementTypeStr);
        if (elemType == API_ZombieElemID) {
            return CreateErrorResponse (APIERR_BADPARS,
                "Invalid connectedElementType '" + elementTypeStr + "'.");
        }
    }
    // ... rest unchanged
}
```

**4. AttributeCommands.cpp - GetAttributesByType (line ~111)**

Simple inline validation:

```cpp
GS::ObjectState GetAttributesByTypeCommand::Execute (...) const
{
    GS::UniString typeStr;
    parameters.Get ("attributeType", typeStr);

    API_AttrTypeID typeID = ConvertAttributeTypeStringToID (typeStr);
    if (typeID == API_ZombieAttrID) {
        return CreateErrorResponse (APIERR_BADPARS,
            "Invalid attributeType '" + typeStr + "'.");
    }
    // ... rest unchanged
}
```

### Checklist

- [ ] Sync fork with upstream
- [ ] Create branch: `fix/validate-type-parameters`
- [ ] Modify ElementCommands.cpp (GetElementsByType helper + Execute, GetConnectedElements)
- [ ] Modify FavoritesCommands.cpp (GetFavoritesByType)
- [ ] Modify AttributeCommands.cpp (GetAttributesByType)
- [ ] Build AC29: `cmake --build Build/AC29 --config RelWithDebInfo`
- [ ] Test valid types work as before
- [ ] Test invalid types return errors with input string
- [ ] Test case-sensitivity (Wall vs wall)
- [ ] Submit PR

## PR Description Template

```markdown
## Summary
Add validation for elementType and attributeType parameters to return clear errors instead of silently falling back to "all elements/attributes" behavior.

## Problem
When an invalid or misspelled type is provided (e.g., "wall" instead of "Wall", or "Bananas"), commands like `GetElementsByType` silently return ALL elements instead of an error. This makes debugging automation scripts difficult and causes subtle bugs.

Root cause: `GetElementTypeFromNonLocalizedName()` returns `API_ZombieElemID` for any unrecognized string, and `ACAPI_Element_GetElemList(API_ZombieElemID, ...)` returns all elements.

## Solution
Add validation after type string conversion in 4 commands:
- GetElementsByType (also refactored helper to fix ignored return value at line 150)
- GetFavoritesByType
- GetConnectedElements
- GetAttributesByType

Invalid types now return `APIERR_BADPARS` with a terse error message that includes the invalid value provided (matching existing Tapir error patterns).

## Testing
Tested with:
- Valid types (Wall, Column, Surface) - works as before
- Invalid types (Bananas, wall, surface) - now returns error with helpful message
- Multi-database mode for GetElementsByType - validation runs once, applied to all databases

## Breaking Change
Yes, but intentional. Scripts relying on invalid types silently returning all elements will now get errors. This is the correct behavior.

## Related Issues (Not Fixed Here)
- Invalid filter values silently ignored (different severity - partial vs total failure)
- Required parameters not enforced at runtime (schema validation issue, could break existing scripts)
```

## References

- Tapir source: `tapir-archicad-automation/archicad-addon/Sources/`
- Schema: `RFIX/Images/CommonSchemaDefinitions.json`
- Error pattern: `CommandBase.cpp:55-68` (CreateErrorResponse)
- Similar validation: `ClassificationCommands.cpp:65` (APIERR_BADPARS usage)

---

## Implementation Attempt Log (2026-02-05)

### Status: ROOT CAUSE FOUND - Workaround Available

### Root Cause: Inline `oneOf` in Response Schema Crashes Archicad

Archicad's JSON schema parser crashes when processing inline `oneOf` in response schema strings defined in C++ code. However, the same `oneOf` pattern works correctly when defined in `CommonSchemaDefinitions.json`.

### Investigation Results

| Test | Result |
|------|--------|
| `CreateErrorResponse` without schema change | Works - returns schema validation error (code 4009) |
| `CreateErrorResponse` + inline `oneOf` schema | **Crashes Archicad** |
| `ZoneBoundariesResponseOrError` (in JSON file) | Works fine |

### Key Finding

The `ZoneBoundariesResponseOrError` pattern works because it's defined in `CommonSchemaDefinitions.json`:

```json
"ZoneBoundariesResponseOrError": {
    "type": "object",
    "oneOf": [
        { "$ref": "#/ZoneBoundariesResponse" },
        { "$ref": "#/ErrorItem" }
    ]
}
```

Then referenced in C++ with just `"$ref": "#/ZoneBoundariesResponseOrError"`.

### Solution

Define `ResponseOrError` schemas in `CommonSchemaDefinitions.json` for each affected command:
- `GetElementsByTypeResponseOrError`
- `GetFavoritesByTypeResponseOrError`
- `GetConnectedElementsResponseOrError`
- `GetAttributesByTypeResponseOrError`

Then reference them in C++ code with `$ref`.

### Verification (2026-02-05)

**All 4 commands tested and working:**

| Command | Invalid Type Test | Valid Type Test |
|---------|-------------------|-----------------|
| GetElementsByType | ✓ `'wall'` → error | ✓ `'Wall'` → elements |
| GetAttributesByType | ✓ `'surface'` → error | ✓ `'Surface'` → attributes |
| GetFavoritesByType | ✓ `'wall'` → error | ✓ `'Wall'` → favorites |
| GetConnectedElements | ✓ `'bananas'` → error | ✓ `'Wall'` → empty array |

- JSON schema approach works (no crash)
- Dynamic error messages with invalid value work
- Valid types still work normally

### Decision: Terse Error Messages

Error messages follow existing Tapir patterns - terse, with invalid value included:
```
"Invalid elementType 'wall'."
```

No case-sensitivity hints or valid type lists. This matches existing patterns like `"Invalid imageType parameter."` in the codebase.

### Implementation Plan (Updated)

**CommonSchemaDefinitions.json:**
1. Add `GetElementsByTypeResponse` + `GetElementsByTypeResponseOrError`
2. Add `GetFavoritesByTypeResponse` + `GetFavoritesByTypeResponseOrError`
3. Add `GetConnectedElementsResponse` + `GetConnectedElementsResponseOrError`
4. Add `GetAttributesByTypeResponse` + `GetAttributesByTypeResponseOrError`

**C++ Changes:**
1. Update response schemas to use `$ref` to new definitions
2. Add validation logic after type conversion in each Execute function
3. Error messages include the invalid value: `"Invalid elementType '" + elementTypeStr + "'."` (terse, matching existing patterns)
4. Optionally refactor `GetElementsFromCurrentDatabase` helper (can defer to keep PR focused)

### Files to Modify

- `archicad-addon/Sources/RFIX/Images/CommonSchemaDefinitions.json` - add 8 schema definitions (4 Response + 4 ResponseOrError)
- `archicad-addon/Sources/ElementCommands.cpp` - GetElementsByType + GetConnectedElements
- `archicad-addon/Sources/FavoritesCommands.cpp` - GetFavoritesByType
- `archicad-addon/Sources/AttributeCommands.cpp` - GetAttributesByType
