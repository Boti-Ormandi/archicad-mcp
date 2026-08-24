# Schema snapshots and updates

`archicad-mcp` packages command documentation so `get_docs` works before Archicad starts and without network access. This document defines the runtime update and maintainer contracts for those snapshots.

## Active registry

Each package contains two immutable baselines:

- `src/archicad_mcp/schemas/builtin.json` for Archicad's built-in JSON API;
- `src/archicad_mcp/schemas/tapir.json` for the validated Tapir release recorded in its `_metadata` object.

The server validates the packaged documents at startup. For Tapir commands, it selects the newer valid strict-SemVer version between the packaged snapshot and the user-cache snapshot. Equal versions always select the packaged snapshot.

`get_docs` reads this process-wide registry; it does not generate or ingest documentation from a live Archicad instance. A Tapir command's `version` field means introduced or last changed. It does not claim that an older installed add-on implements a later command or has the exact current schema.

## Runtime update channel

When automatic updates are enabled, startup schedules at most one nonblocking check after the shared 24-hour interval has elapsed. Startup never waits for this check, and there is no recurring timer or separate daemon.

The updater:

1. Lists stable, non-draft, non-prerelease releases from [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation) and selects the highest bare strict-SemVer tag.
2. Resolves and peels the tag to an exact commit instead of trusting `target_commitish`.
3. Downloads `docs/archicad-addon/command_definitions.js`, `docs/archicad-addon/common_schema_definitions.js`, and `LICENSE` at that commit.
4. Transforms the documentation through the same non-executing parser used by the release generator and validates the complete registry.
5. Accepts only a strictly newer version. An equal identity is current; a moved tag, changed accepted hash, or older version is refused.
6. Publishes an accepted snapshot atomically to the user cache and updates the active registry without restarting the server.

Requests use fixed GitHub API and raw-content hosts, strict HTTPS without redirects, and bounded response sizes and timeouts. The updater reads no token or other credential. Future releases are accepted only while the upstream `LICENSE` bytes match the MIT identity pinned by the package; a license change requires a reviewed package release.

This channel trusts the upstream repository, GitHub TLS, and the stable release metadata they provide. The project operates no separate feed or signing service.

## Cache behavior

The cache, check state, and persistent lock live under the operating system's user cache directory. Nothing is written into the installed package. Cache corruption is treated as an absent cache, reported by `doctor` and `schemas status`, and may be replaced by a later valid update.

`ARCHICAD_MCP_AUTO_UPDATE=0` disables automatic checks without disabling a valid cache. `ARCHICAD_MCP_OFFLINE=1` forbids all update network access while retaining a valid cache and takes precedence over automatic mode. `schemas reset` is the only command that deliberately returns selection to the packaged baseline by removing the cached snapshot and check state.

## Updating the packaged Tapir snapshot

A packaged baseline change is a maintainer release operation. The provenance constants in `src/archicad_mcp/schemas/tapir_source.py` identify the provider version, exact upstream commit, pinned license, and SHA-256 of both documentation inputs. Review and update those pins before generating a new baseline.

Download the two documentation inputs at the reviewed upstream commit and run:

```bash
uv run python scripts/generate_tapir_snapshot.py \
    command_definitions.js common_schema_definitions.js \
    --output src/archicad_mcp/schemas/tapir.json
```

The generator performs no Git, network, submodule, or package operation. It verifies the input hashes before parsing, never executes JavaScript, rejects malformed or ambiguous input, validates the resulting registry, and emits deterministic UTF-8 JSON with embedded provenance.

Review a baseline update as one change:

1. Verify the stable upstream tag, peeled commit, input hashes, and license bytes.
2. Update the packaged provenance pins.
3. Generate the snapshot from the exact pinned inputs.
4. Review the generated diff and embedded `_metadata`.
5. Run the generator, registry, updater, cache, MCP protocol, and full repository checks.
6. Include the baseline and its provenance changes in the same reviewed release.

Routine development and end-user setup must not rewrite these files or require an upstream checkout, Git submodule, or manual schema generation.
