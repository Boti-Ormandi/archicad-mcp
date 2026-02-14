# Archicad MCP Server - AI Instructions

## Project Overview
MCP server for Archicad automation with 4 tools. Script-first architecture where AI writes Python for complex operations.

## Research Resources
- **Tapir**: [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation) - the Archicad JSON API add-on we connect to
- **Built-in API**: [SzamosiMate/multiconn_archicad](https://github.com/SzamosiMate/multiconn_archicad) - Archicad JSON API schemas and code generation (Python wrapper, not official Graphisoft docs)
- **Archicad API DevKit**: Official API documentation and struct definitions
- Use these to understand API behavior, not just guess from responses or web searches

## Architecture Rules

### Tool Design
- 4 tools: `list_instances`, `execute_script`, `get_docs`, `get_properties`
- NO individual wrappers for each Archicad command
- NO semantic search / embeddings
- Scripts handle complex logic, not tool proliferation

### Code Conventions
- Python 3.11+ features (type unions with `|`, match statements OK)
- Type hints on all public functions
- Async/await for all I/O (aiohttp, not requests)
- Raw dicts for Archicad responses, Pydantic only for our types

### File Structure
```
src/archicad_mcp/
├── server.py              # FastMCP server, 4 tool definitions, lifespan
├── config.py              # Security config (sandboxed/unrestricted, path blocking)
├── models.py              # Pydantic models (ArchicadInstance, ScriptResult)
├── core/
│   ├── connection.py      # Single Archicad connection (HTTP, Tapir/built-in dispatch)
│   ├── manager.py         # Multi-instance discovery, port scanning 19723-19744
│   ├── errors.py          # Typed exceptions (ConnectionError, CommandError, etc.)
│   └── properties.py      # Property cache, search, group/type filtering
├── scripting/
│   ├── executor.py        # Script sandbox (builtins, modules, timeout, safe open)
│   └── api.py             # ArchicadAPI class injected into script namespace
└── schemas/
    ├── cache.py           # SchemaCache: load, merge, resolve $refs, sync from repos
    ├── docgen.py          # Generate execute_script docstring from schemas
    ├── search.py          # Inverted index, weighted scoring, fuzzy matching
    ├── tapir.json         # Embedded Tapir command schemas (auto-updated)
    └── builtin.json       # Embedded built-in API schemas (synced from multiconn)

tests/
├── unit/                  # Pure logic tests (no I/O, no mocking)
├── mock/                  # HTTP-mocked tests (aioresponses)
└── integration/           # Live Archicad tests (@pytest.mark.integration)
```

### Error Handling
- Use typed exceptions from `core/errors.py`
- Always include `suggestion` field for actionable AI guidance
- Return structured errors, not raw strings

### Dependencies
- mcp, aiohttp, pydantic, rapidfuzz, openpyxl
- NO sentence-transformers, faiss, or heavy ML deps

## What NOT To Do
- Don't create wrapper functions for individual Archicad commands
- Don't add "semantic search" or "smart routing"
- Don't model every Archicad response type with Pydantic
- Don't use sync HTTP (requests library)
- Don't add features without discussion

## Testing
- Unit tests: Pure logic, no I/O
- Mock tests: Use aioresponses for HTTP mocking
- Integration tests: Marked with `@pytest.mark.integration`, require real Archicad

## Pre-commit
Pre-commit hooks run automatically on commit (ruff lint, ruff format, mypy, trailing whitespace, yaml/toml checks). If a hook fails, fix the issue and commit again.

## Commands (Manual, only when needed)
```bash
ruff check .              # Lint
ruff check . --fix        # Auto-fix lint issues
ruff format .             # Format code
mypy src/                 # Type check
pytest                    # Run tests
pytest -m "not integration"  # Skip integration tests
archicad-mcp-sync <repo>  # Sync schemas (auto-detects Tapir or multiconn repo)
```
