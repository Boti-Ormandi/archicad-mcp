# AI Friction Points

Observations from Claude using the MCP tools in a real session (Feb 2026).

## 1. Compact Signatures Insufficient for Nested Structures

The `execute_script` docstring includes compact signatures for all Tapir commands:

```
SetPropertyValuesOfElements(elementPropertyValues: [{elementId, propertyId, propertyValue}])
```

This is enough for simple commands, but for nested structures the compact format hides
critical details. When setting property values, I constructed this (wrong):

```python
await archicad.tapir("SetPropertyValuesOfElements", {
    "elementPropertyValues": [{
        "elementId": slab["elementId"],
        "propertyValues": [{                    # WRONG - nested array
            "propertyId": {"guid": "..."},
            "propertyValue": {"value": "6"}
        }]
    }]
})
```

The actual schema has `propertyId` and `propertyValue` as siblings of `elementId`, not
nested under a `propertyValues` array:

```python
await archicad.tapir("SetPropertyValuesOfElements", {
    "elementPropertyValues": [{
        "elementId": slab["elementId"],
        "propertyId": {"guid": "..."},          # CORRECT - flat sibling
        "propertyValue": {"value": "6"}         # CORRECT - flat sibling
    }]
})
```

The compact signature `[{elementId, propertyId, propertyValue}]` does show the flat
structure, but when the command name suggests "property values" (plural), the AI's
training bias toward REST-style nested payloads kicks in.

**Workaround**: Call `get_docs(command="SetPropertyValuesOfElements")` for the full
JSON schema. This works but adds an extra round trip.

**Possible improvement**: Include a one-line example for commands with non-obvious
parameter structures in the compact docs. E.g.:

```
SetPropertyValuesOfElements(elementPropertyValues: [{elementId, propertyId, propertyValue}])
  -> {executionResults: ExecutionResults}
  Sets the property values of elements.
  Example: {"elementPropertyValues": [{"elementId": {"guid": "..."}, "propertyId": {"guid": "..."}, "propertyValue": {"value": "6"}}]}
```

## 2. execute_command vs execute_script Overlap

Two tools exist for running Archicad commands:

- `execute_command` -- sends a single JSON API command, returns structured result
- `execute_script` -- runs Python code with full API access

In practice, the AI reaches for `execute_script` every time because:
- It can do everything `execute_command` can, plus loops, filtering, file I/O
- The mental overhead of choosing between two tools on every call adds up
- Simple one-liners in script are barely more work than execute_command

The only advantage of `execute_command` is fewer tokens for simple queries (no Python
wrapping needed). Whether that justifies a separate tool is debatable.

**Consideration**: Merging into a single `execute_script` tool would simplify the
interface. The CLAUDE.md already says "Scripts handle complex logic, not tool
proliferation."

## 3. What Worked Well

For reference, these aspects worked without friction:

- Dynamic `execute_script` docstring with all Tapir command signatures -- immediate
  visibility into what's available without calling get_docs
- `get_docs` discovery workflow (overview -> category -> full schema) -- natural and fast
- `get_properties` returning copy-paste-ready GUIDs with usage examples
- Error messages with `suggestion` fields guiding toward fixes
- Element type detection in search (searching "wall" suggests GetElementsByType)
