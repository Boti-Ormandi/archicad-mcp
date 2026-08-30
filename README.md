# archicad-mcp

[![CI](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/archicad-mcp.svg)](https://pypi.org/project/archicad-mcp/)

`archicad-mcp` is a local stdio MCP server for Archicad automation through Archicad's built-in JSON API, with optional [Tapir](https://github.com/ENZYME-APD/tapir-archicad-automation) commands. Its four tool categories cover instance discovery, searchable command documentation, property discovery, and multi-step Python workflows. Validated Archicad and Tapir command documentation is packaged with the server and is available without a running Archicad instance.

## Quick start

### Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- An MCP client that can launch a local stdio server

Add the server to the client's MCP configuration:

```json
{
  "mcpServers": {
    "archicad": {
      "command": "uvx",
      "args": ["archicad-mcp"]
    }
  }
}
```

Save the configuration and restart or reload the client. Then call:

```text
get_docs(command="API.GetAllElements")
```

A successful response has the ID `native:API.GetAllElements` and includes the command schema. This first success requires neither Archicad nor Tapir.

## Tools and workflow

| Tool | Purpose |
| --- | --- |
| `list_instances` | Find running Archicad instances and their ports. |
| `get_docs` | Browse, search, or retrieve built-in and Tapir command schemas. |
| `get_properties` | Find element properties and property IDs. |
| `execute_script` | Run a multi-step Python workflow against an instance. |

Use `get_docs` to move from discovery to exact schemas:

```text
get_docs()
get_docs(search="create slab")
get_docs(command="API.GetAllElements")
get_docs(commands=["API.GetAllElements", "CreateSlabs"])
```

For live work, call `list_instances`, inspect the needed command schemas with `get_docs` and property IDs with `get_properties`, then pass the selected `port` to `execute_script`. Live tools require Archicad, and the server and Archicad must run on the same host. Tapir is needed only for Tapir calls and `get_properties`; built-in Archicad capabilities remain available without it.

## Script execution

`execute_script` accepts the body of an async Python function. It injects `archicad` and `port`, permits top-level `await`, and returns the value assigned to `result`.

Scripts run as your operating-system user and are not sandboxed; review the code before executing it.

```python
result = await archicad.command("GetProductInfo")
```

See [Script execution](https://github.com/Boti-Ormandi/archicad-mcp/blob/master/docs/script-execution.md) for the complete authoring, result, cancellation, and safety contract.

## Diagnostics and installation alternatives

Use the public console command for setup and diagnostics:

- `uvx archicad-mcp setup` prints the client-neutral configuration without editing files.
- `uvx archicad-mcp doctor --json` reports package, schema, and local Archicad diagnostics.
- `uvx archicad-mcp config --json` prints the effective runtime configuration.
- `uvx archicad-mcp --version` and `uvx archicad-mcp --help` report the installed version and available commands.

For a persistent installation, run:

```bash
uv tool install archicad-mcp
```

The installed `archicad-mcp` command then replaces `uvx archicad-mcp` in terminal commands and the MCP configuration. If live discovery fails, check that Archicad is listening on its local JSON API port in the range 19723-19743.

For migration from 0.1.x, use the public console command; live schema generation, repository submodules, and a preliminary manual schema refresh are no longer part of command discovery.

## Schema updates

`get_docs` uses the packaged command documentation or a newer validated Tapir snapshot from the user cache. When automatic updates are enabled and the shared 24-hour interval permits, startup schedules at most one bounded, nonblocking check. Startup does not wait for it, and there is no recurring timer or daemon.

| Variable | Behavior |
| --- | --- |
| `ARCHICAD_MCP_AUTO_UPDATE=0` | Disable automatic schema checks while retaining a valid cache. |
| `ARCHICAD_MCP_OFFLINE=1` | Disable schema-update network access while retaining a valid cache. |

Offline schema mode does not stop `uvx` from acquiring the package. Use `archicad-mcp schemas status`, `archicad-mcp schemas update`, and `archicad-mcp schemas reset` to inspect, update, or reset the cache. An older installed Tapir add-on may not implement commands in a newer active schema; built-in Archicad documentation is unaffected.

See [Schema snapshots and updates](https://github.com/Boti-Ormandi/archicad-mcp/blob/master/docs/schema-updates.md) for the cache, validation, update, and maintainer contracts.

## Contributing and releases

See [CONTRIBUTING.md](https://github.com/Boti-Ormandi/archicad-mcp/blob/master/CONTRIBUTING.md) for development setup and checks, report bugs with the [GitHub bug form](https://github.com/Boti-Ormandi/archicad-mcp/issues/new?template=bug_report.yml), and find release notes and artifacts on [GitHub Releases](https://github.com/Boti-Ormandi/archicad-mcp/releases).

## License

[MIT](https://github.com/Boti-Ormandi/archicad-mcp/blob/master/LICENSE)
