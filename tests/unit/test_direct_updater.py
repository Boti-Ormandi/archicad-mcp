"""Focused direct-updater tests: monotonicity, leases, CAS, and lifecycle."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from archicad_mcp.schemas import cache_store as cs
from archicad_mcp.schemas import updater as upd
from archicad_mcp.schemas.github_release import (
    COMMAND_DEFINITIONS_PATH,
    COMMON_SCHEMA_DEFINITIONS_PATH,
    LICENSE_PATH,
    FetchOutcome,
    git_ref_url,
    raw_source_url,
    releases_page_one_url,
)
from archicad_mcp.schemas.tapir_source import (
    CACHE_DISTRIBUTION,
    TapirSnapshotIdentity,
    serialize_tapir_snapshot,
    sha256_hex,
    snapshot_metadata,
    transform_inputs,
)

UPSTREAM_COMMIT = "ce033d6bdcc90b538b3c5f7ab62f676099b96823"
NEW_COMMIT = "f" * 40
UPSTREAM_REPOSITORY = "https://github.com/ENZYME-APD/tapir-archicad-automation"

VERSION_COMMAND: dict[str, object] = {
    "name": "GetAddOnVersion",
    "version": "0.1.0",
    "description": "Reports the version.",
    "inputScheme": None,
    "outputScheme": {
        "type": "object",
        "properties": {"version": {"type": "string"}},
        "required": ["version"],
    },
}


def full_command(name: str, version: str) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "description": f"{name} description.",
        "inputScheme": None,
        "outputScheme": None,
    }


def source_js(version_command_name: str = "GetAddOnVersion") -> bytes:
    command = dict(VERSION_COMMAND)
    if version_command_name != "GetAddOnVersion":
        command = full_command(version_command_name, "0.1.0")
    return (
        b"var gCommands = "
        + json.dumps([{"name": "C", "commands": [command]}]).encode("utf-8")
        + b";\n"
    )


COMMON_JS = b'var gSchemaDefinitions = {"ElementType":{"enum":["Wall"]}};\n'
# The exact pinned upstream LICENSE bytes (sha256 matches PINNED_LICENSE_SHA256).
PINNED_LICENSE_BYTES_B64 = "TUlUIExpY2Vuc2UKCkNvcHlyaWdodCAoYykgMjAyNCBFbnp5bWUgQVBECgpQZXJtaXNzaW9uIGlzIGhlcmVieSBncmFudGVkLCBmcmVlIG9mIGNoYXJnZSwgdG8gYW55IHBlcnNvbiBvYnRhaW5pbmcgYSBjb3B5Cm9mIHRoaXMgc29mdHdhcmUgYW5kIGFzc29jaWF0ZWQgZG9jdW1lbnRhdGlvbiBmaWxlcyAodGhlICJTb2Z0d2FyZSIpLCB0byBkZWFsCmluIHRoZSBTb2Z0d2FyZSB3aXRob3V0IHJlc3RyaWN0aW9uLCBpbmNsdWRpbmcgd2l0aG91dCBsaW1pdGF0aW9uIHRoZSByaWdodHMKdG8gdXNlLCBjb3B5LCBtb2RpZnksIG1lcmdlLCBwdWJsaXNoLCBkaXN0cmlidXRlLCBzdWJsaWNlbnNlLCBhbmQvb3Igc2VsbApjb3BpZXMgb2YgdGhlIFNvZnR3YXJlLCBhbmQgdG8gcGVybWl0IHBlcnNvbnMgdG8gd2hvbSB0aGUgU29mdHdhcmUgaXMKZnVybmlzaGVkIHRvIGRvIHNvLCBzdWJqZWN0IHRvIHRoZSBmb2xsb3dpbmcgY29uZGl0aW9uczoKClRoZSBhYm92ZSBjb3B5cmlnaHQgbm90aWNlIGFuZCB0aGlzIHBlcm1pc3Npb24gbm90aWNlIHNoYWxsIGJlIGluY2x1ZGVkIGluIGFsbApjb3BpZXMgb3Igc3Vic3RhbnRpYWwgcG9ydGlvbnMgb2YgdGhlIFNvZnR3YXJlLgoKVEhFIFNPRlRXQVJFIElTIFBST1ZJREVEICJBUyBJUyIsIFdJVEhPVVQgV0FSUkFOVFkgT0YgQU5ZIEtJTkQsIEVYUFJFU1MgT1IKSU1QTElFRCwgSU5DTFVESU5HIEJVVCBOT1QgTElNSVRFRCBUTyBUSEUgV0FSUkFOVElFUyBPRiBNRVJDSEFOVEFCSUxJVFksCkZJVE5FU1MgRk9SIEEgUEFSVElDVUxBUiBQVVJQT1NFIEFORCBOT05JTkZSSU5HRU1FTlQuIElOIE5PIEVWRU5UIFNIQUxMIFRIRQpBVVRIT1JTIE9SIENPUFlSSUdIVCBIT0xERVJTIEJFIExJQUJMRSBGT1IgQU5ZIENMQUlNLCBEQU1BR0VTIE9SIE9USEVSCkxJQUJJTElUWSwgV0hFVEhFUiBJTiBBTiBBQ1RJT04gT0YgQ09OVFJBQ1QsIFRPUlQgT1IgT1RIRVJXSVNFLCBBUklTSU5HIEZST00sCk9VVCBPRiBPUiBJTiBDT05ORUNUSU9OIFdJVEggVEhFIFNPRlRXQVJFIE9SIFRIRSBVU0UgT1IgT1RIRVIgREVBTElOR1MgSU4gVEhFClNPRlRXQVJFLgo="
LICENSE_BYTES = base64.b64decode(PINNED_LICENSE_BYTES_B64)


def cache_document_bytes(
    *,
    version: str,
    commit: str = UPSTREAM_COMMIT,
) -> tuple[bytes, bytes]:
    """Return canonical cached document bytes plus their input JS pair."""

    commands_js = (
        b"var gCommands = "
        + json.dumps(
            [
                {
                    "name": "C",
                    "commands": [
                        dict(VERSION_COMMAND),
                        full_command("Zed", version),
                    ],
                }
            ]
        ).encode("utf-8")
        + b";\n"
    )
    metadata = snapshot_metadata(
        provider_version=version,
        distribution=CACHE_DISTRIBUTION,
        package_path="schema-cache/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag=version,
        upstream_commit=commit,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": sha256_hex(commands_js),
            "common_schema_definitions.js": sha256_hex(COMMON_JS),
        },
    )
    document = transform_inputs(commands_js, COMMON_JS, metadata=metadata)
    return serialize_tapir_snapshot(document), commands_js


class FakeGitHub:
    """Route-bound fake transport recording every requested URL."""

    def __init__(self, routes: Mapping[str, FetchOutcome]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []

    async def __call__(self, url: str, maximum: int) -> FetchOutcome:
        del maximum
        self.calls.append(url)
        outcome = self.routes.get(url, FetchOutcome(status=404, headers={}, body=b""))
        return outcome


def routes_for(
    version: str,
    *,
    commit: str = NEW_COMMIT,
    etag: str | None = '"etag-new"',
    commands_js: bytes | None = None,
) -> dict[str, FetchOutcome]:
    js = commands_js if commands_js is not None else source_js()
    return {
        releases_page_one_url(): FetchOutcome(
            status=200,
            headers={"ETag": etag} if etag else {},
            body=json.dumps(
                [{"tag_name": version, "draft": False, "prerelease": False, "assets": []}]
            ).encode("utf-8"),
        ),
        git_ref_url(version): FetchOutcome(
            status=200,
            headers={},
            body=json.dumps({"object": {"sha": commit, "type": "commit"}}).encode("utf-8"),
        ),
        raw_source_url(commit, COMMAND_DEFINITIONS_PATH): FetchOutcome(
            status=200, headers={}, body=js
        ),
        raw_source_url(commit, COMMON_SCHEMA_DEFINITIONS_PATH): FetchOutcome(
            status=200, headers={}, body=COMMON_JS
        ),
        raw_source_url(commit, LICENSE_PATH): FetchOutcome(
            status=200, headers={}, body=LICENSE_BYTES
        ),
    }


async def wait_for_claimed_lease(task: asyncio.Future[Any]) -> None:
    """Bound lease polling and surface an attempt that exits before claiming."""

    async with asyncio.timeout(5.0):
        while cs.load_check_state().lease is None:
            if task.done():
                raise AssertionError(f"update exited before claiming lease: {task.result()!r}")
            await asyncio.sleep(0.01)


@pytest.fixture()
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "cache-root"
    root.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    monkeypatch.delenv("ARCHICAD_MCP_AUTO_UPDATE", raising=False)
    return root


@pytest.fixture()
def packaged() -> upd.PackagedTapir:
    return upd.load_packaged_tapir()


IDENTITY = TapirSnapshotIdentity(
    version="1.5.8",
    distribution="packaged",
    package_path="archicad_mcp/schemas/tapir.json",
    upstream_repository=UPSTREAM_REPOSITORY,
    upstream_tag="1.5.8",
    upstream_commit=UPSTREAM_COMMIT,
    license_name="MIT",
    source_sha256="1" * 64,
    input_hashes={"command_definitions.js": "2" * 64, "common_schema_definitions.js": "3" * 64},
)


def identity_for(
    version: str, commit: str, source: str, inputs: dict[str, str]
) -> TapirSnapshotIdentity:
    return TapirSnapshotIdentity(
        version=version,
        distribution=CACHE_DISTRIBUTION,
        package_path="schema-cache/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag=version,
        upstream_commit=commit,
        license_name="MIT",
        source_sha256=source,
        input_hashes=inputs,
    )


@pytest.mark.parametrize(
    ("candidate", "cache", "expected_decision", "may_cache"),
    [
        ("1.6.0", None, "accept-newer", True),
        ("1.6.0", "1.5.9", "accept-newer", True),
        ("2.0.0", "1.9.0", "accept-newer", True),
        ("1.5.7", None, "upstream-rollback", False),
        ("1.6.0", "1.7.0", "upstream-rollback", False),
    ],
)
def test_acceptance_lattice_version_rules(
    candidate: str,
    cache: str | None,
    expected_decision: str,
    may_cache: bool,
) -> None:
    cached = (
        None
        if cache is None
        else identity_for(cache, NEW_COMMIT, "9" * 64, {"command_definitions.js": "8" * 64})
    )
    decision = upd.decide_acceptance(
        candidate_version=candidate,
        candidate_commit=NEW_COMMIT,
        candidate_source_sha256="4" * 64,
        candidate_input_hashes={"command_definitions.js": "5" * 64},
        packaged=IDENTITY,
        cache=cached,
    )
    assert decision.decision == expected_decision
    assert decision.may_cache is may_cache


def test_equal_version_same_identity_is_replay_and_never_cached() -> None:
    decision = upd.decide_acceptance(
        candidate_version=IDENTITY.version,
        candidate_commit=IDENTITY.upstream_commit,
        candidate_source_sha256=IDENTITY.source_sha256,
        candidate_input_hashes=dict(IDENTITY.input_hashes),
        packaged=IDENTITY,
        cache=None,
    )
    assert (decision.decision, decision.may_cache) == ("replay-current", False)


def test_packaged_replay_ignores_distribution_dependent_snapshot_hash() -> None:
    # Regression: the packaged full snapshot hash depends on distribution
    # metadata, so equal commit plus equal inputs must be replay/current even
    # when the serialized bytes differ.
    decision = upd.decide_acceptance(
        candidate_version=IDENTITY.version,
        candidate_commit=IDENTITY.upstream_commit,
        candidate_source_sha256="d" * 64,
        candidate_input_hashes=dict(IDENTITY.input_hashes),
        packaged=IDENTITY,
        cache=None,
    )
    assert (decision.decision, decision.may_cache) == ("replay-current", False)


def test_cached_replay_requires_exact_cached_canonical_source_hash() -> None:
    active_inputs = {
        "command_definitions.js": "5" * 64,
        "common_schema_definitions.js": "6" * 64,
    }
    cached = identity_for("1.6.0", UPSTREAM_COMMIT, "4" * 64, active_inputs)
    same_bytes_hash = upd.decide_acceptance(
        candidate_version="1.6.0",
        candidate_commit=cached.upstream_commit,
        candidate_source_sha256="4" * 64,
        candidate_input_hashes=dict(active_inputs),
        packaged=IDENTITY,
        cache=cached,
    )
    assert (same_bytes_hash.decision, same_bytes_hash.may_cache) == ("replay-current", False)
    drifted_bytes = upd.decide_acceptance(
        candidate_version="1.6.0",
        candidate_commit=cached.upstream_commit,
        candidate_source_sha256="e" * 64,
        candidate_input_hashes=dict(active_inputs),
        packaged=IDENTITY,
        cache=cached,
    )
    assert drifted_bytes.decision == "upstream-equivocation"


def test_moved_commit_is_always_equivocation_even_with_matching_hashes() -> None:
    moved_inputs = dict(IDENTITY.input_hashes)
    for active_cache in (
        None,
        identity_for("1.7.0", UPSTREAM_COMMIT, "4" * 64, moved_inputs),
    ):
        if active_cache is not None and active_cache.version <= IDENTITY.version:
            continue
        decision = upd.decide_acceptance(
            candidate_version=("1.7.0" if active_cache is not None else IDENTITY.version),
            candidate_commit=NEW_COMMIT,
            candidate_source_sha256=(
                "4" * 64 if active_cache is not None else IDENTITY.source_sha256
            ),
            candidate_input_hashes=dict(moved_inputs),
            packaged=IDENTITY,
            cache=active_cache,
        )
        assert decision.decision == "upstream-equivocation"


@pytest.mark.parametrize(
    ("commit", "source", "inputs"),
    [
        (NEW_COMMIT, "4" * 64, {"command_definitions.js": "5" * 64}),
        (UPSTREAM_COMMIT, "e" * 64, {"command_definitions.js": "5" * 64}),
        (
            UPSTREAM_COMMIT,
            "4" * 64,
            {"command_definitions.js": "5" * 64, "common_schema_definitions.js": "d" * 64},
        ),
    ],
)
def test_equal_version_with_any_drift_is_equivocation(
    commit: str, source: str, inputs: dict[str, str]
) -> None:
    active_inputs = {
        "command_definitions.js": "5" * 64,
        "common_schema_definitions.js": "6" * 64,
    }
    cached = identity_for("1.6.0", UPSTREAM_COMMIT, "4" * 64, active_inputs)
    decision = upd.decide_acceptance(
        candidate_version="1.6.0",
        candidate_commit=commit,
        candidate_source_sha256=source,
        candidate_input_hashes=inputs,
        packaged=IDENTITY,
        cache=cached,
    )
    assert decision.decision == "upstream-equivocation"
    assert decision.may_cache is False


def test_package_wins_tie_when_cache_is_older_or_equal(packaged: upd.PackagedTapir) -> None:
    newer_packaged = dataclasses.replace(
        packaged.identity,
        version="2.0.0",
        upstream_tag="2.0.0",
        source_sha256="c" * 64,
    )
    older_cache = identity_for("1.9.0", NEW_COMMIT, "b" * 64, {"command_definitions.js": "a" * 64})
    equal_candidate = upd.decide_acceptance(
        candidate_version="2.0.0",
        candidate_commit=newer_packaged.upstream_commit,
        candidate_source_sha256=newer_packaged.source_sha256,
        candidate_input_hashes=dict(newer_packaged.input_hashes),
        packaged=newer_packaged,
        cache=older_cache,
    )
    assert equal_candidate.decision == "replay-current"


def test_corrupt_cache_falls_back_to_the_packaged_floor(
    packaged: upd.PackagedTapir,
) -> None:
    decision = upd.decide_acceptance(
        candidate_version="1.6.0",
        candidate_commit=NEW_COMMIT,
        candidate_source_sha256="4" * 64,
        candidate_input_hashes={"command_definitions.js": "5" * 64},
        packaged=packaged.identity,
        cache=None,
    )
    assert decision.decision == "accept-newer"


async def test_happy_path_accepts_newer_release_atomically(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    package_before = packaged.payload
    accepted: list[str] = []
    outcome = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.6.0")),
        manual=True,
        mode="automatic",
        auto_enabled=True,
        on_accepted=lambda snapshot: accepted.append(snapshot.provider_version),
    )
    assert outcome == upd.UpdateOutcome("updated", None)
    result = cs.read_cached_snapshot()
    assert result.cached is not None and result.error_code is None
    assert result.cached.version == "1.6.0"
    assert accepted == ["1.6.0"]
    state = cs.load_check_state()
    assert state.last_outcome == "updated"
    assert state.release_etag == '"etag-new"'
    assert state.lease is None
    assert upd.load_packaged_tapir().payload == package_before, "package must never be written"


async def test_304_records_current_without_touching_cache(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    payload, _ = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), payload)
    cs.store_check_state(cs.CheckState(release_etag='"stored"'))
    fake = FakeGitHub({releases_page_one_url(): FetchOutcome(status=304, headers={}, body=None)})
    outcome = await upd.run_update_check(packaged, fetch=fake, manual=True)
    assert outcome.status == "current"
    assert fake.calls == [releases_page_one_url()]
    assert cs.snapshot_path().read_bytes() == payload
    state = cs.load_check_state()
    assert state.last_outcome == "current"
    assert state.release_etag == '"stored"'
    assert state.last_check_wall is not None


async def test_rollback_refuses_and_retains_active_cache(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    active, _ = cache_document_bytes(version="1.7.0")
    cs.atomically_replace(cs.snapshot_path(), active)
    outcome = await upd.run_update_check(
        packaged, fetch=FakeGitHub(routes_for("1.6.0")), manual=True
    )
    assert outcome == upd.UpdateOutcome("upstream-rollback", "upstream-rollback")
    assert cs.snapshot_path().read_bytes() == active


async def test_moved_same_version_tag_is_upstream_equivocation(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    active, _active_js = cache_document_bytes(version="1.6.0", commit=UPSTREAM_COMMIT)
    cs.atomically_replace(cs.snapshot_path(), active)
    moved_routes = routes_for("1.6.0", commit=NEW_COMMIT)
    outcome = await upd.run_update_check(packaged, fetch=FakeGitHub(moved_routes), manual=True)
    assert outcome == upd.UpdateOutcome("upstream-equivocation", "upstream-equivocation")
    assert cs.snapshot_path().read_bytes() == active


async def test_replay_of_identical_cache_keeps_state_stable(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    active, active_js = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), active)
    identity = cs.read_cached_snapshot().cached
    assert identity is not None
    same_source = routes_for("1.6.0", commit=identity.commit, commands_js=active_js)
    outcome = await upd.run_update_check(packaged, fetch=FakeGitHub(same_source), manual=True)
    assert outcome.status == "current"
    assert cs.snapshot_path().read_bytes() == active


async def test_corrupted_cache_self_heals_on_next_acceptance(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    cs.atomically_replace(cs.snapshot_path(), b"{corrupt garbage")
    outcome = await upd.run_update_check(
        packaged, fetch=FakeGitHub(routes_for("1.6.0")), manual=True
    )
    assert outcome.status == "updated"
    healed = cs.read_cached_snapshot()
    assert healed.cached is not None and healed.error_code is None


async def test_ttl_blocks_automatic_but_not_manual_checks(
    cache_env: Path, packaged: upd.PackagedTapir, monkeypatch: pytest.MonkeyPatch
) -> None:
    cs.store_check_state(cs.CheckState(last_check_wall=time.time()))
    blocking = FakeGitHub({})

    blocked = await upd.run_update_check(
        packaged,
        fetch=blocking,
        manual=False,
        mode="automatic",
        auto_enabled=True,
    )
    assert blocked.status == "current"
    assert blocking.calls == []

    manual_outcome = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.6.0")),
        manual=True,
        mode="automatic",
        auto_enabled=True,
    )
    assert manual_outcome.status == "updated"
    del monkeypatch


async def test_offline_forbids_both_auto_and_manual_before_any_network(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    silent = FakeGitHub({})
    auto = await upd.run_update_check(packaged, fetch=silent, manual=False, mode="offline")
    manual = await upd.run_update_check(packaged, fetch=silent, manual=True, mode="offline")
    assert auto == upd.UpdateOutcome("offline-network-forbidden", None)
    assert manual == upd.UpdateOutcome("offline-network-forbidden", None)
    assert silent.calls == []
    assert not cs.snapshot_path().exists()


async def test_auto_disabled_short_circuits_without_network(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    fake = FakeGitHub({})
    outcome = await upd.run_update_check(
        packaged, fetch=fake, manual=False, mode="automatic", auto_enabled=False
    )
    assert outcome.status == "auto-disabled"
    assert fake.calls == []


async def test_fresh_foreign_lease_blocks_and_stale_lease_is_taken_over(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    fresh = cs.CacheLease(id=str(uuid.uuid4()), wall=time.time(), monotonic=time.monotonic())
    cs.store_check_state(cs.CheckState(lease=fresh))
    fake = FakeGitHub(routes_for("1.6.0"))
    blocked = await upd.run_update_check(packaged, fetch=fake, manual=True)
    assert blocked.status == "in-flight"

    stale_delta = cs.LEASE_STALE_SECONDS + 10
    cs.store_check_state(
        cs.CheckState(
            lease=cs.CacheLease(
                id=str(uuid.uuid4()),
                wall=time.time() - stale_delta,
                monotonic=time.monotonic() - stale_delta,
            )
        )
    )
    outcome = await upd.run_update_check(packaged, fetch=fake, manual=True)
    assert outcome.status == "updated"


def test_env_grammar_functions_accept_only_pinned_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value, expected in [(None, True), ("1", True), ("0", False)]:
        if value is None:
            monkeypatch.delenv("ARCHICAD_MCP_AUTO_UPDATE", raising=False)
        else:
            monkeypatch.setenv("ARCHICAD_MCP_AUTO_UPDATE", value)
        assert upd.auto_update_enabled() is expected
    with pytest.raises(ValueError):
        upd.auto_update_enabled("yes")

    monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    assert upd.offline_mode_enabled() is False
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "1")
    assert upd.offline_mode_enabled() is True
    with pytest.raises(ValueError):
        upd.offline_mode_enabled("true")


async def test_cancellation_clears_the_lease_and_permits_no_late_write(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    release_gate = asyncio.Event()

    async def hanging_fetch(url: str, maximum: int) -> FetchOutcome:
        del url, maximum
        await release_gate.wait()
        return FetchOutcome(status=500, headers={}, body=b"")

    task = asyncio.ensure_future(upd.run_update_check(packaged, fetch=hanging_fetch, manual=True))
    await wait_for_claimed_lease(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = cs.load_check_state()
    assert state.lease is None, "cancellation must clear our lease"
    assert not cs.snapshot_path().exists(), "no cache write may follow cancellation"
    # Any late fetch completion must not mutate durable state either.
    release_gate.set()
    await asyncio.sleep(0.05)
    assert not cs.snapshot_path().exists()
    assert cs.load_check_state().lease is None


async def test_attempt_timeout_records_failure_and_clears_the_lease(
    cache_env: Path,
    packaged: upd.PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upd, "ATTEMPT_TIMEOUT_SECONDS", 0.05)

    async def slow_fetch(url: str, maximum: int) -> FetchOutcome:
        del url, maximum
        await asyncio.sleep(5.0)
        raise AssertionError("must be cancelled by the attempt timeout")

    outcome = await upd.run_update_check(packaged, fetch=slow_fetch, manual=True)
    assert outcome == upd.UpdateOutcome("failed", "attempt-timeout")
    state = cs.load_check_state()
    assert state.lease is None
    assert state.last_outcome == "failed"


async def test_concurrent_cache_change_blocks_acceptance_without_overwrite(
    cache_env: Path, packaged: upd.PackagedTapir, monkeypatch: pytest.MonkeyPatch
) -> None:
    active, _ = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), active)

    real_read = upd.read_cached_snapshot

    def racing_read() -> cs.CacheSnapshotResult:
        result = real_read()
        if result.cached is not None:
            raced = cs.CachedSnapshot(
                payload=result.cached.payload,
                version=result.cached.version,
                tag=result.cached.tag,
                commit=result.cached.commit,
                source_sha256="e" * 64,
                input_hashes=dict(result.cached.input_hashes),
                observed_majors=result.cached.observed_majors,
                observed_platforms=result.cached.observed_platforms,
                snapshot=result.cached.snapshot,
            )
            return cs.CacheSnapshotResult(raced, None)
        return result

    newer_routes = routes_for("2.0.0")
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(upd, "read_cached_snapshot", racing_read)
        outcome = await upd.run_update_check(packaged, fetch=FakeGitHub(newer_routes), manual=True)
    assert outcome == upd.UpdateOutcome("failed", "cache-concurrent-change")
    assert cs.snapshot_path().read_bytes() == active, "CAS refusal must not overwrite"
    assert cs.load_check_state().last_outcome == "failed"


def test_schemas_status_reports_local_state_only(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    cs.atomically_replace(cs.snapshot_path(), cache_document_bytes(version="1.6.0")[0])
    report = upd.schemas_status(packaged, mode="automatic", auto_enabled=True)
    assert report["packaged_version"] == "1.5.8"
    assert report["active"] == {"origin": "cache", "version": "1.6.0"}
    assert report["cache"]["present"] is True
    assert report["check_state"]["ttl_seconds"] == cs.TTL_SECONDS
    assert "payload" not in json.dumps(report)
    offline_report = upd.schemas_status(packaged, mode="offline", auto_enabled=True)
    assert offline_report["offline"] is True


def test_reset_schema_cache_clears_durable_state_under_lock(cache_env: Path) -> None:
    payload, _ = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), payload)
    cs.store_check_state(cs.CheckState(last_outcome="updated"))
    removed = upd.reset_schema_cache()
    assert sorted(removed) == ["check-state.json", "tapir.json"]
    assert not cs.snapshot_path().exists()
    assert cs.cache_lock_path().exists()


def test_reset_returns_bounded_lock_busy_instead_of_hanging(cache_env: Path) -> None:
    # Regression: reset once blocked indefinitely on a busy native lock.
    started = threading.Event()

    def hold() -> None:
        held = cs.acquire_cache_lock(timeout=30.0)
        try:
            started.set()
            time.sleep(8.0)
        finally:
            cs.release_cache_lock(held)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert started.wait(5.0)
        began = time.monotonic()
        with pytest.raises(cs.CacheStoreError) as caught:
            upd.reset_schema_cache()
        assert caught.value.code == "lock-busy"
        elapsed = time.monotonic() - began
        assert elapsed < upd.RESET_LOCK_TIMEOUT_SECONDS + 3.0
    finally:
        thread.join(timeout=15.0)
    removed = upd.reset_schema_cache()
    assert isinstance(removed, list)


async def test_200_listing_etag_persists_across_terminal_outcomes(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    # Regression: only the updated outcome once persisted the fresh ETag.
    active, active_js = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), active)
    cs.store_check_state(cs.CheckState(release_etag='"etag-old"'))

    moved = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.6.0", commit=NEW_COMMIT, etag='"etag-moved"')),
        manual=True,
    )
    assert moved.status == "upstream-equivocation"
    assert cs.load_check_state().release_etag == '"etag-moved"'

    older = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.5.7", etag='"etag-old-release"')),
        manual=True,
    )
    assert older.status == "upstream-rollback"
    assert cs.load_check_state().release_etag == '"etag-old-release"'

    replay_routes = routes_for("1.6.0", commit=UPSTREAM_COMMIT, commands_js=active_js)
    replay_routes[releases_page_one_url()] = FetchOutcome(
        status=200,
        headers={"ETag": '"etag-replay"'},
        body=replay_routes[releases_page_one_url()].body,
    )
    replay = await upd.run_update_check(packaged, fetch=FakeGitHub(replay_routes), manual=True)
    assert replay.status == "current"
    assert cs.load_check_state().release_etag == '"etag-replay"'


async def test_failed_fetches_never_erase_the_stored_etag(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    cs.store_check_state(cs.CheckState(release_etag='"keep"'))
    routes = routes_for("1.6.0", etag='"transient"')
    del routes[raw_source_url(NEW_COMMIT, LICENSE_PATH)]
    outcome = await upd.run_update_check(packaged, fetch=FakeGitHub(routes), manual=True)
    assert outcome.status == "failed"
    assert cs.load_check_state().release_etag == '"keep"'


def _tampering_fetch(
    routes: Mapping[str, FetchOutcome], tamper: Callable[[], None]
) -> Callable[[str, int], Awaitable[FetchOutcome]]:
    state = {"done": False}

    async def fetch(url: str, maximum: int) -> FetchOutcome:
        del maximum
        if not state["done"]:
            state["done"] = True
            tamper()
        return routes.get(url, FetchOutcome(status=404, headers={}, body=b""))

    return fetch


async def test_cleared_lease_refuses_completion_and_writes_nothing(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    def clear_lease() -> None:
        cs.check_state_path().unlink(missing_ok=True)

    fetch = _tampering_fetch(routes_for("1.6.0"), clear_lease)
    outcome = await upd.run_update_check(packaged, fetch=fetch, manual=True)
    assert outcome == upd.UpdateOutcome("failed", "lease-takeover")
    assert not cs.snapshot_path().exists()
    assert cs.load_check_state().lease is None
    assert cs.load_check_state().last_outcome is None


async def test_replaced_lease_refuses_completion(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    thief = str(uuid.uuid4())

    def replace_lease() -> None:
        cs.store_check_state(
            cs.CheckState(
                lease=cs.CacheLease(id=thief, wall=time.time(), monotonic=time.monotonic())
            )
        )

    fetch = _tampering_fetch(routes_for("1.6.0"), replace_lease)
    outcome = await upd.run_update_check(packaged, fetch=fetch, manual=True)
    assert outcome == upd.UpdateOutcome("failed", "lease-takeover")
    assert not cs.snapshot_path().exists()
    active_lease_after_replace = cs.load_check_state().lease
    assert active_lease_after_replace is not None
    assert active_lease_after_replace.id == thief


async def test_reset_racing_an_update_is_never_overwritten_later(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    payload, _ = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), payload)

    def reset_midflight() -> None:
        cs.check_state_path().unlink(missing_ok=True)
        cs.snapshot_path().unlink(missing_ok=True)

    fetch = _tampering_fetch(routes_for("2.0.0"), reset_midflight)
    outcome = await upd.run_update_check(packaged, fetch=fetch, manual=True)
    assert outcome.error == "lease-takeover"
    assert not cs.snapshot_path().exists(), "reset racing an update must win"
    state = cs.load_check_state()
    assert state.lease is None and state.last_outcome is None
    await asyncio.sleep(0.05)
    assert not cs.snapshot_path().exists(), "no late publication may follow"


async def test_busy_cache_lock_yields_bounded_lock_busy_outcome(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    started = threading.Event()

    def hold() -> None:
        held = cs.acquire_cache_lock(timeout=30.0)
        try:
            started.set()
            time.sleep(upd.STATE_LOCK_TIMEOUT_SECONDS + 3.0)
        finally:
            cs.release_cache_lock(held)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert started.wait(5.0)
        began = time.monotonic()
        outcome = await upd.run_update_check(
            packaged, fetch=FakeGitHub(routes_for("1.6.0")), manual=True
        )
        elapsed = time.monotonic() - began
    finally:
        thread.join(timeout=15.0)
    assert outcome.status == "failed"
    assert outcome.error == "lock-busy"
    assert elapsed >= upd.STATE_LOCK_TIMEOUT_SECONDS * 0.9
    assert elapsed < upd.STATE_LOCK_TIMEOUT_SECONDS + 5.0
    # Once the holder releases, checks work again.
    thread.join(timeout=20.0)
    recovered = await upd.run_update_check(
        packaged, fetch=FakeGitHub(routes_for("1.6.0")), manual=True
    )
    assert recovered.status == "updated"


async def test_cancellation_and_timeout_repetitions_never_mutate_late(
    cache_env: Path, packaged: upd.PackagedTapir, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Targeted repetitions: every cancelled or timed-out attempt must leave
    # no lease, no snapshot, and no late write when the fetch later completes.
    for round_index in range(4):
        gate: asyncio.Event = asyncio.Event()
        finished: asyncio.Event = asyncio.Event()

        async def hanging_fetch(
            url: str,
            maximum: int,
            *,
            gate: asyncio.Event = gate,
            finished: asyncio.Event = finished,
        ) -> FetchOutcome:
            del url, maximum
            await gate.wait()
            finished.set()
            return FetchOutcome(status=200, headers={}, body=b"[]")

        task = asyncio.ensure_future(
            upd.run_update_check(packaged, fetch=hanging_fetch, manual=True)
        )
        await wait_for_claimed_lease(task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cs.load_check_state().lease is None
        assert not cs.snapshot_path().exists()
        gate.set()
        for _ in range(200):
            if finished.is_set():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
        assert not cs.snapshot_path().exists(), f"late write in cancellation round {round_index}"
        assert cs.load_check_state().lease is None

    monkeypatch.setattr(upd, "ATTEMPT_TIMEOUT_SECONDS", 0.05)
    for round_index in range(3):

        async def slow_fetch(url: str, maximum: int) -> FetchOutcome:
            del url, maximum
            await asyncio.sleep(5.0)
            raise AssertionError("must be cancelled by the attempt timeout")

        outcome = await upd.run_update_check(packaged, fetch=slow_fetch, manual=True)
        assert outcome == upd.UpdateOutcome("failed", "attempt-timeout"), round_index
        state = cs.load_check_state()
        assert state.lease is None and state.last_outcome == "failed"
        assert not cs.snapshot_path().exists(), f"write survived timeout round {round_index}"


def test_foundation_modules_import_no_server_cli_config_or_signing() -> None:
    forbidden = (
        "archicad_mcp.server",
        "archicad_mcp.cli",
        "archicad_mcp.config",
        "archicad_mcp.models",
        "archicad_mcp.schemas.signed_manifest",
        "archicad_mcp.schemas.provenance",
        "archicad_mcp.schemas.source",
        "from scripts",
        "import scripts",
        "threading",
        "to_thread",
        "Timer(",
    )
    modules = [Path(inspect.getfile(module)) for module in (upd, cs)]
    import archicad_mcp.schemas.github_release as gh
    import archicad_mcp.schemas.semver as sv
    import archicad_mcp.schemas.tapir_source as ts

    modules.extend(Path(inspect.getfile(m)) for m in (gh, sv, ts))
    generator = Path(upd.__file__).parents[3] / "scripts" / "generate_tapir_snapshot.py"
    for path in [*modules, generator]:
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{path.name} must not reference {needle}"


def test_updater_exposes_direct_update_integration_surface() -> None:
    from archicad_mcp.schemas import github_release as gh
    from archicad_mcp.schemas import tapir_source as ts

    # Provenance constants must agree across the foundation modules; a drift
    # would make every candidate cache document fail closed at validation.
    assert gh.UPSTREAM_REPOSITORY_URL == ts.TAPIR_UPSTREAM_REPOSITORY
    from archicad_mcp.schemas.cache_store import CACHE_SNAPSHOT_RELATIVE_PATH

    assert CACHE_SNAPSHOT_RELATIVE_PATH == ts.CACHE_PACKAGE_PATH
    for name in (
        "run_update_check",
        "load_packaged_tapir",
        "schemas_status",
        "reset_schema_cache",
        "decide_acceptance",
        "auto_update_enabled",
        "offline_mode_enabled",
        "UpdateOutcome",
        "PackagedTapir",
    ):
        assert hasattr(upd, name), f"missing foundation API: {name}"


async def test_slow_claim_cannot_write_a_lease_after_attempt_deadline(
    cache_env: Path,
    packaged: upd.PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upd, "ATTEMPT_TIMEOUT_SECONDS", 0.1)
    real_load = cs.load_check_state

    def slow_load() -> cs.CheckState:
        time.sleep(0.2)
        return real_load()

    monkeypatch.setattr(upd, "load_check_state", slow_load)
    fake = FakeGitHub({})
    outcome = await upd.run_update_check(packaged, fetch=fake, manual=True)
    assert outcome == upd.UpdateOutcome("failed", "attempt-timeout")
    assert fake.calls == []
    assert cs.load_check_state().lease is None
    assert not cs.snapshot_path().exists()


async def test_sync_transform_delay_hits_attempt_deadline(
    cache_env: Path,
    packaged: upd.PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: a synchronous transform must not publish after the absolute
    # attempt deadline; the budget is re-checked before any mutation.
    monkeypatch.setattr(upd, "ATTEMPT_TIMEOUT_SECONDS", 0.3)
    real_transform = transform_inputs

    def slow_transform(*args: Any, **kwargs: Any) -> dict[str, Any]:
        time.sleep(0.6)
        return real_transform(*args, **kwargs)

    monkeypatch.setattr(upd, "transform_inputs", slow_transform)
    accepted: list[str] = []
    outcome = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.6.0")),
        manual=True,
        on_accepted=lambda snapshot: accepted.append(snapshot.provider_version),
    )
    assert outcome == upd.UpdateOutcome("failed", "attempt-timeout")
    assert not cs.snapshot_path().exists(), "late publication after expiry"
    assert accepted == [], "no callback may run after expiry"
    state = cs.load_check_state()
    assert state.last_outcome == "failed"
    assert state.lease is None or cs.lease_is_stale(
        state.lease, now_wall=time.time(), now_monotonic=time.monotonic()
    )
    await asyncio.sleep(0.1)
    assert not cs.snapshot_path().exists(), "no late write after drain"


async def test_cancellation_during_synchronous_transform_window(
    cache_env: Path,
    packaged: upd.PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A helper thread requests cancellation while the event loop is inside the
    # synchronous transform. The explicit post-transform checkpoint must then
    # deliver it before publication.
    monkeypatch.setattr(upd, "ATTEMPT_TIMEOUT_SECONDS", 30.0)
    transform_started = threading.Event()
    release_transform = threading.Event()
    helper_finished = threading.Event()
    real_transform = transform_inputs

    def blocking_transform(*args: Any, **kwargs: Any) -> dict[str, Any]:
        transform_started.set()
        if not release_transform.wait(5.0):
            raise RuntimeError("cancellation helper did not release transform")
        return real_transform(*args, **kwargs)

    monkeypatch.setattr(upd, "transform_inputs", blocking_transform)
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(
        upd.run_update_check(packaged, fetch=FakeGitHub(routes_for("1.6.0")), manual=True)
    )

    def request_cancellation() -> None:
        if transform_started.wait(5.0):
            loop.call_soon_threadsafe(task.cancel)
        release_transform.set()
        helper_finished.set()

    helper = threading.Thread(target=request_cancellation)
    helper.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_transform.set()
        helper.join(timeout=5.0)
    assert helper_finished.is_set()
    assert not helper.is_alive()
    assert not cs.snapshot_path().exists()
    assert cs.load_check_state().lease is None
    await asyncio.sleep(0)
    assert not cs.snapshot_path().exists(), "no late write after cancel"


async def test_failed_cas_retains_etag_and_next_200_retries(
    cache_env: Path,
    packaged: upd.PackagedTapir,
) -> None:
    # Negative control: a local completion failure retains the prior ETag so
    # the candidate is retried against a later 200 rather than suppressed.
    active, _active_js = cache_document_bytes(version="1.6.0")
    cs.atomically_replace(cs.snapshot_path(), active)
    cs.store_check_state(cs.CheckState(release_etag='"etag-old"'))

    real_read = upd.read_cached_snapshot

    def racing_read() -> cs.CacheSnapshotResult:
        result = real_read()
        if result.cached is not None:
            raced = cs.CachedSnapshot(
                payload=result.cached.payload,
                version=result.cached.version,
                tag=result.cached.tag,
                commit=result.cached.commit,
                source_sha256="e" * 64,
                input_hashes=dict(result.cached.input_hashes),
                observed_majors=result.cached.observed_majors,
                observed_platforms=result.cached.observed_platforms,
                snapshot=result.cached.snapshot,
            )
            return cs.CacheSnapshotResult(raced, None)
        return result

    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(upd, "read_cached_snapshot", racing_read)
        outcome = await upd.run_update_check(
            packaged,
            fetch=FakeGitHub(routes_for("2.0.0", etag='"etag-new"')),
            manual=True,
        )
    assert outcome == upd.UpdateOutcome("failed", "cache-concurrent-change")
    assert cs.snapshot_path().read_bytes() == active
    retained = cs.load_check_state().release_etag
    assert retained == '"etag-old"'

    fake304 = FakeGitHub({releases_page_one_url(): FetchOutcome(status=304, headers={}, body=None)})
    suppressed = await upd.run_update_check(packaged, fetch=fake304, manual=True)
    assert suppressed.status == "current"
    assert fake304.calls == [releases_page_one_url()]

    retry = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("2.0.0", etag='"etag-next"')),
        manual=True,
    )
    assert retry.status == "updated"
    assert cs.load_check_state().release_etag == '"etag-next"'


async def test_200_without_etag_clears_stored_validator(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    cs.store_check_state(cs.CheckState(release_etag='"old"'))
    outcome = await upd.run_update_check(
        packaged,
        fetch=FakeGitHub(routes_for("1.6.0", etag=None)),
        manual=True,
    )
    assert outcome.status == "updated"
    state = cs.load_check_state()
    assert state.release_etag is None


async def test_status_exposes_observed_assets_of_the_cached_release(
    cache_env: Path,
    packaged: upd.PackagedTapir,
) -> None:
    routes = routes_for("1.7.0")
    routes[releases_page_one_url()] = FetchOutcome(
        status=200,
        headers={"ETag": '"assets-etag"'},
        body=json.dumps(
            [
                {
                    "tag_name": "1.7.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {"name": "TapirAddOn_AC29_Mac.zip"},
                        {"name": "TapirAddOn_AC30_Win.apx"},
                        {"name": "source.zip"},
                    ],
                }
            ]
        ).encode("utf-8"),
    )
    outcome = await upd.run_update_check(packaged, fetch=FakeGitHub(routes), manual=True)
    assert outcome.status == "updated"
    cached = cs.read_cached_snapshot().cached
    assert cached is not None
    assert cached.observed_majors == (29, 30)
    assert cached.observed_platforms == ("macos", "windows")
    report = upd.schemas_status(packaged, mode="automatic", auto_enabled=True)
    assert report["cache"]["observed_assets"] == {
        "majors": [29, 30],
        "platforms": ["macos", "windows"],
    }


async def test_untrusted_error_codes_are_bounded_before_persistence(
    cache_env: Path, packaged: upd.PackagedTapir
) -> None:
    class OversizedDiagnostic(ValueError):
        code = "x" * 300

    async def fail_fetch(url: str, maximum: int) -> FetchOutcome:
        del url, maximum
        raise OversizedDiagnostic

    outcome = await upd.run_update_check(packaged, fetch=fail_fetch, manual=True)
    assert outcome == upd.UpdateOutcome("failed", "internal-error")
    state = cs.load_check_state()
    assert state.last_error == "internal-error"
    assert state.lease is None


def test_snapshot_projection_requires_user_cache_distribution(packaged: upd.PackagedTapir) -> None:
    assert upd.snapshot_from_payload(packaged.payload) is None
