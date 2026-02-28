# Tapir Bug: CreateSlabs Level Parameter Incorrect at Story Boundaries

**Status**: PR Submitted - [#348](https://github.com/ENZYME-APD/tapir-archicad-automation/pull/348)

**Target Repository**: https://github.com/ENZYME-APD/tapir-archicad-automation

## Summary

`CreateSlabs` interprets the `level` parameter as an absolute Z coordinate but fails to convert it to a story-relative offset after assigning the slab to a story. This causes slabs placed at or above story boundaries to appear at incorrect (doubled) Z positions.

## Reproduction

### Setup
Story structure with 3m floor heights:
- Story 0: level = 0m
- Story 1: level = 3m
- Story 2: level = 6m

### Test Cases

```python
# Case 1: level below story boundary
CreateSlabs(slabsData=[{
    "level": 2.9,
    "polygonCoordinates": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]
}])

# Case 2: level equals story boundary
CreateSlabs(slabsData=[{
    "level": 3.0,
    "polygonCoordinates": [{"x": 2, "y": 0}, {"x": 3, "y": 0}, {"x": 3, "y": 1}, {"x": 2, "y": 1}]
}])

# Case 3: level above story boundary
CreateSlabs(slabsData=[{
    "level": 3.1,
    "polygonCoordinates": [{"x": 4, "y": 0}, {"x": 5, "y": 0}, {"x": 5, "y": 1}, {"x": 4, "y": 1}]
}])
```

### Results

| Input Level | Assigned Story | Expected Z | Actual Z | Status |
|-------------|----------------|------------|----------|--------|
| 2.9 | Story 0 (0m) | 2.9m | ~2.9m | Correct |
| 3.0 | Story 1 (3m) | 3.0m | **~5.9m** | Bug |
| 3.1 | Story 1 (3m) | 3.1m | **~6.0m** | Bug |

*Tested on Archicad 29 with default slab thickness (~0.2m). Z values are slab bottom.*

## Root Cause

**File**: `archicad-addon/Sources/ElementCreationCommands.cpp`, lines 282-286

The `CreateSlabsCommand::SetTypeSpecificParameters` function:
1. Reads the input `level` as an absolute Z coordinate
2. Calls `GetFloorIndexAndOffset()` to determine the correct story and offset
3. Sets `element.header.floorInd` to the story index
4. **Bug**: Leaves `element.slab.level` as the original absolute value instead of the computed offset

The Archicad API interprets `slab.level` as **relative to the assigned story**, so the final Z becomes:
```
actual_z = story_level + slab.level
         = 3.0 + 3.0   // for level=3.0 case
         = 6.0         // wrong!
```

### Current Code (lines 282-286)

```cpp
GS::Optional<GS::ObjectState> CreateSlabsCommand::SetTypeSpecificParameters (API_Element& element, API_ElementMemo& memo, const Stories& stories, const GS::ObjectState& parameters) const
{
    parameters.Get ("level", element.slab.level);                                   // line 284
    const auto floorIndexAndOffset = GetFloorIndexAndOffset (element.slab.level, stories);  // line 285
    element.header.floorInd = floorIndexAndOffset.first;                            // line 286
    // BUG: element.slab.level is NOT updated to floorIndexAndOffset.second!
```

### Correct Pattern (from CreateColumnsCommand, lines 162-164)

```cpp
const auto floorIndexAndOffset = GetFloorIndexAndOffset (apiCoordinate.z, stories);
element.header.floorInd = floorIndexAndOffset.first;
element.column.bottomOffset = floorIndexAndOffset.second;  // Uses computed offset
```

### Correct Pattern (from CreateObjectsCommand, lines 738-740)

```cpp
const auto floorIndexAndOffset = GetFloorIndexAndOffset (apiCoordinate.z, stories);
element.header.floorInd = floorIndexAndOffset.first;
element.object.level = floorIndexAndOffset.second;  // Uses computed offset
```

## Proposed Fix

Replace lines 284-286:

```cpp
// BEFORE (buggy)
parameters.Get ("level", element.slab.level);
const auto floorIndexAndOffset = GetFloorIndexAndOffset (element.slab.level, stories);
element.header.floorInd = floorIndexAndOffset.first;

// AFTER (fixed - matches Columns/Objects pattern)
double inputLevel = 0.0;
parameters.Get ("level", inputLevel);
const auto floorIndexAndOffset = GetFloorIndexAndOffset (inputLevel, stories);
element.header.floorInd = floorIndexAndOffset.first;
element.slab.level = floorIndexAndOffset.second;
```

**Why this approach**: Follows the established pattern used by `CreateColumnsCommand` (line 164) and `CreateObjectsCommand` (line 740) - read input into separate variable, compute floor index and offset, then write both outputs to element struct.

## Verification

After the fix, the test cases should produce:

| Input Level | Assigned Story | Stored Offset | Actual Z | Status |
|-------------|----------------|---------------|----------|--------|
| 2.9 | Story 0 (0m) | 2.9 | 2.9m | Correct |
| 3.0 | Story 1 (3m) | 0.0 | 3.0m | Correct |
| 3.1 | Story 1 (3m) | 0.1 | 3.1m | Correct |

## Affected Commands

- **CreateSlabs** - confirmed bug
- **CreateMeshes** - likely same issue (uses similar pattern)
- **CreateZones** - needs verification

## Workaround

Until fixed, use levels slightly below story boundaries:
```python
# To place slab at z=3m on Story 1, use level just below 3m
CreateSlabs(slabsData=[{"level": 2.999, ...}])  # Stays on Story 0, placed at 2.999m
```

This keeps the slab on the lower story where the absolute level works correctly.
