# Tapir Documentation: Add Unit Specifications

**Status**: Verified (tested against Archicad 29)

**Target Repository**: https://github.com/ENZYME-APD/tapir-archicad-automation

## Summary

Tapir coordinate and dimension parameters use **meters** internally, but most fields lack unit documentation. Some angle fields correctly specify "in radians" - we need to apply the same pattern to length/distance fields.

## Documentation Architecture

1. **Source**: `archicad-addon/Sources/RFIX/Images/CommonSchemaDefinitions.json`
2. **Generation**: `GenerateDocumentation` Tapir command generates JS files
3. **Output**: `docs/archicad-addon/common_schema_definitions.js`
4. **Website**: `docs/archicad-addon/index.html` renders API docs

## Current State

### Already Documented (Good Examples)
These fields correctly specify units - follow this pattern:

| Field | Description |
|-------|-------------|
| `rotationAngle` | "Rotation angle in radians." |
| `rotation` | "The orientation in radian." |
| `slantAngle` | "The slant angle of the beam in radians." |
| `arcAngle` | "The arc angle... in radians." |
| `gridAngle` | "The angle of the grid in radians." |
| `altitude` | "altitude in meters" |

### Needs Unit Documentation

#### Schema Types (cascades to all usages)

| Type | Fields | Unit |
|------|--------|------|
| `Coordinate2D` | x, y | meters |
| `Coordinate3D` | x, y, z | meters |
| `Dimensions3D` | x, y, z | meters |
| `BoundingBox3D` | xMin, yMin, zMin, xMax, yMax, zMax | meters |

#### WallDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `zCoordinate` | (none) | meters |
| `height` | "height relative to bottom" | meters |
| `bottomOffset` | "base level of the wall relative to the floor level" | meters |
| `offset` | "wall's base line's offset from ref. line" | meters |
| `begThickness` | "Thickness at the beginning in case of trapezoid wall" | meters |
| `endThickness` | "Thickness at the end in case of trapezoid wall" | meters |

#### BeamDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `zCoordinate` | (none) | meters |
| `level` | "base height of the beam relative to the floor level" | meters |
| `offset` | "beam ref.line offset from the center" | meters |
| `verticalCurveHeight` | "The height of the vertical curve of the beam." | meters |

#### SlabDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `thickness` | "Thickness of the slab." | meters |
| `level` | "Distance of the reference level of the slab from the floor level." | meters |
| `offsetFromTop` | "Vertical distance between the reference level and the top of the slab." | meters |
| `zCoordinate` | (none) | meters |

#### ColumnDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `zCoordinate` | (none) | meters |
| `height` | "height relative to bottom" | meters |
| `bottomOffset` | "base level of the column relative to the floor level" | meters |

#### MeshDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `level` | "The Z reference level of coordinates." | meters |
| `skirtLevel` | "The height of the skirt." | meters |

#### CurtainWallDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `height` | (none) | meters |

#### CurtainWallPanelDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `d` | "Depth of the panel connection hole." | meters |
| `w` | "Width of the panel connection hole." | meters |

#### CurtainWallFrameContour

| Field | Current Description | Unit |
|-------|---------------------|------|
| `a1` | "Width1 of the frame contour." | meters |
| `a2` | "Width2 of the frame contour." | meters |
| `b1` | "Length1 of the frame contour." | meters |
| `b2` | "Length2 of the frame contour." | meters |

#### Story

| Field | Current Description | Unit |
|-------|---------------------|------|
| `level` | "The story level." | meters |

#### PolylineDetails / ZoneDetails

| Field | Current Description | Unit |
|-------|---------------------|------|
| `zCoordinate` | (none) | meters |

#### LayoutInfo (not "LayoutDetails")

| Field | Current Description | Unit |
|-------|---------------------|------|
| `width` | (none) | **millimeters** |
| `height` | (none) | **millimeters** |

**Note:** Layout dimensions use millimeters (paper units), not meters. Verified: A4=210x297mm, A3=420x297mm.

#### ZoneBoundary

| Field | Current Description | Unit |
|-------|---------------------|------|
| `area` | "The area of the polygon of the boundary." | square meters |

#### Texture

| Field | Current Description | Unit |
|-------|---------------------|------|
| `xSize` | "X size of the picture in model space, by default 1." | meters |
| `ySize` | "Y size of the picture in model space, by default 1." | meters |

### No Changes Needed (Unitless)

| Field | Description | Why |
|-------|-------------|-----|
| `red`, `green`, `blue` | "value between 0.0 and 1.0" | ratio |
| `segmentIndex` | "index of the curtain wall segment" | index |
| `value` | generic numeric | context-dependent |

## Proposed Changes

### Fields with Existing Descriptions
**Pattern**: Append "in meters" or "in square meters" to existing descriptions.

### Fields Missing Descriptions Entirely (9 fields)
These fields have `(none)` above - they need complete descriptions, not just unit suffixes.

Verified line numbers in `CommonSchemaDefinitions.json`:

| Location | Field | Line | Proposed Description |
|----------|-------|------|---------------------|
| WallDetails | `zCoordinate` | 2218 | "The Z coordinate of the wall in meters." |
| BeamDetails | `zCoordinate` | 2280 | "The Z coordinate of the beam in meters." |
| SlabDetails | `zCoordinate` | 2331 | "The Z coordinate of the slab in meters." |
| ColumnDetails | `zCoordinate` | 2368 | "The Z coordinate of the column in meters." |
| PolylineDetails | `zCoordinate` | 2514 | "The Z coordinate of the polyline in meters." |
| ZoneDetails | `zCoordinate` | 2565 | "The Z coordinate of the zone in meters." |
| CurtainWallDetails | `height` | 2583 | "The height of the curtain wall in meters." |
| LayoutInfo | `width` | 3061 | "The width of the layout in millimeters." |
| LayoutInfo | `height` | 3064 | "The height of the layout in millimeters." |

Style notes (matching existing patterns):
- "The Z coordinate of..." matches "Z value of the coordinate." (Coordinate3D)
- "The height of..." matches "The height of the skirt." (MeshDetails)
- "The width of..." matches "Width of the panel connection hole." (CurtainWallFrameDetails)

**Approach**: Include these in the same PR - all documentation changes together.

### File: `CommonSchemaDefinitions.json`

**Pattern**: Add "in meters" or "in square meters" to descriptions.

Example changes:

```json
// Coordinate2D (line ~429)
"description": "2D coordinate in meters."
"x": { "description": "X value of the coordinate in meters." }
"y": { "description": "Y value of the coordinate in meters." }

// WallDetails.height (line ~2221)
"description": "Height relative to bottom in meters."

// SlabDetails.thickness (line ~2319)
"description": "Thickness of the slab in meters."

// ZoneBoundary.area (line ~3497)
"description": "The area of the polygon of the boundary in square meters."
```

## Summary of Changes

| Category | Count | Unit |
|----------|-------|------|
| Schema types (Coordinate2D, 3D, Dimensions3D, BoundingBox3D) | 4 types, ~18 fields | meters |
| Element details (Wall, Beam, Slab, Column, Mesh, CurtainWall) | ~25 fields | meters |
| Other (Story, Zone, Texture) | ~6 fields | meters |
| LayoutInfo (width, height) | 2 fields | **millimeters** |
| ZoneBoundary.area | 1 field | square meters |
| Fields needing new descriptions | 9 fields | (see above) |
| **Total fields** | **~51 fields** | |

## Files to Modify

1. `archicad-addon/Sources/RFIX/Images/CommonSchemaDefinitions.json` (source)
2. `docs/archicad-addon/common_schema_definitions.js` (regenerate or sync)

## Verification Summary

### Empirical Testing (Archicad 29 + Tapir API)

| Test | Value | Unit Confirmed |
|------|-------|----------------|
| Slab thickness | 0.3 | meters (30cm) |
| Slab polygon | 8x6 | meters |
| Wall height | 3 | meters |
| Column height | 2.8 | meters |
| Story levels | 0, 3, 6 | meters (3m stories) |
| Zone area | 25 (5x5m zone) | square meters |
| A4 layout | 210x297 | millimeters |
| A3 layout | 420x297 | millimeters |
| A2 layout | 594x420 | millimeters |
| BoundingBox3D | zMin=-0.2, zMax=0.1 | meters |
| CurtainWall height | 3 | meters |
| CurtainWall frame d | 0.03 | meters (3cm) |
| CurtainWall frame w | 0.02 | meters (2cm) |
| CurtainWall frame a1/a2 | 0.05 | meters (5cm) |
| CurtainWall frame b1 | 0.05 | meters (5cm) |
| CurtainWall frame b2 | 0.25 | meters (25cm) |

### Authoritative Source (Archicad C++ API DevKit)

From `API_Texture` struct in `APIdefs_Attributes.h`:
- `xSize` - "X size of the picture in **model space**."
- `ySize` - "Y size of the picture in **model space**."
- `rotAng` - "Rotation angle in **radians**."

"Model space" = meters in Archicad.

### Potential Separate Issue

CurtainWallDetails.angle returned `90` which appears to be **degrees**, not radians (90 radians ≈ 14 full rotations).

- **Tapir schema**: Says "in radians"
- **Official API** (`API_CurtainWallType.angle`): No unit specified - just "Angle of the curtain wall (input only)"
- **Empirical value**: 90 (looks like degrees)

This is a documentation gap - requires separate investigation.

## Complexity

- **Effort**: Low (text changes only, but many fields)
- **Risk**: None (documentation only)
- **Approach**: Single PR with all unit documentation (including new descriptions for fields that currently have none)

## Out of Scope

- **CurtainWallDetails.angle**: May be incorrectly documented as radians (returns 90, not ~1.57). Requires separate investigation.
