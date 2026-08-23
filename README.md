# archicad-mcp

[![CI](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Boti-Ormandi/archicad-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/archicad-mcp.svg)](https://pypi.org/project/archicad-mcp/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)

`archicad-mcp` is a native MCP server for Archicad automation. It uses `mcp>=2,<3` over stdio and exposes four tools: `list_instances`, `get_docs`, `get_properties`, and `execute_script`.

The command library is available before Archicad is running. Immutable packaged snapshots provide the baseline command registry, so clients can browse, search, and retrieve Archicad and Tapir documentation as soon as the server starts. By default, each server start performs one bounded nonblocking update check against the authoritative Tapir GitHub repository and can activate a newer validated schema stored in the OS user cache directory; the installed package is never modified.

## Design

**Native MCP with a small tool surface.** The server is implemented on the native MCP SDK and serves stdio by default. Four MCP tools cover instance discovery, command documentation, property discovery, and script execution rather than exposing every Archicad command as a separate MCP tool.

**Immutable documentation registry.** `builtin.json` and `tapir.json` are packaged baseline snapshots. The Tapir snapshot is generated from the Tapir add-on's own `GenerateDocumentation` output for release 1.5.8 (upstream: [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation), MIT license); each command entry records the add-on version that introduced or last changed it. The packaged baseline is therefore Tapir 1.5.8: every documented Tapir command is available on that add-on release, and an installed add-on older than a command's recorded version does not implement that command. `get_docs` reads the immutable capability registry; it does not generate or ingest documentation from a live Archicad process. The complete packaged command library remains discoverable even when no Archicad instance is running.

**Deterministic discovery and retrieval.** `get_docs()` browses the catalog, `get_docs(search="...")` performs intent search, and exact or batch retrieval accepts both bare and namespaced IDs. For example, `CreateSlabs` and `tapir:CreateSlabs` identify the same Tapir capability, while `API.GetAllElements` and `native:API.GetAllElements` identify the same native capability.

**Direct Tapir schema updates.** The active Tapir schema is always the newer valid schema by strict semantic version between the packaged snapshot and the user-cache snapshot; equal versions always select the packaged snapshot. By default the running server schedules one bounded, nonblocking update check at startup when the shared 24-hour TTL permits; there is no recurring timer and nothing runs after the server exits. Startup immediately serves the packaged or cached catalog and never waits for the network. `ARCHICAD_MCP_AUTO_UPDATE=0` disables automatic checks without disabling or downgrading the cache, `ARCHICAD_MCP_OFFLINE=1` forbids all update network access while continuing to load the cache, and `archicad-mcp schemas update` requests a manual check. See [Tapir schema updates](#tapir-schema-updates).

**Multi-instance discovery.** The default local scan covers ports 19723 through 19743 inclusive. Port 19744 is not part of the default range.

**Honest local execution.** `execute_script` runs Python in a disposable same-user `local_user` child process. The child provides timeout/cancellation handling, stdout/stderr capture, and structured failures, but it is not hostile-code isolation: the script has the ordinary authority of the user account. There is no approval or confirmation gate.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_instances` | Discover running Archicad instances on the default local port range and report project/version/Tapir availability, including each instance's observed Tapir version when it reports one. |
| `get_docs` | Deterministically browse the command catalog, intent-search it, or retrieve one or many exact command documents by bare or namespaced ID. |
| `get_properties` | Discover Archicad element properties and property identifiers for a selected running instance. |
| `execute_script` | Execute Python against a selected Archicad instance in a disposable same-user child process. |

## Quick Start

`uvx` can run the published package without a separate project installation:

```bash
uvx archicad-mcp --help
uvx archicad-mcp doctor --json
```

`archicad-mcp` with no arguments starts the stdio MCP server. The explicit equivalent is `archicad-mcp serve`.

Add it to an MCP client configuration:

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

For a ready-to-copy configuration snippet, run:

```bash
uvx archicad-mcp setup
```

`setup` is output-only; it does not edit client configuration files. `archicad-mcp config` is also read-only and reports the effective runtime settings.

Install the [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation) that is compatible with the Archicad major you use for full native + Tapir capability. If Archicad is reachable without Tapir, the capability view reports `tapir_unavailable`: native capabilities remain available, while Tapir-only capabilities are omitted.

## Use

Documentation discovery does not require Archicad to be running. Typical `get_docs` modes are:

```text
get_docs()                                      # browse overview/categories
get_docs(category="Element Listing Commands") # deterministic category browse
get_docs(search="create slab")                 # intent search
get_docs(command="CreateSlabs")                # exact Tapir retrieval
get_docs(command="tapir:CreateSlabs")          # same exact Tapir capability
get_docs(command="API.GetAllElements")         # exact native retrieval
get_docs(commands=["CreateSlabs", "API.GetAllElements"])  # batch retrieval
```

When Archicad is running, call `list_instances` to discover targets, use `get_properties` or `get_docs` to gather identifiers and command contracts, then call `execute_script` for multi-step Python workflows. The default execution timeout is 300 seconds; timeout and transport cancellation terminate the owned worker. Stdout and stderr are captured and failures are returned in structured form.

Useful CLI commands are:

```bash
archicad-mcp                 # stdio serve
archicad-mcp serve           # explicit stdio serve
archicad-mcp doctor --json   # package/schema/Archicad diagnostics
archicad-mcp setup           # print MCP client setup only
archicad-mcp config          # read-only effective configuration
archicad-mcp schemas status  # local-only packaged/cache/active/check diagnostics
archicad-mcp schemas update  # manual update check (honors offline mode)
archicad-mcp schemas reset   # delete the cached schema and check state
archicad-mcp --help
archicad-mcp --version
```

## Security

`execute_script` uses the `local_user` execution model. A script runs in a disposable child process under the same user account as the server. The process boundary is for reliability and cancellation, not hostile-code containment: scripts can use ordinary Python imports, access files available to the user, start processes, make network requests, and perform destructive Archicad/Tapir operations with that user's authority. There is no filesystem path policy and no approval/confirmation gate.

Schema acquisition has its own bounded network boundary. Update traffic goes only to fixed GitHub endpoints for the upstream [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation) repository over strict HTTPS with no redirects, under size and time limits, and no token or other credential is read from configuration or environment. First use trusts that public GitHub repository, GitHub TLS, and the stable release metadata it presents — the same upstream users already trust for the Tapir add-on binary; the project operates no feed and makes no additional signature or independent-review claim. Acceptance is monotonic: a moved tag for an already accepted version, a hash mismatch, or an older release is refused and the active schema is retained. Future releases are accepted only while the upstream `LICENSE` bytes match the MIT identity pinned with the packaged baseline; a changed license fails closed until a reviewed package release updates the pin.

Runtime cache/state belongs in the OS user cache directory. The server never writes into the installed package and does not require Git, a checkout, submodules, tokens, or a manual schema refresh on the user side.

| Variable | Behavior |
| --- | --- |
| `ARCHICAD_MCP_AUTO_UPDATE` | Unset or `1`: automatic startup checks are enabled (the default). Exactly `0`: automatic checks are disabled; cached schemas remain usable. |
| `ARCHICAD_MCP_OFFLINE` | Set exactly to `1`: all update network access is forbidden, the newest valid cached schema remains active, and manual updates refuse with an offline error. Overrides automatic mode. |

## Tapir schema updates

Every release contains immutable `builtin.json` and `tapir.json` baseline snapshots. They are sufficient to start the server and discover the packaged command catalog offline, and they document the newest validated Tapir release available at packaging time.

At startup the running server schedules at most one update check, gated by a shared 24-hour TTL across processes. The check is a single bounded background task inside the already-running MCP server: it never delays startup, never recurs on a timer, and nothing runs after the server exits. A successful check reprojects the capability view atomically; a failed, disabled, or skipped check retains the active view untouched.

Selection compares strict semantic versions. A candidate newer than the active schema is transformed, validated, and accepted; an equal version with the same recorded commit and input hashes is treated as current; an equal version with a different commit or any different accepted hash is refused as equivocation — a moved release tag is equivocation even when the derived bytes match; an older version is refused as rollback. A newer cache stays active across restarts and in offline mode until a newer package supersedes it, and equal versions always select the packaged snapshot.

Acquisition is fixed to the stable GitHub Releases of [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation). The server lists stable releases, selects the highest bare SemVer tag client-side, resolves and peels that tag to an exact commit, and downloads exactly three files at that commit over strict HTTPS: the two generated documentation inputs and the `LICENSE` file. Responses are size- and time-limited, and no token is read. The inputs pass a strict, non-executing transformation and full registry validation before anything is published to the cache. A future upstream release is accepted only while its `LICENSE` file keeps the MIT identity pinned by the packaged baseline; a changed license fails closed until a reviewed package release updates the pinned license policy.

Validated snapshots are cached under the OS user cache directory (`archicad-mcp/schema-cache/`) together with bounded check state and a permanent lock file. Cache corruption is treated as absent, surfaced by `doctor` and `schemas status`, and may self-heal on a later successful update. Nothing is ever written into the installed package.

Manual control:

```bash
archicad-mcp schemas status --json   # local-only packaged/cache/active/check diagnostics; never networks
archicad-mcp schemas update --json   # immediate manual check; bypasses the TTL but honors offline mode
archicad-mcp schemas reset --json    # delete the cached snapshot and check state; the only downgrade-to-package operation
```

See [Native v2 release guide](docs/native-v2.md) for migration, compatibility, and maintainer regeneration notes.

## Requirements

- Python 3.11+
- An MCP-compatible client
- For live automation: a supported Archicad major
- For full capability: a Tapir add-on release built for that Archicad major

Without Tapir, a reachable Archicad instance operates in the documented `tapir_unavailable` partial mode: native capabilities remain available and Tapir-only capabilities are omitted. The active documentation describes current Tapir behavior; it does not prove exact support for every older installed add-on. Each command records the add-on version that introduced or last changed it, and an installed add-on older than a command's recorded version does not implement that command.

## Development

```bash
git clone https://github.com/Boti-Ormandi/archicad-mcp.git
cd archicad-mcp
uv sync --frozen --all-extras --dev
```

To point an MCP client at a local checkout:

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

Quality checks:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -m "not integration"
```

The unit and MCP protocol/stdio tests do not require a running Archicad instance. Ordinary source development does not regenerate or rewrite the packaged schema snapshots. Do not initialize repository submodules or run a manual schema refresh to update them; replacing packaged snapshots and refreshing their upstream provenance pins are explicit maintainer release operations described in [docs/native-v2.md](docs/native-v2.md).

## Migration from 0.1.x

The native-v2 release uses the native MCP SDK and the `archicad-mcp` console command. Existing client configurations should launch the public `uvx archicad-mcp` console command rather than calling unsupported package-internal server entry points. See [docs/native-v2.md](docs/native-v2.md) for the migration checklist and compatibility behavior.

## License

[MIT](LICENSE)
