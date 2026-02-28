# Schema Sync Automation

## Problem

Both schema files (`tapir.json`, `official.json`) require manual work to keep in sync
with their upstream sources. If the upstream repos update, our schemas go stale silently.

## Source Repos

### Tapir schemas (`tapir.json`)
- **Repo**: [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation)
- **Files**: `docs/archicad-addon/command_definitions.js`, `common_schema_definitions.js`
- **Release pattern**: Semver tags without `v` prefix (`1.2.9`, `1.2.10`, ...)
- **Frequency**: Multiple commits/week, releases every 2-4 weeks
- **Current sync**: `archicad-mcp-sync <repo-path>` CLI tool (manual)
- **Format**: JS variable assignments wrapping JSON arrays/objects

### Official API schemas (`official.json`)
- **Repo**: [SzamosiMate/multiconn_archicad](https://github.com/SzamosiMate/multiconn_archicad)
- **Files**: `code_generation/official/schema/_command_details.json`, `official_api_master_schema.json`
- **Release pattern**: Semver tags with `v` prefix (`v0.5.2`, `v0.5.3`, ...)
- **Frequency**: Releases every 1-2 months, tracks Tapir releases
- **Current sync**: None. `official.json` was manually derived from `_command_details.json`.
- **Format**: Plain JSON

Neither repo has GitHub Actions workflows. Neither repo is under our control.

## Approach: Git Submodules + Renovate + CI

### Why this approach

We don't control the upstream repos, so webhooks/repository_dispatch are not an option.
The remaining choices are scheduled polling (build it ourselves) or submodules + Renovate
(let a battle-tested tool handle detection and PR creation). Renovate wins because:

- It already handles release detection, PR creation, changelogs, and auto-merge policies
- The git-submodules manager supports semver tag tracking since [PR #30104](https://github.com/renovatebot/renovate/pull/30104) (merged July 2024)
- Zero custom polling code to maintain

### How it works (end to end)

```
Upstream releases new tag
  -> Renovate detects it (runs on schedule, typically every few hours)
  -> Renovate opens PR bumping .gitmodules branch value to new tag
  -> GitHub Action triggers on that PR
  -> Action runs archicad-mcp-sync against the updated submodule
  -> Action commits regenerated schema files back to the PR branch
  -> PR is ready for review (or auto-merged if configured)
```

## Implementation Steps

### Step 1: Add submodules pinned to release tags

```bash
git submodule add -b 1.2.9 https://github.com/ENZYME-APD/tapir-archicad-automation.git deps/tapir
git submodule add -b v0.5.2 https://github.com/SzamosiMate/multiconn_archicad.git deps/multiconn
```

The `-b <tag>` sets the `branch` field in `.gitmodules` to the current release tag.
This is what Renovate uses to detect the versioning scheme and propose updates.

Resulting `.gitmodules`:
```ini
[submodule "deps/tapir"]
    path = deps/tapir
    url = https://github.com/ENZYME-APD/tapir-archicad-automation.git
    branch = 1.2.9

[submodule "deps/multiconn"]
    path = deps/multiconn
    url = https://github.com/SzamosiMate/multiconn_archicad.git
    branch = v0.5.2
```

These submodules are CI-only -- not runtime dependencies. The `deps/` directory sits at
project root, outside `src/`, so hatchling's src layout already excludes it from package
builds. No `pyproject.toml` changes needed.

### Step 2: Configure Renovate

Install the [Renovate GitHub App](https://github.com/apps/renovate) on the repo, then add:

**`renovate.json`**:
```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended"],
  "git-submodules": {
    "enabled": true
  },
  "packageRules": [
    {
      "matchManagers": ["git-submodules"],
      "matchPackageNames": ["ENZYME-APD/tapir-archicad-automation"],
      "groupName": "tapir schemas",
      "automerge": true
    },
    {
      "matchManagers": ["git-submodules"],
      "matchPackageNames": ["SzamosiMate/multiconn_archicad"],
      "groupName": "official schemas",
      "automerge": true
    }
  ]
}
```

When Renovate detects that the `branch` value in `.gitmodules` is a valid semver string,
it automatically switches from `git-refs` to `semver` versioning and proposes tag-based
updates. No custom regex manager needed.

Docs: [Renovate git-submodules manager](https://docs.renovatebot.com/modules/manager/git-submodules/)

### Step 3: GitHub Action to regenerate schemas on submodule bump

When Renovate opens a PR that bumps a submodule tag, this workflow runs the sync tool
and commits the regenerated schema files back to the PR branch.

**`.github/workflows/schema-sync.yml`**:
```yaml
name: Schema Sync

on:
  pull_request:
    paths:
      - '.gitmodules'
      - 'deps/**'

permissions:
  contents: write

jobs:
  sync-schemas:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref }}
          submodules: true
          token: ${{ secrets.GITHUB_TOKEN }}

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install sync tool
        run: pip install -e .

      - name: Run schema sync (tapir)
        run: archicad-mcp-sync deps/tapir

      - name: Run schema sync (official)
        run: archicad-mcp-sync deps/multiconn

      - name: Check for changes
        id: diff
        run: |
          git diff --quiet src/archicad_mcp/schemas/ || echo "changed=true" >> "$GITHUB_OUTPUT"

      - name: Commit updated schemas
        if: steps.diff.outputs.changed == 'true'
        uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "sync schemas from upstream"
          file_pattern: "src/archicad_mcp/schemas/*.json"
```

Uses [git-auto-commit-action](https://github.com/stefanzweifel/git-auto-commit-action)
to push the regenerated files back to the Renovate PR branch.

### Step 4: Extend `archicad-mcp-sync` for official schemas

The CLI currently only handles Tapir. Extend it to auto-detect the repo type based on
directory contents:

```
archicad-mcp-sync deps/tapir       # detects docs/archicad-addon/ -> tapir sync
archicad-mcp-sync deps/multiconn   # detects code_generation/official/ -> official sync
```

Auto-detection logic:
- `<repo>/docs/archicad-addon/command_definitions.js` exists -> Tapir repo
- `<repo>/code_generation/official/schema/_command_details.json` exists -> multiconn repo
- Neither -> error with message listing what was expected

The official sync should:
- Read `code_generation/official/schema/_command_details.json` (names, descriptions, groups)
- Read `code_generation/official/schema/official_api_master_schema.json` (full parameter/return schemas)
- Merge both into `official.json` with full schemas per command

This upgrades official API docs from name+description to full parameter/return schemas,
making them equivalent to Tapir command docs.

## Implementation Order

1. **Add submodules** -- `deps/tapir` and `deps/multiconn` pinned to release tags
2. **Configure Renovate** -- `renovate.json` with git-submodules enabled
3. **Add GitHub Action** -- `.github/workflows/schema-sync.yml`
4. **Extend sync CLI** -- auto-detect repo type, add official API sync with full schemas

Version tracking is handled by git itself (`.gitmodules` branch field + submodule commit)
so no separate staleness metadata is needed in the generated JSON.

## Notes

- Submodules in `deps/` are CI-only. Local dev still uses `archicad-mcp-sync <local-path>`.
- Renovate's git-submodules manager is still labeled beta but the semver tag feature
  has been merged and working since mid-2024.
- If Renovate proves unreliable, the fallback is a simple cron-based GitHub Action
  that polls `gh api repos/{owner}/{repo}/releases/latest` -- all the submodule and
  CI infrastructure stays the same, only the trigger mechanism changes.
