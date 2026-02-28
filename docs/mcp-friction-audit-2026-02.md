# MCP Friction Audit - February 2026

Hands-on testing of all 4 MCP tools against a live Archicad 29 instance (Untitled project, 2 elements).

## HIGH Severity

### 1. `get_docs(search="wall")` Returns 0 Command Results

**Observed**: Searching for "wall" returns zero command matches. Only an `element_type_hint` sidebar appears.

**Root cause**: `search.py:146` — `enum` is in `SCHEMA_KEYWORDS`, so enum values like `"Wall"` inside parameter schemas (`elementType` enum) are never indexed. The word "wall" doesn't appear in any command name or description, only in enum values.

**Impact**: An AI asking "how do I work with walls?" gets no results. Should surface `GetElementsByType`, `GetDetailsOfElements`, `MoveElements`, `DeleteElements`, etc.

**Repro**:
```
get_docs(search="wall")
→ {"total": 0, "results": [], "element_type_hint": {...}}
```

**Fixed**: Index enum values during search index build. `search.py` indexes `enum` lists with `WEIGHT_ENUM=20` and resolves `$ref` targets via ref schema lookup (no recursive expansion — avoids circular ref issues with Hotlink/Hotlinks). `cache.py` passes merged ref schemas to the index builder. Also removed `_truncate_enum` / `MAX_ENUM_VALUES=12` which was hiding 84% of valid element types from `get_docs(command=...)` responses. Result: `search("wall")` → 4 commands (GetElementsByType, GetConnectedElements, GetFavoritesByType, API.GetElementsByType).

---

### 2. Multi-Word Search Ranking Broken

**Observed**: `get_docs(search="delete element")` ranks **GetSubelementsOfHierarchicalElements** (score 170) above **DeleteElements** (score 105).

**Root cause**: Scoring is purely additive per-token. The token "element" prefix-matches "elements" across many fields in the Subelements command (description, parameters, returns), accumulating points. No bonus for multi-token co-occurrence. No penalty for missing tokens ("delete" doesn't appear in GetSubelementsOfHierarchicalElements at all).

**Impact**: Multi-word searches return unintuitive rankings. The more specific the query, the worse the results.

**Repro**:
```
get_docs(search="delete element")
→ #1: GetSubelementsOfHierarchicalElements (170)
→ #3: DeleteElements (105)
```

**Fixed**: Coverage multiplier in `search.py`. Scoring functions (`_score_exact_and_prefix`, `_score_fuzzy`) now track which query tokens matched each command. For multi-token queries, each command's score is multiplied by `matched_tokens / total_tokens`. Single-token queries unaffected. Result: `search("delete element")` → #1 DeleteElements (105), GetSubelementsOfHierarchicalElements drops to 85.

---

## MEDIUM Severity

### 3. Archicad Command Errors Arrive as `success: true`

**Observed**: Passing invalid parameters to an Archicad command through `execute_script` returns `success: true` with the error buried in the result dict.

**Repro**:
```python
# In execute_script:
result = await archicad.tapir("GetElementsByType", {"elementType": "wall"})
# Returns: {"success": true, "result": {"error": {"code": -2130313112, "message": "Invalid elementType 'wall'."}}}
```

**Root cause**: Tapir's protocol defines two error response shapes: `ErrorItem` (`{"error": {...}}`, no `success` field) for top-level command errors, and `FailedExecutionResult` (`{"success": false, "error": {...}}`) for per-item batch failures. `_execute_tapir` in `connection.py` only checked `success: false`, missing the `ErrorItem` shape entirely — and that's what most commands return for validation errors (e.g., invalid `elementType`).

**Impact**: AI continues processing as if it got valid data. The error field is easy to miss in a large response.

**Fixed**: `connection.py` now checks for the `error` field directly in the Tapir response, catching both `ErrorItem` and `FailedExecutionResult` shapes. The `success` field check was redundant since `FailedExecutionResult` always includes `error`. Result: invalid parameters now raise `CommandError` with the Tapir error message and code, surfaced as `ScriptResult(success=False, error="...")`.

---

### 4. Duplicate Tapir vs Built-in Commands, No Guidance

**Observed**: Multiple commands exist in both APIs:
- `GetElementsByType` (Tapir) vs `API.GetElementsByType` (built-in)
- `GetPropertyValuesOfElements` (Tapir) vs `API.GetPropertyValuesOfElements` (built-in)
- `Get3DBoundingBoxes` (Tapir) vs `API.Get3DBoundingBoxes` (built-in)

The `execute_script` description says:
```
await archicad.tapir(name, params)    # Tapir commands
await archicad.command(name, params)  # Built-in API (prefix with "API.")
```

But never explains when to prefer one over the other.

**Impact**: AI may use the wrong variant. Tapir versions generally have richer parameters (filters, databases), while built-in versions may have different return structures.

**Status**: Left as-is. The fix for #5 (built-in signatures now visible in `execute_script` docs) largely mitigates this — the AI sees both variants side by side and can choose based on the signatures. Adding "prefer Tapir" guidance would be misleading since it's not universally true and Tapir isn't always installed.

---

### 5. `execute_script` Docs Only Show Tapir Commands

**Observed**: The `execute_script` tool description embeds compact signatures for all 98 Tapir commands but none of the 73 built-in API commands. Built-in commands are referenced as "MORE COMMANDS" with a tip to use `get_docs`.

**Root cause**: `docgen.py:221-225` filters `if cmd_data.get("api") != "tapir": continue`.

**Impact**: An AI needing `API.GetAllPropertyIds`, `API.GetElementsByClassification`, or `API.Get2DBoundingBoxes` won't see them without an extra `get_docs` round-trip.

**Fix options**:
- Include built-in commands in the docstring (adds ~73 signatures, roughly 2-3K tokens)
- Or add a "BUILT-IN API COMMANDS" summary section with just the names grouped by category

---

### 6. Property Values Are Locale-Formatted Strings

**Observed**: `GetPropertyValuesOfElements` returns numeric values as locale-formatted strings.

**Repro**:
```python
# Query the "Area" property
result = await archicad.tapir("GetPropertyValuesOfElements", {
    "elements": elements[:3],
    "properties": [{"propertyId": {"guid": "AC5CCA52-F79B-4850-92A9-BED7CB7C3847"}}]
})
# Returns: {"value": "1,00"}  ← comma decimal, not 1.0
```

**Impact**: Any script doing arithmetic (`float(value)`) will crash on locales using comma decimals. The AI must know to replace commas: `float(value.replace(",", "."))`.

**Fixed**: Added locale warning to `execute_script` COMMON PATTERNS in `docgen.py`. The note shows the inline pattern `float(value.replace(",", "."))`. This is an upstream Archicad behavior (locale-formatted display strings) that we can't fix at the source — documentation is the correct mitigation.

---

## LOW Severity

### 7. `import` Statements Fail in Sandboxed Mode

**Observed**: Writing `import json` in a script fails because `__import__` isn't in `SCRIPT_BUILTINS`. The module IS available — it's pre-injected into the namespace.

**Root cause**: `executor.py:291-293` — sandboxed mode uses restricted builtins without `__import__`. Modules are injected at `executor.py:284-285` via `**ALLOWED_MODULES`.

**Impact**: The `execute_script` description says "AVAILABLE MODULES: json, csv, math..." which reads as "you can import these", not "these are already imported". An AI writing `import json` gets a confusing error.

**Fixed**: Both fixes applied. Added a restricted `_safe_import` to `SCRIPT_BUILTINS` in `executor.py` — `import json` now works in sandboxed mode, while `import os` raises `ImportError` with the list of available modules. Also clarified the docstring label to "AVAILABLE MODULES (pre-imported, `import x` also works)" in `docgen.py`.

---

### 8. `get_docs(command="NonExistentCommand")` Gives Generic Suggestion

**Observed**: Querying a nonexistent command returns `"suggestion": "Use get_docs() to browse commands"` with no fuzzy alternatives.

**Root cause**: `server.py:185-186` tries `schemas.search(command)`, but for completely unrelated names the fuzzy search finds nothing above threshold.

**Repro**:
```
get_docs(command="NonExistentCommand")
→ {"error": "Command 'NonExistentCommand' not found", "suggestion": "Use get_docs() to browse commands"}
```

For slightly wrong names it works better:
```
get_docs(command="GetElementByType")  ← missing 's'
→ Would fuzzy-match to GetElementsByType
```

**Fixed**: Replaced the token-based search fallback with direct fuzzy matching against command names via `SchemaCache.find_similar_commands()` in `cache.py`. Uses `rapidfuzz.fuzz.ratio` with a 40% threshold — always returns top 3 closest command names. `server.py` now calls this instead of `schemas.search()` for command-not-found. Result: `get_docs(command="NonExistentCommand")` → `Similar: ['API.ExecuteAddOnCommand', 'CreateColumns', ...]`.

---

## What Worked Well

For reference, these aspects had zero friction:

- **`list_instances`** — instant, clear, gives port number needed for other tools
- **`execute_script`** — the async `await archicad.tapir()` / `archicad.command()` pattern is clean and intuitive
- **Error formatting** — script errors include line numbers and the offending line of code
- **`get_properties` search** — `search="area"` with `group="Wall"` returned exactly what's needed
- **`get_properties` typo correction** — `group="Walll"` returned `"Did you mean: ['Wall']?"`
- **Result truncation** — large lists auto-truncated with clear warning
- **`get_docs` category browsing** — `get_docs()` → `get_docs(category="...")` → `get_docs(command="...")` is a natural drill-down
- **Compact Tapir signatures in execute_script** — seeing all 98 command signatures inline eliminates most `get_docs` lookups
- **Timeout support** — `timeout_seconds` parameter on execute_script is a good safety net
- **stdout capture** — `print()` output returned alongside result, useful for debugging

---

## Test Matrix

| Test | Tool | Input | Result | Status |
|------|------|-------|--------|--------|
| List instances | `list_instances` | — | Found 1 instance on port 19723 | PASS |
| Get docs overview | `get_docs()` | — | 171 commands, 23 categories | PASS |
| Search "create" | `get_docs` | `search="create"` | 21 results, good ranking | PASS |
| Search "wall" | `get_docs` | `search="wall"` | 0 results (only hint) | FAIL |
| Search "delete element" | `get_docs` | `search="delete element"` | Wrong #1 ranking | FAIL |
| Search "move" | `get_docs` | `search="move"` | 3 results, correct | PASS |
| Search "property" | `get_docs` | `search="property"` | 21 results, good | PASS |
| Command lookup | `get_docs` | `command="GetElementsByType"` | Full schema | PASS |
| Bad command | `get_docs` | `command="NonExistentCommand"` | Generic suggestion | MINOR |
| Properties overview | `get_properties` | `port=19723` | 1802 props, 93 groups | PASS |
| Property search | `get_properties` | `search="area"` | 268 results with GUIDs | PASS |
| Property by group | `get_properties` | `group="Wall"` | 140 wall properties | PASS |
| Property typo | `get_properties` | `group="Walll"` | "Did you mean: ['Wall']?" | PASS |
| Script: get walls | `execute_script` | `GetElementsByType(Wall)` | 0 walls (empty project) | PASS |
| Script: get all | `execute_script` | `GetAllElements` | 2 elements | PASS |
| Script: project info | `execute_script` | `GetProjectInfo` | Name, path, type | PASS |
| Script: stories | `execute_script` | `GetStories` | 3 stories | PASS |
| Script: property values | `execute_script` | `GetPropertyValuesOfElements` | Values returned (locale fmt) | PASS* |
| Script: wrong type | `execute_script` | `elementType: "wall"` | CommandError raised | FIXED |
| Script: bad command | `execute_script` | `NonExistentCommand` | Clear CommandError | PASS |
| Script: syntax error | `execute_script` | `x = 1 +` | "Syntax error at line 6" | PASS |
| Script: runtime error | `execute_script` | `1 / 0` | "Line 3: ZeroDivisionError" | PASS |
| Script: print + result | `execute_script` | `print(); result=` | Both stdout and result | PASS |
| Script: no result | `execute_script` | `x = 42` | `result: null` | PASS |
| Wrong port | `execute_script` | `port=99999` | "No Archicad instance on port 99999" | PASS |
| Wrong port | `get_properties` | `port=99999` | Same error | PASS |
