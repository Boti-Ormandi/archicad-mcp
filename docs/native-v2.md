# Native v2 release guide

This note covers migration from the 0.1.x line, runtime compatibility, and the maintainer process for immutable packaged snapshots and direct Tapir schema updates.

## Upgrade from 0.1.x

The native-v2 release runs directly on `mcp>=2,<3` and serves MCP over stdio. MCP clients should launch the public `uvx archicad-mcp` console command instead of calling unsupported package-internal server APIs:

```text
uvx archicad-mcp
```

`archicad-mcp` with no arguments and `archicad-mcp serve` use the same stdio server path. `archicad-mcp setup` prints a client configuration without editing files, `archicad-mcp config` is read-only, and `archicad-mcp doctor --json` provides machine-readable package/schema/Archicad diagnostics.

The command catalog no longer depends on a running Archicad instance. Packaged immutable snapshots bootstrap documentation discovery, so remove any client or operator procedure that expected live schema generation before `get_docs` could work. There is also no user-side Git, checkout, submodule, or manual schema-refresh step.

## Archicad and Tapir compatibility

Compatibility is determined by the observed runtime environment together with the active snapshot's documented provider version and each command's recorded version. The release intentionally does not encode a hard-coded Archicad version range that can drift from the shipped catalog.

| Observed environment | Capability status | Behavior |
| --- | --- | --- |
| Supported Archicad major with a compatible Tapir add-on available | `tapir_available` | Native and Tapir capabilities are exposed. |
| Supported Archicad major reachable without Tapir | `tapir_unavailable` | Native capabilities remain available; Tapir-only capabilities are omitted. |
| No Archicad is running, or no live product identity is available | `compatibility_unknown` | The packaged catalog remains fully discoverable; live property/script operations still require a target instance. |

For full capability, install the Tapir release built for the Archicad major in use. Version honesty rules apply at runtime: the latest active documentation is process-wide and identical for every instance, each command keeps its upstream `version` meaning introduced-or-last-changed, and for an observed older `tapir_version`, commands introduced later are not supported by that instance. Commands whose current schema changed after an older add-on's release are conservatively not claimed as exact historical schemas. `list_instances` additionally reports `tapir_version` for each instance that returns an exact supported response, so instances on different add-on releases can expose differing values; the server does not fabricate one filtered global catalog per instance.

## Maintainer schema update process

Schema maintenance is a release operation, not a runtime or end-user operation. The installed package contains `src/archicad_mcp/schemas/builtin.json` and `src/archicad_mcp/schemas/tapir.json` as immutable baseline snapshots.

The packaged Tapir snapshot is self-describing. Its embedded `_metadata` record states the snapshot format, the Tapir provider version it documents, the package path, the upstream repository, tag, and exact commit it was generated from, the SHA-256 of each upstream input file, and the upstream license. The server validates this record at startup and derives every capability document's provider version and provenance from it; missing or malformed metadata prevents startup rather than degrading silently.

To regenerate the Tapir snapshot, a maintainer downloads the release's generated documentation files from the upstream repository (`docs/archicad-addon/command_definitions.js` and `docs/archicad-addon/common_schema_definitions.js` at the recorded tag) and runs:

```bash
uv run python scripts/generate_tapir_snapshot.py \
    command_definitions.js common_schema_definitions.js \
    --output src/archicad_mcp/schemas/tapir.json
```

The generator is deterministic and release-only, and it shares its parsing/transformation code with the runtime update path so release-time and runtime validation cannot drift. It refuses any input whose SHA-256 does not match the recorded upstream bytes, rejects duplicate JSON keys, duplicate command names, and non-integral float literals (integral literals such as `0.0` are normalized to `0` without changing values), validates the complete document through the immutable registry before writing, and produces byte-identical output for the same inputs. It performs no Git, submodule, network, or package operations. When refreshing any packaged baseline, review its provenance and licensing, validate the exact bytes through the registry and protocol tests, and include them in the same reviewed release change. Ordinary source changes must not rewrite these files as a side effect, and the workflow does not depend on repository submodules.

The packaged Tapir baseline documents exactly one add-on release (currently 1.5.8). A command entry's own version field records when that command was introduced or last changed; it does not claim that older installed add-ons implement commands introduced after their release.

For the direct online update channel, maintainers publish ordinary stable releases upstream; the project hosts no feed, key, or metadata service. At runtime the server:

1. Lists the stable (non-draft, non-prerelease) releases of `ENZYME-APD/tapir-archicad-automation` through the GitHub API using conditional requests and bounded pagination, and selects the highest bare strict-SemVer tag client-side rather than trusting `/releases/latest` ordering.
2. Resolves the selected tag and peels annotated tag objects, at most four levels, to a final commit SHA; `target_commitish` is never used as a pin.
3. Downloads exactly `docs/archicad-addon/command_definitions.js`, `docs/archicad-addon/common_schema_definitions.js`, and `LICENSE` at that peeled commit from strict raw HTTPS paths, accepting no redirects or alternate hosts, under size and time limits.
4. Transforms and validates the inputs through the same shared, non-executing code path as the packaged generator, then applies monotonic acceptance: strictly newer versions are accepted, equal identities replay as current, an equal version with a different commit or any different accepted hash is refused as equivocation (a moved tag is equivocation even when derived bytes match), and older versions are refused as rollback.
5. Publishes the canonical snapshot atomically into the versioned user-cache `schema-cache/` directory, records bounded nonsecret check state, and activates the newer schema live without restarting the server.

A future upstream release is accepted only while its `LICENSE` bytes retain the MIT identity pinned with the packaged baseline. Any changed license fails closed and requires a reviewed package release that deliberately updates the pinned license policy.

Users control the channel without configuration beyond opt-outs: `ARCHICAD_MCP_AUTO_UPDATE` unset or `1` keeps the default startup check enabled and exactly `0` disables automatic checks; `ARCHICAD_MCP_OFFLINE=1` forbids all update network access while continuing to load the newest cached schema (manual `archicad-mcp schemas update` refuses offline). No token or other credential is read from configuration or environment for updates. `archicad-mcp schemas reset` deletes the cached snapshot and check state, including obsolete pre-release cache state left by earlier development builds, under the cache lock; the persistent lock file itself remains. Runtime cache/state lives in the OS user cache directory, and the installed package is never modified.
