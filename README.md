# archicad-mcp

[![CI](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/archicad-mcp.svg)](https://pypi.org/project/archicad-mcp/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)

MCP server for Archicad automation. Connects AI assistants to running Archicad instances via the [Tapir JSON API](https://github.com/ENZYME-APD/tapir-archicad-automation), enabling everything from simple queries to complex multi-step workflows through Python scripting.

Built on a script-first architecture: 4 MCP tools front 173 underlying Archicad commands (100 Tapir + 73 built-in). Rather than expose each command as its own tool, the server lets the AI write Python directly against the API.

## Design

**Minimal tool surface.** Every Archicad command is accessible through `execute_script`, which provides full async Python with loops, filtering, and file I/O. Complex logic lives in Python scripts, not in per-command tool wrappers.

**Dynamic documentation.** The `execute_script` tool description is generated at startup from live Archicad schemas. The AI always sees accurate command signatures, parameter types, and examples - no stale docs.

**Multi-instance.** Parallel port scanning across 19723-19744 discovers all running Archicad instances. Target any instance by port number - work with multiple projects simultaneously.

**Full-text search.** Inverted index over all command schemas with weighted field scoring and fuzzy matching via rapidfuzz. Typo-tolerant: "proprty" still finds property commands.

## Tools

| Tool | Purpose |
|------|---------|
| `list_instances` | Discover running Archicad instances (port, project name, version, Tapir status) |
| `execute_script` | Execute Python with full async Archicad API access and file I/O |
| `get_docs` | Search and retrieve command documentation (schemas, examples, parameters) |
| `get_properties` | Discover element properties (area, volume, length) with cached GUID lookup |

## Example

A typical interaction — "give me a room area schedule by floor":

The AI calls `list_instances` to find a running Archicad:

```json
[
  {
    "port": 19723,
    "project_name": "Residential_Block.pln",
    "project_path": "C:/Projects/Residential_Block.pln",
    "project_type": "solo",
    "archicad_version": "27.0.0",
    "is_tapir_available": true
  }
]
```

Then `get_properties` to look up the GUID for "Net Area":

```json
{
  "found": true,
  "property": {
    "name": "Net Area",
    "group": "Zone",
    "guid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "type": "StaticBuiltIn",
    "value_type": "Real",
    "measure_type": "Area",
    "editable": false
  }
}
```

Then `execute_script` with the Python it composes from those lookups:

```python
zones = (await archicad.tapir("GetElementsByType", {"elementType": "Zone"}))["elements"]
details = (await archicad.tapir("GetDetailsOfElements", {"elements": zones}))["detailsOfElements"]
# guid from the get_properties response above
props = (await archicad.tapir("GetPropertyValuesOfElements", {
    "elements": zones,
    "properties": [{"propertyId": {"guid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}}],
}))["propertyValuesForElements"]

by_floor: dict = {}
for zone, det, row in zip(zones, details, props):
    floor = det["floorIndex"]
    raw_area = row["propertyValues"][0]["propertyValue"]["value"]
    # Locale: Archicad may return "12,40" instead of "12.40"
    area = float(str(raw_area).replace(",", "."))
    bucket = by_floor.setdefault(floor, {"zones": [], "total_m2": 0.0})
    bucket["zones"].append({"name": det["details"]["name"], "area_m2": round(area, 2)})
    bucket["total_m2"] = round(bucket["total_m2"] + area, 2)

result = {"total_zones": len(zones), "by_floor": by_floor}
```

Returns:

```json
{
  "success": true,
  "result": {
    "total_zones": 18,
    "by_floor": {
      "0": {
        "zones": [
          {"name": "Entrance Hall", "area_m2": 8.4},
          {"name": "Living Room", "area_m2": 32.1}
        ],
        "total_m2": 55.2
      },
      "1": {
        "zones": [
          {"name": "Master Bedroom", "area_m2": 22.3}
        ],
        "total_m2": 37.1
      }
    }
  },
  "execution_time_ms": 287
}
```

A per-command MCP server would chain those API calls into separate tool invocations and force the AI to aggregate client-side. With `execute_script`, the chain, the loop, and the aggregation all live in one round-trip.

## Quick Start

Install the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation) into your Archicad (versions 25–29 supported, Windows and macOS). It ships as a per-version `.apx` (Windows) or `.zip` (macOS) file, installed via Archicad's *Options → Add-On Manager → Add*.

Then add to your MCP client configuration (e.g. Claude Desktop, VS Code, etc.):

```json
{
  "mcpServers": {
    "archicad": {
      "type": "stdio",
      "command": "uvx",
      "args": ["archicad-mcp"]
    }
  }
}
```

`uvx` fetches the latest release from PyPI on first run. Pin to a specific version like `["archicad-mcp@0.1.0"]`. To run from a local checkout instead, see [Development](#development).

### Use

With Archicad running, the server auto-discovers instances on startup. Ask your AI assistant to interact with Archicad — it has full access to the command reference and can write scripts for complex operations.

## Security

The script executor supports two security modes, controlled via environment variables:

| Variable | Values | Default |
|----------|--------|---------|
| `ARCHICAD_MCP_SECURITY` | `unrestricted`, `sandboxed` | `unrestricted` |
| `ARCHICAD_MCP_BLOCKED_PATHS` | Comma-separated glob patterns | OS system directories |
| `ARCHICAD_MCP_ALLOWED_WRITE_PATHS` | Comma-separated glob patterns | Desktop, Documents, temp |

**Unrestricted** (default): Read/write access to most paths. System directories (e.g. `C:/Windows`, `/usr`) are always blocked.

**Sandboxed**: Read access everywhere, write access restricted to the allowed paths list.

## Requirements

- Python 3.11+
- Archicad 25–29 with the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation) installed
- An MCP-compatible client

## Development

```bash
git clone https://github.com/Boti-Ormandi/archicad-mcp.git
cd archicad-mcp
uv sync --all-extras   # runtime + dev tooling (ruff, mypy, pytest)
```

To point your MCP client at the local checkout instead of the published package:

```json
{
  "mcpServers": {
    "archicad": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/archicad-mcp", "archicad-mcp"]
    }
  }
}
```

Dev tooling:

```bash
# Lint and format
ruff check src/
ruff format src/

# Type check
mypy src/

# Tests (unit + mock, no Archicad needed)
pytest -m "not integration"

# Integration tests (requires running Archicad)
pytest
```

### Schema sync

The repo uses git submodules in `deps/` for upstream schema tracking (CI-only, not needed for local development). To regenerate the embedded schemas locally:

```bash
git submodule update --init
archicad-mcp-sync deps/tapir       # regenerates src/archicad_mcp/schemas/tapir.json
archicad-mcp-sync deps/multiconn   # regenerates src/archicad_mcp/schemas/builtin.json
```

## License

[MIT](LICENSE)
