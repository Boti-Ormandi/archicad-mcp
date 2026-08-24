# Contributing

Contributions should stay focused, preserve the public MCP and CLI contracts, and include verification appropriate to the change.

## Reporting bugs

Search the [existing issues](https://github.com/Boti-Ormandi/archicad-mcp/issues) before filing a bug. Use the bug report form and include:

- output from `archicad-mcp --version`;
- Python, operating system, and MCP client versions;
- the Archicad major and Tapir version for live-operation failures;
- minimal reproduction steps and the expected result; and
- relevant output from `archicad-mcp doctor --json`.

Remove credentials, proprietary project data, and other sensitive information from diagnostics and logs.

## Development setup

```bash
git clone https://github.com/Boti-Ormandi/archicad-mcp.git
cd archicad-mcp
uv sync --frozen --all-extras --dev
```

To point an MCP client at the checkout:

```json
{
  "mcpServers": {
    "archicad": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/archicad-mcp", "archicad-mcp"]
    }
  }
}
```

## Checks

Run the same quality checks as the primary CI job:

```bash
uv lock --check
uv sync --frozen --all-extras --dev
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src
uv run pytest -q
```

Release tooling has a separate strict mypy lane. Set `MYPYPATH` to `src` first (`$env:MYPYPATH = "src"` in PowerShell or `export MYPYPATH=src` in a POSIX shell), then run:

```bash
uv run mypy --explicit-package-bases scripts/verify_release_artifacts.py scripts/generate_tapir_snapshot.py scripts/release_operations.py tests/unit/test_release_artifacts.py tests/unit/test_release_operations.py
```

`uv run pre-commit run --all-files` is a useful additional local check, but it does not replace the full CI command set above.

Tests marked `integration` require a running Archicad instance with Tapir and skip when Archicad is unavailable. For faster offline iteration, use `uv run pytest -m "not integration"`; run the complete `uv run pytest -q` suite before opening a pull request.

## Schema snapshots

The packaged files under `src/archicad_mcp/schemas/` are release-owned snapshots. Ordinary source changes must not regenerate or edit them. Do not add a repository submodule or a user-side schema generation step.

A deliberate Tapir baseline update must use the pinned upstream files and deterministic generator described in [docs/schema-updates.md](docs/schema-updates.md). Review the provenance constants, generated diff, licensing, registry validation, and protocol tests together.

## Pull requests

- Keep each pull request limited to one coherent change.
- Explain user-visible behavior and any compatibility or security effect.
- Add or update tests when behavior changes.
- Update user documentation when commands, configuration, or limitations change.
- Do not update `uv.lock` unless dependency metadata changed.
- Do not create or edit `docs/releases/*.md` in an ordinary pull request. Those files are curated inputs to the named release transaction.

Before requesting review, inspect the complete diff and confirm that generated snapshots, release notes, and unrelated files are unchanged unless the pull request explicitly owns them.
