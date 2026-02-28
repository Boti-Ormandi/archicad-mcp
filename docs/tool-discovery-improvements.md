# Tool Discovery Improvements

## Problem

Tools have weak browse/discovery. The AI can search if it knows what to look for,
but can't efficiently explore what's available.

`get_docs` is missing Level 1 (browse by category). It also has several consistency
gaps compared to `get_properties` in error handling and response format.

## Discovery Levels

Both tools should follow:
- Level 0: Overview (categories/groups with counts)
- Level 1: Browse (list items in a category/group)
- Level 2: Detail (full schema/info for one item)

`get_properties` has all three. `get_docs` is missing Level 1.

## Implementation Plan

### 1. Add `category` parameter to `get_docs`

**File: server.py** — `get_docs` tool

- Add `category: str | None = None` parameter
- Add elif branch: `elif category: return schemas.get_category(category)`
- Priority: `command` > `commands` > `category` > `search` > overview
- Update docstring: add `get_docs(category="Element Commands")` to USAGE and Examples

**File: schemas/cache.py** — `get_summary()`

- Update tip: `"Use get_docs(category='...') to browse commands in a category"`

### 2. Fuzzy matching for bad category names

**File: schemas/cache.py** — `get_category()`

When category matches nothing, suggest similar categories. Mirror the
`find_similar_groups()` pattern from `core/properties.py`:

- Substring match: `query_lower in cat_lower`
- Prefix match: `cat_lower.startswith(query_lower[:3])`
- Fuzzy fallback: `rapidfuzz.fuzz.ratio >= 70` (if available)
- Return max 3 suggestions

Result on bad category:
```python
{
    "category": "Element Commds",
    "total": 0,
    "commands": [],
    "suggestion": "Did you mean: Element Commands?"
}
```

### 3. Similar command suggestions for bad `command` name

**File: server.py** — `get_docs` tool, the `command` not-found branch

Instead of generic "not found", use `schemas.search(command)` to find close matches:
```python
if result is None:
    search_results = schemas.search(command)
    similar = [r["name"] for r in search_results.get("results", [])[:3]]
    response = {
        "error": f"Command '{command}' not found",
        "suggestion": f"Similar: {similar}" if similar else "Use get_docs() to browse commands",
    }
    return response
```

This reuses the existing SearchIndex (which already has fuzzy matching) instead
of building new matching logic.

### 4. Truncation warning in `get_docs` search

**File: schemas/search.py** — `SearchIndex.search()`

The method already receives and applies `limit` but doesn't report truncation.

Add `"showing"` and `"total"` fields to the search response, plus a tip when
truncated:
```python
result = {
    "query": query,
    "total": len(scored),
    "showing": len(results),
    "results": results,
}
if len(scored) > len(results):
    result["tip"] = f"{len(scored)} matches truncated to {len(results)}. Refine your search."
```

Check current `search()` return format and adapt — the fields may already partially
exist. Goal: match `get_properties` pattern of `total` + `showing` + tip on truncation.

### 5. Standardize guidance field names

Across both tools, adopt this convention:
- `"suggestion"` — error recovery ("did you mean?", "not found, try X")
- `"tip"` — operational hints ("results truncated", "use X to drill down")

**Changes needed:**

server.py `get_docs` command-not-found branch:
- Change `"tip"` to `"suggestion"` (this is error recovery, not a hint)

schemas/cache.py `get_summary()`:
- Keep `"tip"` (this is an operational hint — correct already)

schemas/search.py zero-results case:
- If currently uses `"tip"` for "No matches found", change to `"suggestion"`
- Keep `"tip"` for truncation warning

No changes needed in `get_properties` — it already uses the right convention.

### 6. Add query echo-back to `get_docs`

**File: schemas/search.py** — verify `search()` includes `"query"` field (it likely
already does based on the code).

**File: schemas/cache.py** — `get_category()` should include query echo:
```python
return {
    "query": {"category": category},
    "category": category,
    "total": len(matches),
    "commands": matches,
}
```

**File: server.py** — `get_docs` command-not-found should include:
```python
"query": {"command": command},
```

## Files Changed

| File | Changes |
|------|---------|
| `server.py` | Add `category` param, fuzzy command suggestions, update docstring |
| `schemas/cache.py` | Fuzzy category matching in `get_category()`, update summary tip, query echo-back |
| `schemas/search.py` | Truncation warning, verify/fix field naming |

## Not Changing

- `get_properties` — already consistent, no modifications needed
- No new dependencies — rapidfuzz is already a dependency
- No new files
