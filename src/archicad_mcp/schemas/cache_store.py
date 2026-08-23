"""Canonical user-cache store for direct Tapir snapshot updates.

The cache directory remains the canonical OS user-cache ``archicad-mcp``
location and never the installed package. The versioned ``schema-cache/``
subdirectory holds one complete canonical cached snapshot document
(``tapir.json``), bounded nonsecret check state (``check-state.json``), and a
permanent native OS lock file that is never deleted during normal operation or
reset. Snapshot parsing and registry construction happen outside the OS lock;
the lock covers only short atomic state transitions.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn, cast

from filelock import BaseFileLock, UnixFileLock, WindowsFileLock
from filelock import Timeout as FileLockTimeout

from archicad_mcp.schemas.registry import (
    ProviderSnapshot,
    SchemaRegistryError,
    load_provider_snapshot,
)
from archicad_mcp.schemas.tapir_source import (
    CACHE_DISTRIBUTION,
    PROVIDER_IDENTITY,
    TapirSnapshotMetadataError,
    TapirSourceError,
    require_distribution,
    sha256_hex,
    tapir_provenance,
    validate_observed_assets,
    validate_snapshot_document,
)
from archicad_mcp.schemas.tapir_source import (
    SNAPSHOT_MAX_BYTES as TAPIR_SNAPSHOT_MAX_BYTES,
)

CACHE_ROOT_NAME: Final[str] = "archicad-mcp"
SCHEMA_CACHE_DIR_NAME: Final[str] = "schema-cache"
SNAPSHOT_FILENAME: Final[str] = "tapir.json"
CHECK_STATE_FILENAME: Final[str] = "check-state.json"
LOCK_FILENAME: Final[str] = "schema-cache.lock"
CACHE_SNAPSHOT_RELATIVE_PATH: Final[str] = f"{SCHEMA_CACHE_DIR_NAME}/{SNAPSHOT_FILENAME}"

CHECK_STATE_FORMAT: Final[str] = "archicad-mcp.tapir-check-state"
CHECK_STATE_VERSION: Final[int] = 1
CHECK_STATE_MAX_BYTES: Final[int] = 16_384
SNAPSHOT_MAX_BYTES: Final[int] = TAPIR_SNAPSHOT_MAX_BYTES

TTL_SECONDS: Final[float] = 24 * 60 * 60
LEASE_STALE_SECONDS: Final[float] = 90.0

_LEASE_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class CacheStoreError(ValueError):
    """A cache-store failure represented by a stable code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CacheLease:
    """One claimed in-flight check lease.

    Validation lives here, so direct construction cannot bypass the canonical
    v4 UUID and finite-timestamp requirements that persisted leases satisfy.
    """

    id: str
    wall: float
    monotonic: float

    def __post_init__(self) -> None:
        if type(self.id) is not str or _LEASE_ID_RE.fullmatch(self.id) is None:
            raise CacheStoreError("check-state-lease")
        if (
            type(self.wall) is not float
            or type(self.monotonic) is not float
            or not _valid_timestamp(self.wall)
            or not _valid_timestamp(self.monotonic)
        ):
            raise CacheStoreError("check-state-lease")

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "wall": self.wall, "monotonic": self.monotonic}


def claim_lease() -> CacheLease:
    """Create one fresh UUID lease with wall and monotonic timestamps."""

    return CacheLease(id=str(uuid.uuid4()), wall=time.time(), monotonic=time.monotonic())


def lease_is_stale(lease: CacheLease, *, now_wall: float, now_monotonic: float) -> bool:
    """A lease becomes stale only after the takeover bound.

    Persisted monotonic time ahead of the current monotonic clock means the
    machine rebooted (or the counter was reset), so the lease is stale
    immediately, including when the wall clock also moved backward. Otherwise
    ``max(wall_age, monotonic_age)`` recovers across backward wall-clock
    changes without indefinite leases; a premature forward-clock jump may
    declare a live lease stale early, which remains safe because every
    mutation requires the exact lease-id CAS.
    """

    if lease.monotonic > now_monotonic:
        return True
    return max(now_wall - lease.wall, now_monotonic - lease.monotonic) > LEASE_STALE_SECONDS


def ttl_allows_check(last_check_wall: float | None, *, now_wall: float) -> bool:
    """Return whether the shared 24-hour TTL permits a new automatic check."""

    if last_check_wall is None:
        return True
    return (now_wall - last_check_wall) >= TTL_SECONDS


def _native_unix_locking_available() -> bool:
    if os.name == "nt":
        return False
    try:
        import fcntl
    except ImportError:
        return False
    return all(hasattr(fcntl, name) for name in ("flock", "LOCK_EX", "LOCK_NB", "LOCK_UN"))


def _native_lock_type() -> type[BaseFileLock]:
    """Select the tested native lock implementation for this platform."""

    if os.name == "nt":
        return WindowsFileLock
    if _native_unix_locking_available():
        return UnixFileLock
    raise CacheStoreError("cache-lock")


def cache_root() -> Path:
    """Return the canonical per-user cache root without creating anything."""

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data)
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            return Path(user_profile) / "AppData" / "Local"
    else:
        xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache_home:
            return Path(xdg_cache_home)
        home = os.environ.get("HOME")
        if home:
            return Path(home) / ".cache"
    fallback = Path.home()
    return fallback / ("AppData/Local" if os.name == "nt" else ".cache")


def schema_cache_dir() -> Path:
    """Return the versioned schema-cache subdirectory without creating it."""

    return cache_root() / CACHE_ROOT_NAME / SCHEMA_CACHE_DIR_NAME


def snapshot_path() -> Path:
    return schema_cache_dir() / SNAPSHOT_FILENAME


def check_state_path() -> Path:
    return schema_cache_dir() / CHECK_STATE_FILENAME


def cache_lock_path() -> Path:
    return schema_cache_dir() / LOCK_FILENAME


def ensure_schema_cache_dir() -> None:
    try:
        schema_cache_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CacheStoreError("cache-dir") from exc


def _native_lock(lock_path: Path) -> BaseFileLock:
    try:
        return _native_lock_type()(
            lock_path,
            timeout=-1.0,
            blocking=True,
            close_error_policy="suppress",
            preserve_lock_file=True,
            fallback_to_soft=False,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise CacheStoreError("cache-lock") from exc


def try_acquire_cache_lock() -> BaseFileLock | None:
    """Try one nonblocking native acquisition of the permanent cache lock."""

    ensure_schema_cache_dir()
    lock = _native_lock(cache_lock_path())
    try:
        lock.acquire(timeout=0.0, blocking=False)
    except FileLockTimeout:
        return None
    except OSError as exc:
        raise CacheStoreError("cache-lock") from exc
    return lock


def acquire_cache_lock(timeout: float) -> BaseFileLock:
    """Acquire the permanent cache lock under an explicit finite deadline."""

    if not math.isfinite(timeout) or timeout <= 0.0:
        raise CacheStoreError("cache-lock")
    ensure_schema_cache_dir()
    lock = _native_lock(cache_lock_path())
    try:
        lock.acquire(timeout=timeout, blocking=True)
    except FileLockTimeout as exc:
        raise TimeoutError from exc
    except OSError as exc:
        raise CacheStoreError("cache-lock") from exc
    return lock


def release_cache_lock(lock: BaseFileLock) -> None:
    """Release once, retry a still-held lock once, then surface failure.

    A persistent release failure is never suppressed: the caller receives a
    stable ``cache-lock-release`` error so lock state cannot silently leak.
    """

    try:
        lock.release()
    except Exception:
        if getattr(lock, "is_locked", True):
            with suppress(Exception):
                lock.release(force=True)
        if getattr(lock, "is_locked", True):
            raise CacheStoreError("cache-lock-release") from None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def atomically_replace(path: Path, data: bytes) -> None:
    """Write private staged bytes then replace the final path in one step."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise CacheStoreError("cache-write") from exc
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException as exc:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise CacheStoreError("cache-write") from exc
        raise
    _fsync_directory(path.parent)


@dataclass(frozen=True, slots=True)
class CheckState:
    """Bounded nonsecret TTL/ETag/outcome/lease state."""

    last_check_wall: float | None = None
    last_outcome: str | None = None
    last_error: str | None = None
    release_etag: str | None = None
    lease: CacheLease | None = None

    def __post_init__(self) -> None:
        if self.last_check_wall is not None and type(self.last_check_wall) is not float:
            raise CacheStoreError("check-state-field")
        if self.last_check_wall is not None and not _valid_timestamp(self.last_check_wall):
            raise CacheStoreError("check-state-field")
        for name in ("last_outcome", "last_error", "release_etag"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not 0 < len(value) <= 256):
                raise CacheStoreError("check-state-field")
            if value is not None:
                _clean_state_text(value)
        if self.lease is not None and type(self.lease) is not CacheLease:
            raise CacheStoreError("check-state-lease")

    def as_json(self) -> dict[str, Any]:
        return {
            "format": CHECK_STATE_FORMAT,
            "version": CHECK_STATE_VERSION,
            "last_check_wall": self.last_check_wall,
            "last_outcome": self.last_outcome,
            "last_error": self.last_error,
            "release_etag": self.release_etag,
            "lease": None if self.lease is None else self.lease.as_json(),
        }


def _valid_timestamp(value: float) -> bool:
    return math.isfinite(value) and value >= 0.0


def _clean_state_text(value: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CacheStoreError("check-state-field")
    return value


def _reject_json_constant(raw: str) -> NoReturn:
    raise ValueError(f"forbidden-constant:{raw}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate-json-key:{key}")
        result[key] = value
    return result


def _lease_from_json(value: object) -> CacheLease | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"id", "wall", "monotonic"}:
        raise CacheStoreError("check-state-lease")
    mapping = cast(dict[str, Any], value)
    # CacheLease validation itself refuses noncanonical/non-v4 ids and
    # nonfinite or negative timestamps.
    return CacheLease(
        id=cast(Any, mapping["id"]),
        wall=cast(Any, mapping["wall"]),
        monotonic=cast(Any, mapping["monotonic"]),
    )


def check_state_from_json(data: bytes) -> CheckState:
    """Parse bounded check-state bytes; any deviation raises a stable code."""

    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CacheStoreError("check-state-json") from exc
    expected_keys = {
        "format",
        "version",
        "last_check_wall",
        "last_outcome",
        "last_error",
        "release_etag",
        "lease",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise CacheStoreError("check-state-shape")
    mapping = cast(dict[str, Any], value)
    if mapping["format"] != CHECK_STATE_FORMAT or mapping["version"] != CHECK_STATE_VERSION:
        raise CacheStoreError("check-state-format")
    return CheckState(
        last_check_wall=mapping["last_check_wall"],
        last_outcome=mapping["last_outcome"],
        last_error=mapping["last_error"],
        release_etag=mapping["release_etag"],
        lease=_lease_from_json(mapping["lease"]),
    )


def check_state_bytes(state: CheckState) -> bytes:
    encoded = json.dumps(state.as_json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > CHECK_STATE_MAX_BYTES:
        raise CacheStoreError("check-state-size")
    return encoded


def load_check_state() -> CheckState:
    """Read bounded check state; missing or corrupt state reads as fresh."""

    path = check_state_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return CheckState()
    except OSError as exc:
        raise CacheStoreError("check-state-read") from exc
    if not data or len(data) > CHECK_STATE_MAX_BYTES:
        return CheckState()
    try:
        return check_state_from_json(data)
    except CacheStoreError:
        return CheckState()


def store_check_state(state: CheckState) -> None:
    atomically_replace(check_state_path(), check_state_bytes(state))


@dataclass(frozen=True, slots=True)
class CachedSnapshot:
    """One validated complete cached Tapir snapshot document."""

    payload: bytes
    version: str
    tag: str
    commit: str
    source_sha256: str
    input_hashes: dict[str, str]
    observed_majors: tuple[int, ...]
    observed_platforms: tuple[str, ...]
    snapshot: ProviderSnapshot


@dataclass(frozen=True, slots=True)
class CacheSnapshotResult:
    """A cache read outcome; corruption is surfaced, never raised as a crash."""

    cached: CachedSnapshot | None
    error_code: str | None


def read_cached_snapshot() -> CacheSnapshotResult:
    """Validate and read the cached snapshot; corruption is treated as absent.

    Parsing and registry construction happen here, outside any OS lock.
    """

    path = snapshot_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return CacheSnapshotResult(None, None)
    except OSError as exc:
        return CacheSnapshotResult(None, f"cache-read:{exc.__class__.__name__}")
    if not data or len(data) > SNAPSHOT_MAX_BYTES:
        return CacheSnapshotResult(None, "cache-size")
    try:
        _root, metadata = validate_snapshot_document(data)
        require_distribution(metadata, CACHE_DISTRIBUTION)
    except (TapirSourceError, TapirSnapshotMetadataError) as exc:
        return CacheSnapshotResult(None, exc.code)
    version = str(metadata["provider_version"])
    inputs = cast(dict[str, Any], metadata["inputs"])
    assets = validate_observed_assets(metadata["observed_assets"])
    try:
        registry_snapshot = load_provider_snapshot(
            data,
            provider=PROVIDER_IDENTITY,
            provider_version=version,
            distribution=f"{CACHE_DISTRIBUTION} {metadata['package_path']}",
            provenance=tapir_provenance(metadata),
        )
    except SchemaRegistryError as exc:
        return CacheSnapshotResult(None, f"registry:{exc.code}")
    cached = CachedSnapshot(
        payload=data,
        version=version,
        tag=str(metadata["upstream_tag"]),
        commit=str(metadata["upstream_commit"]),
        source_sha256=sha256_hex(data),
        input_hashes={str(name): str(inputs[name]) for name in sorted(inputs)},
        observed_majors=tuple(int(major) for major in assets["majors"]),
        observed_platforms=tuple(str(platform) for platform in assets["platforms"]),
        snapshot=registry_snapshot,
    )
    return CacheSnapshotResult(cached, None)


def reset_cache() -> list[str]:
    """Delete cached snapshot/check state while retaining the native lock file."""

    ensure_schema_cache_dir()
    targets = [snapshot_path(), check_state_path()]
    removed: list[str] = []
    for target in targets:
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CacheStoreError("cache-reset") from exc
        removed.append(target.name)
    return sorted(removed)


__all__ = [
    "CACHE_ROOT_NAME",
    "CACHE_SNAPSHOT_RELATIVE_PATH",
    "CHECK_STATE_FILENAME",
    "CHECK_STATE_FORMAT",
    "CHECK_STATE_MAX_BYTES",
    "CHECK_STATE_VERSION",
    "LEASE_STALE_SECONDS",
    "LOCK_FILENAME",
    "SCHEMA_CACHE_DIR_NAME",
    "SNAPSHOT_FILENAME",
    "SNAPSHOT_MAX_BYTES",
    "TTL_SECONDS",
    "CacheLease",
    "CacheSnapshotResult",
    "CacheStoreError",
    "CachedSnapshot",
    "CheckState",
    "acquire_cache_lock",
    "atomically_replace",
    "cache_lock_path",
    "cache_root",
    "check_state_bytes",
    "check_state_from_json",
    "check_state_path",
    "claim_lease",
    "ensure_schema_cache_dir",
    "lease_is_stale",
    "load_check_state",
    "read_cached_snapshot",
    "release_cache_lock",
    "reset_cache",
    "schema_cache_dir",
    "snapshot_path",
    "store_check_state",
    "try_acquire_cache_lock",
    "ttl_allows_check",
]
