"""Direct Tapir snapshot update orchestration.

Pure updater foundation for the direct GitHub acquisition contract: bounded
stable-release checks, strict SemVer monotonicity with package-wins-ties, and
all durable mutation under the permanent native cache lock through a UUID
in-flight lease and compare-and-swap acceptance. One absolute monotonic
attempt deadline started before the lease claim bounds claim, network,
transform, registry construction, completion, and publication; synchronous
work re-checks cancellation and remaining budget before any mutation.
Network, parsing, and registry construction happen outside the OS lock. This
module imports no server, CLI, config, or models code; runtime integration
wires explicit inputs through.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Final, Literal, cast

import aiohttp
from filelock import BaseFileLock

from archicad_mcp.schemas.cache_store import (
    CACHE_SNAPSHOT_RELATIVE_PATH,
    LEASE_STALE_SECONDS,
    SNAPSHOT_MAX_BYTES,
    TTL_SECONDS,
    CachedSnapshot,
    CacheLease,
    CacheSnapshotResult,
    CacheStoreError,
    CheckState,
    acquire_cache_lock,
    atomically_replace,
    claim_lease,
    lease_is_stale,
    load_check_state,
    read_cached_snapshot,
    release_cache_lock,
    snapshot_path,
    store_check_state,
    try_acquire_cache_lock,
    ttl_allows_check,
)
from archicad_mcp.schemas.cache_store import (
    reset_cache as reset_cached_files,
)
from archicad_mcp.schemas.github_release import (
    ATTEMPT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    UPSTREAM_REPOSITORY_URL,
    FetchOutcome,
    GitHubReleaseError,
    TapirReleaseAcquisition,
    acquire_latest_stable_release,
    github_request_headers,
    minimal_request_headers,
    releases_page_one_url,
)
from archicad_mcp.schemas.registry import (
    ProviderSnapshot,
    SchemaRegistryError,
    load_provider_snapshot,
)
from archicad_mcp.schemas.semver import SemverValidationError, compare_semver
from archicad_mcp.schemas.tapir_source import (
    CACHE_DISTRIBUTION,
    PINNED_LICENSE_NAME,
    PINNED_LICENSE_SHA256,
    PROVIDER_IDENTITY,
    TapirSnapshotIdentity,
    TapirSnapshotMetadataError,
    identity_from_metadata,
    load_packaged_identity,
    load_tapir_identity,
    serialize_tapir_snapshot,
    sha256_hex,
    snapshot_metadata,
    transform_inputs,
    verify_license_identity,
)

logger = logging.getLogger(__name__)

UpdateMode = Literal["automatic", "offline"]

# Bounded nonblocking lock participation: every state transition retries
# native nonblocking acquisition on await points until min(per-lock bound,
# remaining attempt budget) instead of blocking a thread or detaching work
# that could mutate after cancellation.
LOCK_RETRY_INTERVAL_SECONDS: Final = 0.05
STATE_LOCK_TIMEOUT_SECONDS: Final = 10.0
RESET_LOCK_TIMEOUT_SECONDS: Final = 5.0
LEASE_CLEANUP_TIMEOUT_SECONDS: Final = 2.0

UpdateStatus = Literal[
    "updated",
    "current",
    "offline-network-forbidden",
    "auto-disabled",
    "in-flight",
    "upstream-rollback",
    "upstream-equivocation",
    "failed",
]

AcceptanceDecision = Literal[
    "accept-newer",
    "replay-current",
    "upstream-rollback",
    "upstream-equivocation",
]


@dataclass(frozen=True, slots=True)
class UpdateOutcome:
    """One bounded update-check result without raw exception or secret text."""

    status: UpdateStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Acceptance:
    """The pure monotonic decision for one candidate against one active identity."""

    decision: AcceptanceDecision
    may_cache: bool


def decide_acceptance(
    *,
    candidate_version: str,
    candidate_commit: str,
    candidate_source_sha256: str,
    candidate_input_hashes: dict[str, str],
    packaged: TapirSnapshotIdentity,
    cache: TapirSnapshotIdentity | None,
) -> Acceptance:
    """Apply the contract's strict monotonic acceptance rules.

    Active is the newer valid snapshot by strict SemVer between the packaged
    and cached identities; equal version always selects the packaged snapshot.
    A candidate not newer than packaged is never cached. Equal-version replay
    against the packaged floor compares the peeled commit plus input hashes:
    the full snapshot hash depends on distribution metadata and never decides.
    Replay against an active cache additionally requires the exact cached
    canonical source hash. Any moved commit is equivocation.
    """

    if cache is not None and compare_semver(cache.version, packaged.version) > 0:
        active: TapirSnapshotIdentity = cache
        active_is_cache = True
    else:
        active = packaged
        active_is_cache = False
    comparison = compare_semver(candidate_version, active.version)
    if comparison > 0:
        return Acceptance("accept-newer", may_cache=True)
    if comparison < 0:
        return Acceptance("upstream-rollback", may_cache=False)
    if candidate_commit != active.upstream_commit or candidate_input_hashes != active.input_hashes:
        return Acceptance("upstream-equivocation", may_cache=False)
    if active_is_cache and candidate_source_sha256 != active.source_sha256:
        return Acceptance("upstream-equivocation", may_cache=False)
    return Acceptance("replay-current", may_cache=False)


def offline_mode_enabled(value: str | None = None) -> bool:
    """Read ARCHICAD_MCP_OFFLINE with its strict unset/``0``/``1`` grammar."""

    raw = os.environ.get("ARCHICAD_MCP_OFFLINE") if value is None else value
    if raw is None or raw == "0":
        return False
    if raw == "1":
        return True
    raise ValueError("ARCHICAD_MCP_OFFLINE must be exactly '0' or '1'")


def auto_update_enabled(value: str | None = None) -> bool:
    """ARCHICAD_MCP_AUTO_UPDATE accepts only unset/``1`` (on) or ``0`` (off)."""

    raw = os.environ.get("ARCHICAD_MCP_AUTO_UPDATE") if value is None else value
    if raw is None or raw == "1":
        return True
    if raw == "0":
        return False
    raise ValueError("ARCHICAD_MCP_AUTO_UPDATE must be exactly '0' or '1'")


@dataclass(frozen=True, slots=True)
class PackagedTapir:
    """The validated packaged Tapir snapshot floor."""

    payload: bytes
    provider_version: str
    provenance: tuple[str, ...]
    identity: TapirSnapshotIdentity
    snapshot: ProviderSnapshot


def load_packaged_tapir() -> PackagedTapir:
    """Load and validate the packaged Tapir snapshot from package resources.

    The strict reader enforces canonical bytes, exact document shape, and the
    tracked packaged provenance pins before any registry construction.
    """

    payload = files("archicad_mcp.schemas").joinpath("tapir.json").read_bytes()
    provider_version, provenance, identity = load_packaged_identity(payload)
    if identity.distribution != "packaged":
        raise TapirSnapshotMetadataError("unexpected_tapir_distribution")
    snapshot = load_provider_snapshot(
        payload,
        provider=PROVIDER_IDENTITY,
        provider_version=provider_version,
        distribution=f"packaged {identity.package_path}",
        provenance=provenance,
    )
    return PackagedTapir(
        payload=payload,
        provider_version=provider_version,
        provenance=provenance,
        identity=identity,
        snapshot=snapshot,
    )


def _aiohttp_fetch(
    session: aiohttp.ClientSession,
    *,
    stored_etag: str | None,
) -> Any:
    """Build the bounded no-redirect transport for one acquisition attempt."""

    async def fetch(url: str, maximum: int) -> FetchOutcome:
        conditional = url == releases_page_one_url()
        headers = (
            github_request_headers(stored_etag) if conditional else minimal_request_headers(url)
        )
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with session.get(
                url, headers=headers, timeout=timeout, allow_redirects=False
            ) as response:
                status = response.status
                response_headers = {
                    str(key): str(value)
                    for key, value in response.headers.items()
                    if key.lower() in {"etag", "link"}
                }
                raw_length = response.headers.get("Content-Length")
                if raw_length is not None and (
                    not raw_length.isascii() or not raw_length.isdigit()
                ):
                    raise GitHubReleaseError("response-size")
                if raw_length is not None and int(raw_length) > maximum:
                    raise GitHubReleaseError("response-size")
                if status == 304:
                    return FetchOutcome(status=status, headers=response_headers, body=None)
                captured = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    captured.extend(chunk)
                    if len(captured) > maximum:
                        raise GitHubReleaseError("response-size")
                return FetchOutcome(status=status, headers=response_headers, body=bytes(captured))
        except TimeoutError as exc:
            raise GitHubReleaseError("network-timeout") from exc
        except aiohttp.ClientError as exc:
            raise GitHubReleaseError("network-client") from exc

    return fetch


class AttemptTimeout(TimeoutError):
    """The absolute one-shot attempt budget is exhausted."""


def _remaining_budget(deadline: float) -> float:
    """Return the remaining monotonic attempt budget or raise ``AttemptTimeout``."""

    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise AttemptTimeout
    return remaining


def _assert_attempt_budget(deadline: float) -> None:
    """Refuse to continue past the absolute attempt deadline."""

    if time.monotonic() >= deadline:
        raise AttemptTimeout


@dataclass(frozen=True, slots=True)
class _Eligibility:
    proceed: bool
    status: UpdateStatus | None = None
    error: str | None = None


def _evaluate_eligibility(
    state: CheckState,
    *,
    manual: bool,
    mode: UpdateMode,
    auto_enabled: bool,
) -> _Eligibility:
    """Apply offline precedence, TTL, and the shared in-flight lease.

    Offline mode forbids every network check, including manual ones. Manual
    checks bypass the shared TTL but never the in-flight lease.
    """

    if mode == "offline":
        return _Eligibility(False, "offline-network-forbidden")
    lease = state.lease
    if lease is not None and not lease_is_stale(
        lease,
        now_wall=time.time(),
        now_monotonic=time.monotonic(),
    ):
        return _Eligibility(False, "in-flight")
    if not manual and not auto_enabled:
        return _Eligibility(False, "auto-disabled")
    if not manual and not ttl_allows_check(state.last_check_wall, now_wall=time.time()):
        return _Eligibility(False, "current")
    return _Eligibility(True)


async def _acquire_lock_bounded(timeout: float) -> BaseFileLock:
    """Acquire the native cache lock via nonblocking retries until a deadline.

    Every wait is an await point, so cancellation lands only while no lock is
    held; acquisition can never block indefinitely or escape its bound.
    """

    if not timeout > 0.0:
        raise CacheStoreError("lock-busy")
    lock_deadline = time.monotonic() + timeout
    while True:
        lock = try_acquire_cache_lock()
        if lock is not None:
            return lock
        remaining = lock_deadline - time.monotonic()
        if remaining <= 0.0:
            raise CacheStoreError("lock-busy")
        await asyncio.sleep(min(LOCK_RETRY_INTERVAL_SECONDS, remaining))


async def _claim_under_lock(
    *,
    manual: bool,
    mode: UpdateMode,
    auto_enabled: bool,
    deadline: float,
) -> tuple[_Eligibility, CheckState, CacheLease | None]:
    """Evaluate eligibility and claim the UUID in-flight lease under one hold."""

    lock = await _acquire_lock_bounded(min(STATE_LOCK_TIMEOUT_SECONDS, _remaining_budget(deadline)))
    try:
        state = load_check_state()
        _assert_attempt_budget(deadline)
        eligibility = _evaluate_eligibility(
            state, manual=manual, mode=mode, auto_enabled=auto_enabled
        )
        if not eligibility.proceed:
            return eligibility, state, None
        _assert_attempt_budget(deadline)
        lease = claim_lease()
        store_check_state(
            CheckState(
                last_check_wall=state.last_check_wall,
                last_outcome=state.last_outcome,
                last_error=state.last_error,
                release_etag=state.release_etag,
                lease=lease,
            )
        )
        return eligibility, state, lease
    finally:
        release_cache_lock(lock)


def _require_active_lease(fresh: CheckState, lease: CacheLease) -> None:
    """Refuse every mutation unless our exact lease is the active one.

    A missing (reset), replaced (takeover), or cleared lease refuses all
    completion, failure, and cleanup writes with ``lease-takeover``.
    """

    active_lease = fresh.lease
    if active_lease is None or active_lease.id != lease.id:
        raise CacheStoreError("lease-takeover")


async def _finish_attempt(
    lease: CacheLease,
    *,
    outcome: str | None,
    error: str | None,
    etag: str | None,
) -> CheckState:
    """Reacquire the lock, require our lease, apply CAS bookkeeping, clear it.

    ``etag=None`` retains the stored validator; a non-None value persists (or,
    when empty-handed by upstream, clears) the page-one ETag for terminal 200
    results only.
    """

    lock = await _acquire_lock_bounded(STATE_LOCK_TIMEOUT_SECONDS)
    try:
        fresh = load_check_state()
        _require_active_lease(fresh, lease)
        stored = CheckState(
            last_check_wall=time.time(),
            last_outcome=outcome,
            last_error=error,
            release_etag=etag if etag is not None else fresh.release_etag,
            lease=None,
        )
        store_check_state(stored)
        return stored
    finally:
        release_cache_lock(lock)


async def _abandon_lease(lease: CacheLease) -> None:
    """Best-effort bounded cleanup of our own lease after cancellation."""

    try:
        lock = await _acquire_lock_bounded(LEASE_CLEANUP_TIMEOUT_SECONDS)
    except CacheStoreError:
        logger.warning("Update lease cleanup was skipped; stale takeover will recover it")
        return
    try:
        fresh = load_check_state()
        active = fresh.lease
        if active is not None and active.id == lease.id:
            store_check_state(
                CheckState(
                    last_check_wall=fresh.last_check_wall,
                    last_outcome=fresh.last_outcome,
                    last_error="cancelled",
                    release_etag=fresh.release_etag,
                    lease=None,
                )
            )
    finally:
        release_cache_lock(lock)


async def _drain_abandon(lease: CacheLease | None) -> None:
    """Finish or cancel cleanup inline; never detach lease mutation work."""

    if lease is None:
        return
    try:
        await _abandon_lease(lease)
    except Exception:
        logger.warning("Update lease cleanup was skipped; stale takeover will recover it")


def _failure_outcome(exc: Exception) -> UpdateOutcome:
    """Map one ordinary attempt exception to a bounded stable outcome."""

    code = getattr(exc, "code", None)
    if (
        type(code) is str
        and 0 < len(code) <= 256
        and all(0x20 <= ord(character) < 0x7F for character in code)
    ):
        return UpdateOutcome("failed", code)
    return UpdateOutcome("failed", "internal-error")


async def _record_failure(lease: CacheLease | None, outcome: UpdateOutcome) -> UpdateOutcome:
    """Record one failed-attempt diagnostic without losing monotonic state.

    The stored page-one ETag is deliberately retained so a locally failed
    candidate (concurrent cache change, lost lease, write or validation
    failure, timeout) is retried against a later 200 instead of being
    suppressed by a 304 for an unprocessed listing.
    """

    if lease is None:
        return outcome
    try:
        await _finish_attempt(
            lease,
            outcome="failed",
            error=outcome.error or "internal-error",
            etag=None,
        )
    except CacheStoreError as exc:
        if exc.code != "lease-takeover":
            logger.warning("Failed-attempt diagnostic could not be recorded (%s)", exc.code)
    except TimeoutError:
        logger.warning("Failed-attempt diagnostic timed out; stale takeover will recover it")
    return outcome


async def run_update_check(
    packaged: PackagedTapir,
    session: aiohttp.ClientSession | None = None,
    *,
    manual: bool = False,
    mode: UpdateMode | None = None,
    auto_enabled: bool | None = None,
    fetch: Any = None,
    on_accepted: Callable[[ProviderSnapshot], Any] | None = None,
) -> UpdateOutcome:
    """Run exactly one bounded update check with the lease/CAS lifecycle.

    One absolute monotonic attempt deadline starts before the lease claim and
    bounds claim, network, transform/registry construction, the completion
    decision, and publication. Every lock wait is capped at min(per-lock
    bound, remaining budget); no detached thread or executor exists. Cancellation
    and timeout drain owned work and clear the lease when possible before this
    coroutine returns or re-raises, so no late cache write survives either.
    KeyboardInterrupt and SystemExit are never converted into update outcomes.
    """

    try:
        effective_mode: UpdateMode = (
            ("offline" if offline_mode_enabled() else "automatic") if mode is None else mode
        )
        effective_auto = auto_update_enabled() if auto_enabled is None else auto_enabled
    except ValueError as exc:
        return UpdateOutcome("failed", str(exc))
    attempt_deadline = time.monotonic() + ATTEMPT_TIMEOUT_SECONDS
    delay = max(0.0, attempt_deadline - time.monotonic())
    lease: CacheLease | None = None
    try:
        async with asyncio.timeout(delay):
            eligibility, prior_state, lease = await _claim_under_lock(
                manual=manual,
                mode=effective_mode,
                auto_enabled=effective_auto,
                deadline=attempt_deadline,
            )
            if not eligibility.proceed or lease is None:
                return UpdateOutcome(eligibility.status or "failed", eligibility.error)
            return await _attempt(
                packaged,
                lease,
                stored_etag=prior_state.release_etag,
                session=session,
                fetch=fetch,
                on_accepted=on_accepted,
                deadline=attempt_deadline,
            )
    except asyncio.CancelledError:
        # Drain owned work deterministically: cleanup runs inline under shield
        # before this coroutine re-raises, and nothing may mutate afterwards.
        await _drain_abandon(lease)
        raise
    except TimeoutError:
        return await _record_failure(lease, UpdateOutcome("failed", "attempt-timeout"))
    except Exception as exc:
        return await _record_failure(lease, _failure_outcome(exc))


async def _attempt(
    packaged: PackagedTapir,
    lease: CacheLease,
    *,
    stored_etag: str | None,
    session: aiohttp.ClientSession | None,
    fetch: Any,
    on_accepted: Callable[[ProviderSnapshot], Any] | None,
    deadline: float,
) -> UpdateOutcome:
    """Run acquisition plus candidate completion for one leased attempt."""

    if fetch is not None:
        acquisition = await acquire_latest_stable_release(fetch, stored_etag=stored_etag)
    elif session is not None:
        transport = _aiohttp_fetch(session, stored_etag=stored_etag)
        acquisition = await acquire_latest_stable_release(transport, stored_etag=stored_etag)
    else:
        async with aiohttp.ClientSession() as owned_session:
            transport = _aiohttp_fetch(owned_session, stored_etag=stored_etag)
            acquisition = await acquire_latest_stable_release(transport, stored_etag=stored_etag)
    return await _complete_attempt(
        packaged,
        lease,
        acquisition=acquisition,
        on_accepted=on_accepted,
        deadline=deadline,
    )


def _terminal_200_status(status: str) -> bool:
    """Whether one completion status consumed a successful 200 listing."""

    return status in ("updated", "current", "upstream-rollback", "upstream-equivocation")


async def _complete_attempt(
    packaged: PackagedTapir,
    lease: CacheLease,
    *,
    acquisition: TapirReleaseAcquisition | None,
    on_accepted: Callable[[ProviderSnapshot], Any] | None,
    deadline: float,
) -> UpdateOutcome:
    """Complete one attempt: CAS acceptance, atomic publication, bookkeeping.

    The candidate transform happens as synchronous work outside the OS lock;
    afterwards this coroutine explicitly yields once so a cancellation
    requested during that synchronous window is delivered, then re-checks the
    remaining monotonic budget before acquiring the lock. Under the lock the
    budget is checked again immediately before any mutation, only a cheap
    SHA-256 re-read confirms the cache did not change concurrently, and at
    most one snapshot file is atomically replaced. A fresh page-one ETag is
    persisted for successfully processed terminal 200 results only; every
    local failure retains the prior validator, a 200 without an ETag clears
    it, and 304 keeps it.
    """

    candidate_payload: bytes | None = None
    candidate_identity: TapirSnapshotIdentity | None = None
    pre_read: CacheSnapshotResult | None = None
    if acquisition is not None:
        candidate_payload, candidate_identity = _build_candidate_payload(acquisition)
        # Cancellation checkpoint after synchronous transform/registry work.
        await asyncio.sleep(0)
        _assert_attempt_budget(deadline)
        pre_read = read_cached_snapshot()
    lock = await _acquire_lock_bounded(min(STATE_LOCK_TIMEOUT_SECONDS, _remaining_budget(deadline)))
    outcome_status: UpdateStatus = "current"
    error: str | None = None
    accepted_payload: bytes | None = None
    try:
        fresh = load_check_state()
        _require_active_lease(fresh, lease)
        # Budget re-check under the acquired lock, immediately pre-mutation.
        _assert_attempt_budget(deadline)
        if acquisition is not None:
            validated_sha = (
                pre_read.cached.source_sha256
                if pre_read is not None and pre_read.cached is not None
                else None
            )
            # Only a previously VALIDATED cache participates in the CAS guard.
            # Absent or corrupt bytes are healed by this attempt; the in-flight
            # lease guarantees no other updater writes concurrently.
            if validated_sha is not None and _snapshot_bytes_sha256() != validated_sha:
                outcome_status = "failed"
                error = "cache-concurrent-change"
            elif candidate_identity is None or candidate_payload is None:
                outcome_status = "failed"
                error = "candidate-unavailable"
            else:
                decision = decide_acceptance(
                    candidate_version=candidate_identity.version,
                    candidate_commit=candidate_identity.upstream_commit,
                    candidate_source_sha256=candidate_identity.source_sha256,
                    candidate_input_hashes=dict(candidate_identity.input_hashes),
                    packaged=packaged.identity,
                    cache=_cached_identity(pre_read),
                )
                outcome_status, error = _apply_decision(decision.decision, candidate_payload)
                if outcome_status == "updated":
                    accepted_payload = candidate_payload
        # A fresh page-one ETag persists only for successfully processed
        # terminal 200 results; 304 and local failures retain the prior value.
        if acquisition is None or not _terminal_200_status(outcome_status):
            persisted_etag = fresh.release_etag
        else:
            persisted_etag = acquisition.release_etag
        store_check_state(
            CheckState(
                last_check_wall=time.time(),
                last_outcome=outcome_status,
                last_error=error,
                release_etag=persisted_etag,
                lease=None,
            )
        )
    finally:
        release_cache_lock(lock)
    if outcome_status == "updated" and accepted_payload is not None and on_accepted:
        published = snapshot_from_payload(accepted_payload)
        if published is not None:
            on_accepted(published)
    return UpdateOutcome(outcome_status, error)


def _apply_decision(
    decision: AcceptanceDecision, candidate_payload: bytes
) -> tuple[UpdateStatus, str | None]:
    """Apply one pure acceptance decision to durable state."""

    if decision == "accept-newer":
        atomically_replace(snapshot_path(), candidate_payload)
        return "updated", None
    if decision == "upstream-rollback":
        return "upstream-rollback", "upstream-rollback"
    if decision == "upstream-equivocation":
        return "upstream-equivocation", "upstream-equivocation"
    return "current", None


def _snapshot_bytes_sha256() -> str | None:
    """Return the raw cached snapshot digest, or ``None`` when absent/oversized."""

    try:
        data = snapshot_path().read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Cached snapshot re-read failed (%s)", exc.__class__.__name__)
        return "<unreadable>"
    if not data or len(data) > SNAPSHOT_MAX_BYTES:
        return "<invalid-size>"
    return sha256_hex(data)


def _cached_identity(pre_read: CacheSnapshotResult | None) -> TapirSnapshotIdentity | None:
    """Project one validated pre-read cache result into its durable identity."""

    if pre_read is None or pre_read.cached is None:
        return None
    return identity_of_cached(pre_read.cached)


def _candidate_metadata(acquisition: TapirReleaseAcquisition) -> dict[str, Any]:
    """Build the user-cache metadata record for one verified acquisition."""

    return snapshot_metadata(
        provider_version=acquisition.version,
        distribution=CACHE_DISTRIBUTION,
        package_path=CACHE_SNAPSHOT_RELATIVE_PATH,
        upstream_repository=UPSTREAM_REPOSITORY_URL,
        upstream_tag=acquisition.tag,
        upstream_commit=acquisition.commit,
        license_name=PINNED_LICENSE_NAME,
        input_hashes={
            "command_definitions.js": sha256_hex(acquisition.command_definitions),
            "common_schema_definitions.js": sha256_hex(acquisition.common_schema_definitions),
        },
        observed_assets=acquisition.observed_assets.as_json(),
    )


def _build_candidate_payload(
    acquisition: TapirReleaseAcquisition,
) -> tuple[bytes, TapirSnapshotIdentity]:
    """Transform, validate, and serialize one candidate into canonical bytes."""

    verify_license_identity(acquisition.license_bytes, PINNED_LICENSE_SHA256)
    document = transform_inputs(
        acquisition.command_definitions,
        acquisition.common_schema_definitions,
        metadata=_candidate_metadata(acquisition),
    )
    payload = serialize_tapir_snapshot(document)
    metadata = cast(dict[str, Any], document["_metadata"])
    return payload, identity_from_metadata(metadata, payload)


def identity_of_cached(cached: CachedSnapshot) -> TapirSnapshotIdentity:
    """Project one validated cached snapshot into its durable identity."""

    return TapirSnapshotIdentity(
        version=cached.version,
        distribution=CACHE_DISTRIBUTION,
        package_path=CACHE_SNAPSHOT_RELATIVE_PATH,
        upstream_repository=UPSTREAM_REPOSITORY_URL,
        upstream_tag=cached.tag,
        upstream_commit=cached.commit,
        license_name=PINNED_LICENSE_NAME,
        source_sha256=cached.source_sha256,
        input_hashes=dict(cached.input_hashes),
        observed_majors=tuple(cached.observed_majors),
        observed_platforms=tuple(cached.observed_platforms),
    )


def snapshot_from_payload(payload: bytes) -> ProviderSnapshot | None:
    """Rebuild one immutable registry snapshot from accepted canonical bytes."""

    try:
        provider_version, provenance, identity = load_tapir_identity(payload)
        if identity.distribution != CACHE_DISTRIBUTION:
            raise TapirSnapshotMetadataError("unexpected_tapir_distribution")
        return load_provider_snapshot(
            payload,
            provider=PROVIDER_IDENTITY,
            provider_version=provider_version,
            distribution=f"{CACHE_DISTRIBUTION} {CACHE_SNAPSHOT_RELATIVE_PATH}",
            provenance=provenance,
        )
    except (
        TapirSnapshotMetadataError,
        SemverValidationError,
        SchemaRegistryError,
    ):
        return None


def schemas_status(
    packaged: PackagedTapir,
    *,
    mode: UpdateMode | None = None,
    auto_enabled: bool | None = None,
) -> dict[str, Any]:
    """Return local-only packaged/cache/active/check diagnostics.

    Never networks and never prints cache content. ``mode``/``auto_enabled``
    are explicit inputs; ``None`` reports the pinned environment value, or
    ``None`` in the payload when that value is invalid.
    """

    cached_identity, cache_error, cache_assets = _cache_identity()
    if (
        cached_identity is not None
        and compare_semver(cached_identity.version, packaged.identity.version) > 0
    ):
        active_origin = "cache"
        active_version = cached_identity.version
    else:
        active_origin = "packaged"
        active_version = packaged.identity.version
    offline: bool | None
    try:
        offline = offline_mode_enabled() if mode is None else mode == "offline"
    except ValueError:
        offline = None
    auto_error: str | None = None
    auto: bool | None
    try:
        auto = auto_update_enabled() if auto_enabled is None else auto_enabled
    except ValueError as exc:
        auto, auto_error = None, str(exc)
    state = load_check_state()
    now_wall = time.time()
    last_check_age = (
        None if state.last_check_wall is None else max(0.0, now_wall - state.last_check_wall)
    )
    lease = state.lease
    in_flight = lease is not None and not lease_is_stale(
        lease,
        now_wall=now_wall,
        now_monotonic=time.monotonic(),
    )
    observed: dict[str, list[Any]] | None = (
        None
        if cached_identity is None
        else {
            "majors": list(cache_assets[0]) if cache_assets else [],
            "platforms": list(cache_assets[1]) if cache_assets else [],
        }
    )
    return {
        "auto_update": {"enabled": auto, "error": auto_error},
        "offline": offline,
        "packaged_version": packaged.identity.version,
        "active": {"origin": active_origin, "version": active_version},
        "cache": {
            "present": cached_identity is not None,
            "version": None if cached_identity is None else cached_identity.version,
            "commit": None if cached_identity is None else cached_identity.upstream_commit,
            "observed_assets": observed,
            "error": cache_error,
        },
        "check_state": {
            "last_outcome": state.last_outcome,
            "last_error": state.last_error,
            "last_check_age_seconds": last_check_age,
            "etag_present": state.release_etag is not None,
            "in_flight": in_flight,
            "ttl_seconds": TTL_SECONDS,
            "lease_stale_seconds": LEASE_STALE_SECONDS,
        },
    }


def _cache_identity() -> tuple[
    TapirSnapshotIdentity | None, str | None, tuple[tuple[int, ...], tuple[str, ...]] | None
]:
    result = read_cached_snapshot()
    if result.cached is not None:
        cached = result.cached
        return (
            identity_of_cached(cached),
            None,
            (tuple(cached.observed_majors), tuple(cached.observed_platforms)),
        )
    return None, result.error_code, None


def reset_schema_cache() -> list[str]:
    """Explicitly delete cached snapshot/check state under the cache lock.

    Lock participation is bounded: a persistently busy lock surfaces the
    stable ``lock-busy`` error instead of hanging.
    """

    try:
        lock = acquire_cache_lock(RESET_LOCK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise CacheStoreError("lock-busy") from exc
    try:
        return reset_cached_files()
    finally:
        release_cache_lock(lock)


__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "Acceptance",
    "AcceptanceDecision",
    "AttemptTimeout",
    "PackagedTapir",
    "UpdateMode",
    "UpdateOutcome",
    "UpdateStatus",
    "auto_update_enabled",
    "decide_acceptance",
    "identity_of_cached",
    "load_packaged_tapir",
    "offline_mode_enabled",
    "read_cached_snapshot",
    "reset_schema_cache",
    "run_update_check",
    "schemas_status",
    "snapshot_from_payload",
]
