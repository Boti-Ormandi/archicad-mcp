# Contributing

Contributions should stay focused, preserve the public MCP and CLI contracts, and include verification appropriate to the change.

## Reporting bugs and vulnerabilities

Search the [existing issues](https://github.com/Boti-Ormandi/archicad-mcp/issues) before filing a bug. After redacting sensitive information, use the [public bug report form](https://github.com/Boti-Ormandi/archicad-mcp/issues/new?template=bug_report.yml) and include:

- output from `uvx archicad-mcp --version`, or the bare command after a persistent install;
- Python, operating system, and MCP client versions;
- the Archicad major and Tapir version for live-operation failures;
- minimal reproduction steps and the expected result; and
- relevant output from `uvx archicad-mcp doctor --json`, or the bare command after a persistent install.

Remove credentials, proprietary project data, private paths, and other sensitive content from diagnostics and logs. Do not post sensitive vulnerability details in a public issue; the project does not currently publish a private reporting channel.

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

When changing script execution documentation or tool metadata, keep the safety, authoring, result, and cancellation contract consistent with [docs/script-execution.md](docs/script-execution.md).

## Schema snapshots

The packaged files under `src/archicad_mcp/schemas/` are release-owned snapshots. Ordinary source changes must not regenerate or edit them. Do not add a repository submodule or a user-side schema generation step.

A deliberate Tapir baseline update must use the pinned upstream files and deterministic generator described in [docs/schema-updates.md](docs/schema-updates.md). Review the provenance constants, generated diff, licensing, registry validation, and protocol tests together.

## Pull requests

- Keep each pull request limited to one coherent change.
- Explain user-visible behavior and any compatibility or security effect.
- Add or update tests when behavior changes.
- Update user documentation when commands, configuration, or limitations change.
- Do not update `uv.lock` unless dependency metadata changed.
- Do not create new `docs/releases/vX.Y.Z.md` files. The existing `v0.2.0.md` and `v0.2.1.md` files are historical records, not inputs to future releases.

Before requesting review, inspect the complete diff and confirm that generated snapshots, historical release records, and unrelated files are unchanged unless the pull request explicitly owns them.

## Maintainer release procedure

Production releases use the manual **Release** workflow and a complete release body supplied at dispatch time:

1. Push the reviewed release source to `origin/master`.
2. Create an annotated exact stable tag (`vX.Y.Z`) at that source and push the tag without moving or replacing an existing stable tag.
3. Prepare the complete public release body outside `docs/releases/`. The workflow does not append generated notes.
4. Dispatch the workflow from `master`, selecting `production`, naming the tag, and loading the body from a file:

   ```bash
   gh workflow run release.yml --ref master -f mode=production -f ref=vX.Y.Z -F release_body=@/path/to/release-body.md
   ```

5. Review the build, repository tests, installed-artifact tests, and exact artifact lineage. Then approve the `pypi` environment.
6. Confirm that exact PyPI reconciliation succeeds before the dependent GitHub Release job publishes the same wheel and source distribution.

For a TestPyPI rehearsal, dispatch from `master` with `mode=testpypi`, set `ref` to an exact lowercase 40-character source SHA, and leave `release_body` empty.

### Release retries and recovery

- Never move, replace, or delete a stable release tag.
- After any possible PyPI or GitHub mutation, use **Re-run failed jobs** on the same workflow run (`gh run rerun RUN_ID --failed`). This preserves successful job outputs and retained artifacts while rerunning failed jobs and their downstream dependents, including a previously skipped GitHub Release job.
- Do not use **Re-run all jobs** for this recovery. A full rerun preserves the run ID, source SHA, and ref, but reruns the build and removes or replaces Actions artifacts from the earlier attempt; it therefore does not preserve the artifact ID or archive digest.
- Start a new dispatch only when no external mutation could have occurred in the earlier run.
- If the same run or its artifact has expired after partial publication, stop and reconcile the external PyPI and GitHub state manually. Do not rebuild and continue automatically.
- A rerun must supply the same body embedded in the original dispatch event. A changed body fails reconciliation against the existing transaction marker without mutating the release.

The workflow creates the GitHub Release as a draft, reconciles its exact assets, and publishes it only after PyPI contains the exact artifact set. Existing exact PyPI files and an exact already-published GitHub Release are idempotent success states.
