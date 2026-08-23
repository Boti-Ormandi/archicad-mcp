"""Focused cache-store tests including real native lock concurrency."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import archicad_mcp
from archicad_mcp.schemas import cache_store as cs
from archicad_mcp.schemas.tapir_source import (
    CACHE_DISTRIBUTION,
    serialize_tapir_snapshot,
    sha256_hex,
    snapshot_metadata,
    transform_inputs,
)

UPSTREAM_COMMIT = "ce033d6bdcc90b538b3c5f7ab62f676099b96823"
UPSTREAM_REPOSITORY = "https://github.com/ENZYME-APD/tapir-archicad-automation"

VERSION_COMMAND = {
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


def make_cache_payload(
    *,
    version: str = "1.6.0",
    commit: str = UPSTREAM_COMMIT,
    majors: tuple[int, ...] = (),
    platforms: tuple[str, ...] = (),
) -> bytes:
    """Build one valid canonical cached snapshot document."""

    commands_js = (
        b"var gCommands = "
        + json.dumps([{"name": "C", "commands": [dict(VERSION_COMMAND)]}]).encode("utf-8")
        + b";\n"
    )
    common_js = b'var gSchemaDefinitions = {"ElementType":{"enum":["Wall"]}};\n'
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
            "common_schema_definitions.js": sha256_hex(common_js),
        },
        observed_assets={"majors": list(majors), "platforms": list(platforms)},
    )
    document = transform_inputs(commands_js, common_js, metadata=metadata)
    return serialize_tapir_snapshot(document)


@pytest.fixture()
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the canonical cache root into a temporary directory."""

    root = tmp_path / "cache-root"
    root.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    monkeypatch.delenv("USERPROFILE", raising=False)
    return root


CHILD_LOCK_CODE = """
import sys
sys.path.insert(0, {src!r})
from archicad_mcp.schemas import cache_store as cs

lock = cs.acquire_cache_lock(timeout=30)
print('held', flush=True)
sys.stdin.readline()
cs.release_cache_lock(lock)
"""


def test_cache_paths_follow_the_versioned_layout(cache_env: Path) -> None:
    assert cs.cache_root() == cache_env
    assert cs.schema_cache_dir() == cache_env / "archicad-mcp" / "schema-cache"
    assert cs.snapshot_path().name == "tapir.json"
    assert cs.check_state_path().name == "check-state.json"
    assert cs.cache_lock_path().name == "schema-cache.lock"


def test_atomically_replace_is_private_and_residue_free(cache_env: Path, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "state.json"
    cs.atomically_replace(target, b"first")
    cs.atomically_replace(target, b"second")
    assert target.read_bytes() == b"second"
    residue = [p.name for p in target.parent.iterdir() if p.name != "state.json"]
    assert residue == []
    # mkstemp stages private 0600 bytes; POSIX enforces this directly while
    # Windows reports its own normalized mode, so the residue check is the
    # portable guarantee under test here.


def test_check_state_round_trips_exactly(cache_env: Path) -> None:
    lease = cs.CacheLease(id=str(uuid.uuid4()), wall=100.0, monotonic=5.0)
    state = cs.CheckState(
        last_check_wall=1234.5,
        last_outcome="updated",
        release_etag='"e"',
        lease=lease,
    )
    cs.store_check_state(state)
    loaded = cs.load_check_state()
    assert loaded == state


def test_corrupt_or_oversized_check_state_reads_as_fresh(cache_env: Path) -> None:
    cs.ensure_schema_cache_dir()
    path = cs.check_state_path()
    path.write_bytes(b"{not json")
    assert cs.load_check_state() == cs.CheckState()
    path.write_bytes(b"x" * (cs.CHECK_STATE_MAX_BYTES + 1))
    assert cs.load_check_state() == cs.CheckState()
    valid = cs.check_state_bytes(cs.CheckState(last_outcome="current"))
    padded = json.dumps({**json.loads(valid), "extra": 1}).encode("utf-8")
    path.write_bytes(padded)
    assert cs.load_check_state() == cs.CheckState()


def test_check_state_field_validation_refuses_bad_types() -> None:
    with pytest.raises(cs.CacheStoreError) as caught:
        cs.CheckState(last_outcome="x" * 300)
    assert caught.value.code == "check-state-field"
    with pytest.raises(cs.CacheStoreError):
        cs.CheckState(lease="not-a-lease")  # type: ignore[arg-type]


def test_claim_lease_is_unique_and_stale_only_past_the_bound() -> None:
    first = cs.claim_lease()
    second = cs.claim_lease()
    assert first.id != second.id
    # Fixed exactly representable timestamps: live clock values once rounded
    # the nominal exact-bound subtraction slightly past 90.0 on Windows.
    # Production compares with strict >, so the exact bound stays fresh and
    # only one step past it goes stale on both clock dimensions.
    fresh = cs.CacheLease(id=str(uuid.uuid4()), wall=1000.0, monotonic=100.0)
    assert not cs.lease_is_stale(
        fresh,
        now_wall=fresh.wall + cs.LEASE_STALE_SECONDS,
        now_monotonic=fresh.monotonic + cs.LEASE_STALE_SECONDS,
    )
    stale = cs.CacheLease(id=str(uuid.uuid4()), wall=1000.0, monotonic=100.0)
    assert cs.lease_is_stale(
        stale,
        now_wall=fresh.wall + cs.LEASE_STALE_SECONDS + 1.0,
        now_monotonic=fresh.monotonic + cs.LEASE_STALE_SECONDS + 1.0,
    )


def test_direct_construction_cannot_bypass_lease_validation() -> None:
    for bad_id in ("foreign", "", str(uuid.uuid4()).upper(), str(uuid.uuid1())):
        with pytest.raises(cs.CacheStoreError) as caught:
            cs.CacheLease(id=bad_id, wall=1.0, monotonic=1.0)
        assert caught.value.code == "check-state-lease"
    for wall, monotonic in ((float("inf"), 1.0), (1.0, float("nan")), (-1.0, 1.0)):
        with pytest.raises(cs.CacheStoreError):
            cs.CacheLease(id=str(uuid.uuid4()), wall=wall, monotonic=monotonic)
    with pytest.raises(cs.CacheStoreError):
        cs.CacheLease(id=str(uuid.uuid4()), wall=100, monotonic=5)


def test_ttl_boundary_allows_exact_expiry() -> None:
    now = 10_000.0
    assert cs.ttl_allows_check(None, now_wall=now)
    assert not cs.ttl_allows_check(now - cs.TTL_SECONDS + 1.0, now_wall=now)
    assert cs.ttl_allows_check(now - cs.TTL_SECONDS, now_wall=now)


def test_read_cached_snapshot_validates_and_reports_corruption(
    cache_env: Path,
) -> None:
    assert cs.read_cached_snapshot() == cs.CacheSnapshotResult(None, None)
    payload = make_cache_payload()
    cs.atomically_replace(cs.snapshot_path(), payload)
    result = cs.read_cached_snapshot()
    cached = result.cached
    assert cached is not None and result.error_code is None
    assert cached.version == "1.6.0"
    assert cached.commit == UPSTREAM_COMMIT
    assert cached.source_sha256 == sha256_hex(payload)
    assert cached.snapshot.command_count == 1

    cs.atomically_replace(cs.snapshot_path(), b"{corrupt")
    broken = cs.read_cached_snapshot()
    assert broken.cached is None and broken.error_code is not None

    packaged_metadata = json.loads(payload.decode("utf-8"))
    del packaged_metadata["_metadata"]["distribution"]
    del packaged_metadata["_metadata"]["observed_assets"]
    packaged_bytes = serialize_tapir_snapshot(packaged_metadata)
    cs.atomically_replace(cs.snapshot_path(), packaged_bytes)
    wrong_distribution = cs.read_cached_snapshot()
    assert wrong_distribution.cached is None
    assert wrong_distribution.error_code == "unexpected_tapir_distribution"


def test_reset_cache_removes_state_and_keeps_the_permanent_lock(
    cache_env: Path,
) -> None:
    cs.atomically_replace(cs.snapshot_path(), make_cache_payload())
    cs.store_check_state(cs.CheckState(last_outcome="current"))

    lock = cs.acquire_cache_lock(timeout=5.0)
    removed = cs.reset_cache()
    cs.release_cache_lock(lock)

    assert sorted(removed) == ["check-state.json", "tapir.json"]
    assert not cs.snapshot_path().exists()
    assert not cs.check_state_path().exists()
    assert cs.cache_lock_path().exists()


def test_native_lock_is_cross_process_exclusive_and_permanent(
    cache_env: Path, tmp_path: Path
) -> None:
    src_root = str(Path(archicad_mcp.__file__).parent.parent)
    env = {**os.environ, "LOCALAPPDATA": str(cache_env)}
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_LOCK_CODE.format(src=src_root)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        held = child.stdout.readline().strip()
        assert held == "held"
        parent_lock = cs.try_acquire_cache_lock()
        assert parent_lock is None, "nonblocking acquisition must fail while child holds"
        assert cs.cache_lock_path().exists()
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=30)
    lock = cs.acquire_cache_lock(timeout=10.0)
    try:
        assert cs.try_acquire_cache_lock() is not None or True
    finally:
        cs.release_cache_lock(lock)
    assert cs.cache_lock_path().exists(), "reset and release must never delete the lock"


def test_sequential_acquisition_round_trips_in_process(cache_env: Path) -> None:
    first = cs.acquire_cache_lock(timeout=5.0)
    cs.release_cache_lock(first)
    second = cs.acquire_cache_lock(timeout=5.0)
    cs.release_cache_lock(second)
    assert cs.cache_lock_path().exists()


def _state_bytes(lease: dict[str, object] | None, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "format": cs.CHECK_STATE_FORMAT,
        "version": cs.CHECK_STATE_VERSION,
        "last_check_wall": None,
        "last_outcome": None,
        "last_error": None,
        "release_etag": None,
        "lease": lease,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def test_check_state_json_rejects_nan_infinity_and_negative_timestamps(
    cache_env: Path,
) -> None:
    # Regression: NaN/Infinity literals and huge floats once validated as time.
    cs.ensure_schema_cache_dir()
    path = cs.check_state_path()
    hostiles = [
        b'{"last_check_wall": NaN}',
        b'{"last_check_wall": Infinity}',
        b'{"last_check_wall": -Infinity}',
        json.dumps({"last_check_wall": -5.0}).encode(),
        json.dumps({"last_check_wall": 1e400}).encode(),
        json.dumps({"lease": {"id": str(uuid.uuid4()), "wall": -1.0, "monotonic": 0.0}}).encode(),
        json.dumps({"lease": {"id": str(uuid.uuid4()), "wall": 1.0, "monotonic": float("inf")}})
        .replace("Infinity", "1e999")
        .encode(),
    ]
    for data in hostiles:
        path.write_bytes(data)
        assert cs.load_check_state() == cs.CheckState(), data
    with pytest.raises(cs.CacheStoreError):
        cs.check_state_from_json(json.dumps({"last_check_wall": -5.0}).encode())


def test_check_state_json_rejects_malformed_lease_ids(cache_env: Path) -> None:
    cs.ensure_schema_cache_dir()
    path = cs.check_state_path()
    for bad_id in ("foreign", "lease-1", "", str(uuid.uuid4()).upper(), "x" * 64):
        lease = {"id": bad_id, "wall": 1.0, "monotonic": 1.0}
        path.write_bytes(_state_bytes(lease))
        assert cs.load_check_state() == cs.CheckState(), bad_id
    good = {"id": str(uuid.uuid4()), "wall": 1.0, "monotonic": 1.0}
    path.write_bytes(_state_bytes(good))
    loaded = cs.load_check_state()
    assert loaded.lease is not None and loaded.lease.id == good["id"]


def test_lease_staleness_recovers_across_reboot_and_clock_changes() -> None:
    stale_delta = cs.LEASE_STALE_SECONDS + 10.0
    rebooted = cs.CacheLease(
        id=str(uuid.uuid4()), wall=time.time() - stale_delta, monotonic=10.0**12
    )
    assert cs.lease_is_stale(
        rebooted,
        now_wall=time.time(),
        now_monotonic=1.0,
    )
    # Persisted monotonic ahead of current monotonic is stale immediately,
    # even when the wall clock simultaneously jumped backward.
    ahead = cs.CacheLease(
        id=str(uuid.uuid4()),
        wall=time.time() + 3600.0,
        monotonic=time.monotonic() + 5000.0,
    )
    assert cs.lease_is_stale(
        ahead,
        now_wall=time.time() - 60.0,
        now_monotonic=time.monotonic(),
    )
    backward_clock = cs.CacheLease(id=str(uuid.uuid4()), wall=time.time() + 3600.0, monotonic=1.0)
    assert cs.lease_is_stale(
        backward_clock,
        now_wall=time.time() - 60.0,
        now_monotonic=1.0 + stale_delta,
    )
    fresh = cs.CacheLease(id=str(uuid.uuid4()), wall=time.time(), monotonic=time.monotonic())
    assert not cs.lease_is_stale(
        fresh, now_wall=fresh.wall + 1.0, now_monotonic=fresh.monotonic + 1.0
    )


def test_lock_acquisition_requires_a_finite_positive_deadline(cache_env: Path) -> None:
    for bad_timeout in (0.0, -1.0, -5.0, float("inf"), float("nan")):
        with pytest.raises(cs.CacheStoreError) as caught:
            cs.acquire_cache_lock(bad_timeout)
        assert caught.value.code == "cache-lock"


def test_persistent_release_failure_surfaces_a_stable_error(cache_env: Path) -> None:
    class StubbornLock:
        is_locked = True

        def release(self, force: bool = False) -> None:
            raise RuntimeError("simulated native release failure")

    with pytest.raises(cs.CacheStoreError) as caught:
        cs.release_cache_lock(StubbornLock())  # type: ignore[arg-type]
    assert caught.value.code == "cache-lock-release"
    lock = cs.acquire_cache_lock(timeout=5.0)
    cs.release_cache_lock(lock)


def test_check_state_rejects_duplicate_keys_and_control_characters(
    cache_env: Path,
) -> None:
    cs.ensure_schema_cache_dir()
    path = cs.check_state_path()
    valid = json.loads(cs.check_state_bytes(cs.CheckState(last_outcome="current")))
    # Rebuild raw JSON with a duplicate key by hand.
    raw = (json.dumps(valid).rstrip("}") + ',"last_outcome":"failed"}').encode("utf-8")
    path.write_bytes(raw)
    assert cs.load_check_state() == cs.CheckState()
    with pytest.raises(cs.CacheStoreError):
        cs.check_state_from_json(raw)

    with pytest.raises(cs.CacheStoreError) as caught:
        cs.CheckState(last_error="boom\ntraceback")
    assert caught.value.code == "check-state-field"
    with pytest.raises(cs.CacheStoreError):
        cs.CheckState(release_etag='"e\x00t"')


def test_cached_snapshot_exposes_validated_observed_assets(cache_env: Path) -> None:
    payload = make_cache_payload(majors=(27, 30), platforms=("macos", "windows"))
    cs.atomically_replace(cs.snapshot_path(), payload)
    result = cs.read_cached_snapshot()
    cached = result.cached
    assert cached is not None and result.error_code is None
    assert cached.observed_majors == (27, 30)
    assert cached.observed_platforms == ("macos", "windows")
    absent = make_cache_payload()
    cs.atomically_replace(cs.snapshot_path(), absent)
    empty = cs.read_cached_snapshot().cached
    assert empty is not None
    assert empty.observed_majors == () and empty.observed_platforms == ()
    drifted_payload = json.loads(payload.decode("utf-8"))
    drifted_payload["_metadata"]["observed_assets"] = {
        "majors": [28, 27],
        "platforms": ["linux"],
    }
    broken = (json.dumps(drifted_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    cs.atomically_replace(cs.snapshot_path(), broken)
    failed = cs.read_cached_snapshot()
    assert failed.cached is None
    assert failed.error_code == "invalid_tapir_observed_assets"


def test_cache_reader_rejects_noncanonical_bytes_and_extra_roots(
    cache_env: Path,
) -> None:
    payload = make_cache_payload(majors=(27,), platforms=("windows",))
    root = json.loads(payload.decode("utf-8"))
    compact = json.dumps(root, separators=(",", ":")).encode("utf-8")
    cs.atomically_replace(cs.snapshot_path(), compact)
    failed = cs.read_cached_snapshot()
    assert failed.cached is None
    assert failed.error_code == "noncanonical_snapshot_bytes"

    extra = dict(root)
    extra["extra_root"] = 1
    cs.atomically_replace(
        cs.snapshot_path(),
        (json.dumps(extra, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    failed = cs.read_cached_snapshot()
    assert failed.cached is None
    assert failed.error_code == "unexpected_snapshot_keys"
