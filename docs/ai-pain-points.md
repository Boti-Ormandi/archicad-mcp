# AI Pain Points - Archicad MCP

Analysis of friction points when an AI assistant uses this MCP to help architects.

## Critical Issues

### 1. Silent Invalid Parameter Handling

**Problem**: Invalid parameters don't error - they return unexpected data.

```python
# "Bananas" is not a valid element type, but this returns ALL elements
GetElementsByType(elementType="Bananas")  # Returns ALL elements, no error
GetElementsByType(elementType="wall")     # Case-sensitive! Returns ALL elements
GetElementsByType(elementType="Wall")     # Correct - returns only walls
```

**Root cause**: Tapir's `GetElementTypeFromNonLocalizedName()` returns `API_ZombieElemID` (meaning "all elements") for any unrecognized string. This is Tapir's design choice, not a bug.

**Impact**: AI can't tell if API calls are correct. May give users wrong data.

**Status**: DOCUMENTED. Added warnings to `execute_command` and `execute_script` docstrings about case-sensitivity. The fix belongs in Tapir upstream, not in our MCP layer (would violate thin-wrapper philosophy).

---

### 2. Property Discovery is Unguided

**Problem**: 1800+ properties exist. No way to know which one maps to architect intent.

```
Architect asks: "Get wall areas"
AI finds 20+ area properties:
- "Area of the Wall (Archicad 22)"
- "Surface Area of the Wall Inside Face (Net)"
- "Surface Area of the Wall Outside Face (Gross)"
- ... 17 more
```

**Impact**: AI must guess or ask clarifying questions every time.

**Status**: FIXED. Added `get_properties` tool with search, group filtering, measure type filtering, and exact lookup:
- `get_properties(search="area", group="Wall")` → find wall area properties with GUIDs
- `get_properties(measure_type="Area")` → all area properties
- `get_properties(property="Length of Reference Line")` → exact GUID lookup
- Returns usage examples with `GetPropertyValuesOfElements`

---

### 3. No Element-to-Property Mapping

**Problem**: Can't answer "what properties can I query on a slab?"

`GetAllProperties` returns all 1800 properties. No `GetPropertiesForElementType(Slab)`.

**Impact**: AI can't help users explore what's queryable on their elements.

**Status**: FIXED. `get_properties(group="Slab")` returns all 40 slab properties. Property groups map to element types (Wall: 75, Column: 84, Beam: 77, Slab: 40, Zone: 46, etc.).

---

### 4. `get_docs` Doesn't Map Architect Intent

**Problem**: Searching architect vocabulary returns irrelevant results.

```
get_docs(search="wall length")
→ Returns: SetProjectInfoField, PublishPublisherSet, GenerateDocumentation
→ Should return: How to get wall length (property or geometry detail)
```

**Root cause**: Search was indexing JSON schema keywords like `minLength`, `maxLength`, `type`, etc. Fuzzy matching with 80% threshold matched "length" to "minlength".

**Impact**: AI can't quickly translate user intent to API calls.

**Status**: PARTIALLY FIXED. Added `SCHEMA_KEYWORDS` blacklist to skip JSON schema keywords during indexing. Search no longer polluted by schema structure. Still doesn't map architect intent to properties (separate issue).

---

### 5. No Common Recipes/Patterns

**Problem**: Every task requires figuring out the API chain from scratch.

Common requests with no documented pattern:
- "List all walls with their lengths"
- "Change surface of selected elements"
- "Export a schedule to Excel"
- "Find elements missing classification"
- "Get total slab area per floor"

**Impact**: AI reinvents solutions each time, may miss optimal approaches.

**Fix needed**: Recipe/pattern library in documentation.

---

## Medium Issues

### 6. "Change Surface" Workflow Undiscoverable

**Architect request**: "Change all columns to Brick surface"

**AI attempts**:
1. Search `get_docs(search="surface change")` → CreateSurfaces (wrong - creates, doesn't assign)
2. Search `get_docs(search="set material")` → Nothing relevant
3. Try `GetGDLParametersOfElements` → Error for columns, no surface params for objects
4. Try `GetDetailsOfElements` → No surface info returned

**Result**: After 4+ API calls, AI still doesn't know how to change surfaces.

**Root cause**: Tapir doesn't expose surface assignment. The native Archicad C++ API supports it (`refMat`, `oppMat`, `sidMat` fields) but Tapir's `SetDetailsOfElements` only implements geometry fields.

**Status**: CAPABILITY GAP - Requires Tapir upstream change. See [surface-assignment-feature.md](./surface-assignment-feature.md) for implementation plan and PR strategy.

---

### 7. Massive GDL Parameter Responses

- `GetGDLParametersOfElements` for ONE object = 92,000 characters
- Easily exceeds token limits
- No pagination or filtering options
- AI can't efficiently process this data

---

### 8. Geometry vs Property Ambiguity

**Problem**: Some data is in `GetDetailsOfElements`, some in properties. No guidance on which.

- Wall length: property or geometry detail?
- Slab thickness: property or geometry detail?
- Zone area: property (calculated) or geometry?

**Impact**: AI tries wrong API first, wastes calls.

---

### 9. No "Did You Mean?" Suggestions

**Problem**: When AI makes mistakes, no guidance to correct.

```
GetElementsByType(elementType="wall")  # lowercase
→ Should suggest: "Did you mean 'Wall'? Element types are case-sensitive."
```

---

### 10. Attribute ID Discovery

**Problem**: To change a surface/layer/material, need attribute ID. Discovery is multi-step.

```python
# To set wall surface to "Brick", AI must:
1. GetAttributesByType(attributeType="Surface")
2. Find "Brick" in results, get its ID
3. Then use that ID in the modification call
```

**Impact**: Simple user requests become multi-call operations.

---

## Testing Log

| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Invalid elementType | `"Bananas"` | Error with valid types | Returns ALL elements (fallback) | DOCUMENTED |
| Valid type, no elements | `"Wall"` (none exist) | Empty array | Empty array | PASS |
| Valid type, exists | `"Column"` | 4 elements | 4 elements | PASS |
| Case sensitivity | `"column"` lowercase | 4 columns | 28 elements (fallback) | DOCUMENTED |
| Property search | `"wall length"` | Relevant results | No longer polluted by schema keywords | PARTIAL |
| Surface change workflow | "change surface" | Clear instructions | Tapir capability gap | BLOCKED |
| GDL params response size | 1 object | Manageable | 92K characters | FAIL |
| Filter syntax | `["IsEditable"]` | Documented | Documented in docstring | FIXED |
| Property discovery | `get_properties(search="length", group="Wall")` | Relevant results | 15 wall length properties with GUIDs | FIXED |
| Property by group | `get_properties(group="Slab")` | Slab properties | 40 slab properties | FIXED |

### Issue: Tapir Invalid Element Type Behavior
- Invalid element types do NOT error
- Instead, they silently return ALL elements
- AI cannot distinguish "valid type with 0 results" from "invalid type"
- **Root cause**: Tapir add-on behavior, not MCP
- **Potential fix**: Validate elementType at MCP layer before sending to Tapir

### Issue: Case Sensitivity Not Documented
```
"Column" → 4 elements (correct)
"column" → 28 elements (fallback - ALL elements)
"COLUMN" → 28 elements (fallback)
```
- Element types are case-sensitive but this isn't documented
- Wrong case silently falls back to all elements

### Issue: $ref Types Not Resolved in get_docs
- Schema uses `$ref` like `{"$ref": "#/ElementType"}`
- `get_docs` shows the reference but NOT the actual enum values
- AI cannot discover valid element types or filter values
- **Status**: FIXED. `get_command()` now resolves $refs to show actual enum values inline. Large enums (>12 values) are truncated with "(+N more)" indicator to keep responses compact.

### Issue: Filter Syntax Undiscoverable
- `filters` parameter documented as `array of ElementFilter`
- But ElementFilter is just an enum of strings (IsEditable, IsVisibleByLayer, etc.)
- AI guesses syntax like `{"filterType": "layer", "layerName": "X"}` - WRONG
- Correct syntax: `["IsEditable", "OnActualFloor"]` (just string array)
- **Status**: FIXED. Added ELEMENT FILTERS section to execute_script docstring with example syntax and valid values.

---

## Proposed Solutions (Prioritized)

### P0 - Critical (AI can't work around these)

1. ~~**Resolve $ref types in get_docs output**~~ **FIXED**
   - `get_command()` resolves $refs inline, truncates large enums to 12 values + "(+N more)"

2. ~~**Validate element types at MCP layer**~~ **DOCUMENTED**
   - Decided against validation (violates thin-wrapper philosophy)
   - Added clear warnings in docstrings about case-sensitivity

3. ~~**Add case normalization or clear error**~~ **DOCUMENTED**
   - Added warnings to `execute_command` and `execute_script` docstrings

### P1 - High (Major friction for common tasks)

4. **Add workflow recipes to get_docs**
   - "How to get wall areas" → property ID + GetPropertyValuesOfElements call
   - "How to change surface" → actual workflow
   - "How to filter by layer" → correct filter syntax

5. ~~**Add examples to filter documentation**~~ **FIXED**
   - Added ELEMENT FILTERS section to execute_script docstring with syntax example and valid values

6. **Truncate/paginate large responses**
   - GDL params: return first N, add pagination
   - Property lists: group by category, allow filtering

### P2 - Medium (Improves efficiency)

7. ~~**Property aliases**~~ **PARTIALLY FIXED**
   - `get_properties(search="area", group="Wall")` enables discovery
   - No hardcoded aliases, but search + filter is sufficient

8. ~~**Element-type-aware property queries**~~ **FIXED**
   - `get_properties(group="Slab")` → only slab-applicable properties
   - Groups map to element types: Wall, Column, Beam, Slab, Zone, etc.

9. **Intent-based search in get_docs**
   - "wall length" → returns geometry detail OR property info, not unrelated commands

### P3 - Nice to have

10. **"Did you mean?" suggestions on errors**
11. **Auto-detect empty project and hint to user**
12. **Common task templates (schedule export, bulk update, etc.)**
