# Surface Assignment - Technical Specification

## Summary

Add surface (material) assignment capability to `SetDetailsOfElements` and surface reading to `GetDetailsOfElements` for Walls and Slabs. Column/Beam segments require a different approach due to hierarchical element structure.

## API Fields by Element Type

### Wall (API_WallType) - Direct element fields
| Field | Type | Description |
|-------|------|-------------|
| `refMat` | `API_OverriddenAttribute` | Reference line side surface |
| `oppMat` | `API_OverriddenAttribute` | Opposite side surface |
| `sidMat` | `API_OverriddenAttribute` | Edge surface |

### Slab (API_SlabType) - Direct element fields
| Field | Type | Description |
|-------|------|-------------|
| `topMat` | `API_OverriddenAttribute` | Top surface |
| `botMat` | `API_OverriddenAttribute` | Bottom surface |
| `sideMat` | `API_OverriddenAttribute` | Edge surface |

### Column Segment (API_ColumnSegmentType) - Subelement
| Field | Type | Description |
|-------|------|-------------|
| `extrusionSurfaceMaterial` | `API_OverriddenAttribute` | Main extrusion surface |
| `endsMaterial` | `API_OverriddenAttribute` | End cap surfaces |
| `materialsChained` | `bool` | Are surfaces linked |

### Beam Segment (API_BeamSegmentType) - Subelement
| Field | Type | Description |
|-------|------|-------------|
| `topMaterial` | `API_OverriddenAttribute` | Top surface |
| `bottomMaterial` | `API_OverriddenAttribute` | Bottom surface |
| `leftMaterial` | `API_OverriddenAttribute` | Left surface |
| `rightMaterial` | `API_OverriddenAttribute` | Right surface |
| `endsMaterial` | `API_OverriddenAttribute` | End cap surfaces |
| `extrusionMaterial` | `API_OverriddenAttribute` | Extrusion surface |

## Complexity Analysis

### Phase 1: Walls and Slabs (Simple)
- Surface fields are directly on the element struct
- Can use existing `ACAPI_Element_Change` with mask
- Pattern matches existing geometry fields (height, offset, etc.)

### Phase 2: Columns and Beams (Complex)
- Surfaces are on **segment subelements**, not the parent element
- Columns/Beams can have multiple segments
- Options:
  1. Support `SetDetailsOfElements` on segment element GUIDs directly
  2. Add a convenience command that sets all segments at once
  3. Require users to use `GetSubelementsOfHierarchicalElements` first

**Recommendation:** Phase 1 only for initial PR. Column/Beam support as follow-up.

## Code Changes

### 1. GetDetailsOfElements - Read surfaces (ElementCommands.cpp ~line 404)

```cpp
case API_WallID:
    // ... existing geometry code ...

    // Surface materials
    if (elem.wall.refMat.IsOverridden) {
        typeSpecificDetails.Add("refMat", GetAttributeIndex(elem.wall.refMat.Value));
    }
    if (elem.wall.oppMat.IsOverridden) {
        typeSpecificDetails.Add("oppMat", GetAttributeIndex(elem.wall.oppMat.Value));
    }
    if (elem.wall.sidMat.IsOverridden) {
        typeSpecificDetails.Add("sidMat", GetAttributeIndex(elem.wall.sidMat.Value));
    }
    break;

case API_SlabID:
    // ... existing geometry code ...

    // Surface materials
    if (elem.slab.topMat.IsOverridden) {
        typeSpecificDetails.Add("topMat", GetAttributeIndex(elem.slab.topMat.Value));
    }
    if (elem.slab.botMat.IsOverridden) {
        typeSpecificDetails.Add("botMat", GetAttributeIndex(elem.slab.botMat.Value));
    }
    if (elem.slab.sideMat.IsOverridden) {
        typeSpecificDetails.Add("sideMat", GetAttributeIndex(elem.slab.sideMat.Value));
    }
    break;
```

### 2. SetDetailsOfElements - Write surfaces (ElementCommands.cpp ~line 822)

```cpp
case API_WallID: {
    // ... existing geometry code ...

    // Surface materials
    Int32 refMatIndex;
    if (typeSpecificDetails->Get("refMat", refMatIndex)) {
        elem.wall.refMat.IsOverridden = true;
        elem.wall.refMat.Value = ACAPI_CreateAttributeIndex(refMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_WallType, refMat);
    }

    Int32 oppMatIndex;
    if (typeSpecificDetails->Get("oppMat", oppMatIndex)) {
        elem.wall.oppMat.IsOverridden = true;
        elem.wall.oppMat.Value = ACAPI_CreateAttributeIndex(oppMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_WallType, oppMat);
    }

    Int32 sidMatIndex;
    if (typeSpecificDetails->Get("sidMat", sidMatIndex)) {
        elem.wall.sidMat.IsOverridden = true;
        elem.wall.sidMat.Value = ACAPI_CreateAttributeIndex(sidMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_WallType, sidMat);
    }
} break;

case API_SlabID: {
    Int32 topMatIndex;
    if (typeSpecificDetails->Get("topMat", topMatIndex)) {
        elem.slab.topMat.IsOverridden = true;
        elem.slab.topMat.Value = ACAPI_CreateAttributeIndex(topMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_SlabType, topMat);
    }

    Int32 botMatIndex;
    if (typeSpecificDetails->Get("botMat", botMatIndex)) {
        elem.slab.botMat.IsOverridden = true;
        elem.slab.botMat.Value = ACAPI_CreateAttributeIndex(botMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_SlabType, botMat);
    }

    Int32 sideMatIndex;
    if (typeSpecificDetails->Get("sideMat", sideMatIndex)) {
        elem.slab.sideMat.IsOverridden = true;
        elem.slab.sideMat.Value = ACAPI_CreateAttributeIndex(sideMatIndex);
        ACAPI_ELEMENT_MASK_SET(mask, API_SlabType, sideMat);
    }
} break;
```

### 3. Schema Updates (CommonSchemaDefinitions.json)

**WallDetails** (~line 2201) - add after existing fields:
```json
"refMat": {
    "type": "integer",
    "description": "Surface index for reference line side (if overridden)"
},
"oppMat": {
    "type": "integer",
    "description": "Surface index for opposite side (if overridden)"
},
"sidMat": {
    "type": "integer",
    "description": "Surface index for edges (if overridden)"
}
```

**WallSettings** (~line 3299) - add same fields

**New: SlabSettings** - add to TypeSpecificSettings oneOf:
```json
"SlabSettings": {
    "type": "object",
    "description": "Settings for modifying a slab.",
    "properties": {
        "topMat": {
            "type": "integer",
            "description": "Surface index for top face"
        },
        "botMat": {
            "type": "integer",
            "description": "Surface index for bottom face"
        },
        "sideMat": {
            "type": "integer",
            "description": "Surface index for edges"
        }
    },
    "additionalProperties": false,
    "required": []
}
```

**SlabDetails** - add same fields to existing schema

## Testing Considerations

### Structure Types
- `API_BasicStructure` - surfaces apply directly to element
- `API_CompositeStructure` - surfaces may be inherited from composite or overridden

### Test Cases
1. Set surface on Basic structure wall - verify change
2. Set surface on Composite structure wall - verify override works
3. Read surfaces back via GetDetailsOfElements
4. Set only one surface (e.g., refMat) - verify others unchanged
5. Invalid surface index - verify error handling

### Edge Cases
- Element with no surface override (IsOverridden = false)
- Setting surface to match composite default
- Profiled walls (may behave differently)

## Usage Example

```python
# 1. Get surface attribute index
surfaces = await archicad.tapir("GetAttributesByType", {"attributeType": "Surface"})
brick = next(s for s in surfaces["attributes"] if "Brick" in s["name"])

# 2. Get walls to modify
walls = await archicad.tapir("GetElementsByType", {"elementType": "Wall"})

# 3. Set surface
await archicad.tapir("SetDetailsOfElements", {
    "elementsWithDetails": [{
        "elementId": walls["elements"][0]["elementId"],
        "details": {
            "typeSpecificDetails": {
                "refMat": brick["index"],
                "oppMat": brick["index"]
            }
        }
    }]
})
```

## Files Changed

| File | Changes |
|------|---------|
| `ElementCommands.cpp` | Add surface fields to Get/SetDetailsOfElements |
| `CommonSchemaDefinitions.json` | Add surface fields to WallDetails, WallSettings, SlabDetails, SlabSettings |

## Open Questions for Maintainers

1. **Scope:** Should Phase 1 include Slabs, or just Walls?
2. **Column/Beam:** Preferred approach for hierarchical elements?
3. **Clear override:** Should we support setting `IsOverridden = false` to revert to default?
4. **Chained materials:** Should we expose `materialsChained` flag?
