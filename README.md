# archicad-mcp

MCP server for Archicad automation. Connects AI assistants to running Archicad instances via the [Tapir JSON API](https://github.com/ENZYME-APD/tapir-archicad-automation), enabling everything from simple queries to complex multi-step workflows through Python scripting.

Built on a script-first architecture: instead of wrapping every Archicad command as a separate tool, the server exposes 4 tools and lets the AI write Python for complex operations.

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

## Quick Start

### Install

```bash
git clone https://github.com/Boti-Ormandi/archicad-mcp.git
cd archicad-mcp
uv sync
```

### Configure your MCP client

Add to your MCP client configuration (e.g. Claude Desktop, VS Code, etc.):

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

### Use

With Archicad running and the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation) installed, the server auto-discovers instances on startup. Ask your AI assistant to interact with Archicad - it has full access to the command reference and can write scripts for complex operations.

## Configuration

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
- Archicad 25+ with the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation) installed
- An MCP-compatible client

## Development

The repo uses git submodules in `deps/` for upstream schema tracking (CI-only, not needed for local development). If you need to run the schema sync tool locally:

```bash
git submodule update --init
```

```bash
uv sync --all-extras

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

## License

[MIT](LICENSE)
