"""Strict non-executing Tapir source transform and snapshot identity.

Shared package code owns the transformation of the upstream documentation
JavaScript into the canonical Tapir snapshot document. The release-maintainer
generator (``scripts/generate_tapir_snapshot.py``) and the direct GitHub
updater both import this module and share one exact full-consuming transform;
JavaScript is never executed. Every snapshot reader enforces canonical bytes,
exact document shape, distribution-specific provenance pins, and registry
consistency before any identity is derived.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NoReturn, cast

from archicad_mcp.schemas.registry import ProviderName, SchemaRegistryError, load_provider_snapshot
from archicad_mcp.schemas.semver import (
    SAFE_INTEGER_MAXIMUM,
    SemverValidationError,
    compare_semver,
    is_stable_release_version,
    validate_semver,
)

TAPIR_METADATA_FORMAT: Final[str] = "archicad-mcp.tapir-snapshot/1"
PROVIDER_IDENTITY: Final[ProviderName] = "tapir"
PACKAGED_DISTRIBUTION: Final[str] = "packaged"
CACHE_DISTRIBUTION: Final[str] = "user-cache"
GENERATOR_IDENTITY: Final[str] = "archicad_mcp.schemas.tapir_source"
GENERATOR_SCRIPT_IDENTITY: Final[str] = "scripts/generate_tapir_snapshot.py"

COMMAND_ENVELOPE_PREFIX: Final[bytes] = b"var gCommands = "
COMMON_ENVELOPE_PREFIX: Final[bytes] = b"var gSchemaDefinitions = "

SOURCE_MAX_BYTES: Final[int] = 16 * 1024 * 1024
SNAPSHOT_MAX_BYTES: Final[int] = 16 * 1024 * 1024
LICENSE_MAX_BYTES: Final[int] = 64 * 1024
ELEMENT_TYPES_SOURCE_SCHEMA: Final[str] = "ElementType"
REQUIRED_COMMAND_NAME: Final[str] = "GetAddOnVersion"

_GIT_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RELATIVE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
)
_COMMAND_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")
_CONTROL_CHARACTER_LIMIT: Final[int] = 0x20

MAX_NAME_LENGTH: Final[int] = 120
MAX_DESCRIPTION_LENGTH: Final[int] = 4096
MAX_JSON_NUMBER_LENGTH: Final[int] = 128

SNAPSHOT_DOCUMENT_KEYS: Final[frozenset[str]] = frozenset(
    {"commands", "element_types", "common_schemas", "_metadata"}
)
SNAPSHOT_COMMAND_KEYS: Final[frozenset[str]] = frozenset(
    {"category", "description", "version", "parameters", "returns", "api", "name"}
)

TAPIR_PACKAGED_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "format",
        "provider",
        "provider_version",
        "package_path",
        "upstream_repository",
        "upstream_tag",
        "upstream_commit",
        "license",
        "inputs",
        "generator",
    }
)
TAPIR_CACHE_METADATA_KEYS: Final[frozenset[str]] = TAPIR_PACKAGED_METADATA_KEYS | {
    "distribution",
    "observed_assets",
}
TAPIR_INPUT_NAMES: Final[frozenset[str]] = frozenset(
    {"command_definitions.js", "common_schema_definitions.js"}
)

OBSERVED_ASSET_KEYS: Final[frozenset[str]] = frozenset({"majors", "platforms"})
OBSERVED_ASSET_PLATFORMS: Final[frozenset[str]] = frozenset({"macos", "windows"})
MAX_OBSERVED_MAJOR: Final[int] = 9999

PINNED_LICENSE_NAME: Final[str] = "MIT"
PINNED_LICENSE_SHA256: Final[str] = (
    "8a457b52ce299c657cfa9210b825a162438a25a0fc4fe027420295feb13575a4"
)

PACKAGED_PACKAGE_PATH: Final[str] = "archicad_mcp/schemas/tapir.json"
PACKAGED_PROVIDER_VERSION: Final[str] = "1.5.8"
PACKAGED_UPSTREAM_COMMIT: Final[str] = "ce033d6bdcc90b538b3c5f7ab62f676099b96823"
PACKAGED_INPUT_SHA256: Final[dict[str, str]] = {
    "command_definitions.js": ("b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8"),
    "common_schema_definitions.js": (
        "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0"
    ),
}

TAPIR_UPSTREAM_REPOSITORY: Final[str] = "https://github.com/ENZYME-APD/tapir-archicad-automation"
CACHE_PACKAGE_PATH: Final[str] = "schema-cache/tapir.json"


class TapirSourceError(ValueError):
    """A bounded transform failure represented by a stable code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TapirSnapshotMetadataError(ValueError):
    """The Tapir snapshot document or metadata record is malformed or unpinned."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""

    return hashlib.sha256(data).hexdigest()


def _refuse(code: str) -> NoReturn:
    raise TapirSourceError(code)


def _metadata_refuse(code: str) -> NoReturn:
    raise TapirSnapshotMetadataError(code)


class _DuplicateKey(ValueError):
    pass


class _ForbiddenNumber(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _MarkedNumber(dict[str, str]):
    """Sentinel preserving the raw literal until integral normalization."""


_INTEGRAL = "__integral__"
_NONINTEGRAL = "__nonintegral__"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _parse_int(raw: str) -> int:
    if len(raw) > MAX_JSON_NUMBER_LENGTH:
        raise _ForbiddenNumber("numeric-literal-too-long")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise _ForbiddenNumber("integer-out-of-range") from exc
    if not value.is_finite() or abs(value) > SAFE_INTEGER_MAXIMUM:
        raise _ForbiddenNumber("integer-out-of-range")
    return int(value)


def _parse_float(raw: str) -> _MarkedNumber:
    """Classify one JSON float literal under exact decimal semantics.

    Integral literals normalize to integers only when their exact decimal
    value stays within the safe range; binary-float rounding never promotes a
    nonintegral or out-of-range literal into an accepted integer.
    """

    if len(raw) > MAX_JSON_NUMBER_LENGTH:
        raise _ForbiddenNumber("numeric-literal-too-long")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return _MarkedNumber({_NONINTEGRAL: raw})
    if value.is_finite() and value == value.to_integral_value():
        if abs(value) > SAFE_INTEGER_MAXIMUM:
            raise _ForbiddenNumber("integer-out-of-range")
        return _MarkedNumber({_INTEGRAL: str(value.quantize(Decimal(1)))})
    return _MarkedNumber({_NONINTEGRAL: raw})


def _parse_constant(raw: str) -> NoReturn:
    raise _ForbiddenNumber(f"forbidden-constant:{raw}")


def _normalize_value(node: object, path: str) -> object:
    """Resolve marked numbers; integral floats become ints, others are refused."""

    if isinstance(node, _MarkedNumber):
        if _INTEGRAL in node:
            return int(node[_INTEGRAL])
        _refuse(f"nonintegral-float:{path}:{node[_NONINTEGRAL]}")
    if isinstance(node, dict):
        normalized: dict[str, Any] = {}
        for key, child in cast(dict[str, Any], node).items():
            child_path = key if path == "" else f"{path}.{key}"
            normalized[key] = _normalize_value(child, child_path)
        return normalized
    if isinstance(node, list):
        return [_normalize_value(child, f"{path}[{index}]") for index, child in enumerate(node)]
    return node


def strict_json_text(text: str) -> object:
    """Parse strict JSON preserving integral-float sentinels for normalization.

    Duplicate keys, NaN/Infinity constants, integers outside the safe exact
    range, and malformed JSON are refused with stable codes.
    """

    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_parse_float,
            parse_int=_parse_int,
            parse_constant=_parse_constant,
        )
    except _DuplicateKey as exc:
        _refuse(f"duplicate-json-key:{exc.args[0]}")
    except _ForbiddenNumber as exc:
        _refuse(exc.code)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        del exc
        _refuse("malformed-json")


def normalize_marked_numbers(value: object, *, root_path: str) -> object:
    """Normalize a parsed tree, refusing nonintegral floats with stable paths."""

    return _normalize_value(value, root_path)


def strict_json_bytes(data: bytes) -> object:
    """Decode strict UTF-8 without a BOM and fully normalize it as strict JSON."""

    if data.startswith(b"\xef\xbb\xbf"):
        _refuse("utf8-bom")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _refuse("invalid-utf8")
    return normalize_marked_numbers(strict_json_text(text), root_path="")


def _decode_source_utf8(data: bytes, label: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        _refuse(f"utf8-bom:{label}")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _refuse(f"invalid-utf8:{label}")


def _parse_labeled_json(text: str, label: str) -> object:
    """Parse one strict JSON value, prefixing structural codes with the label."""

    try:
        return strict_json_text(text)
    except TapirSourceError as exc:
        code = exc.code
        if code.startswith("duplicate-json-key:") or code == "malformed-json":
            kind, _, detail = code.partition(":")
            _refuse(f"{kind}:{label}:{detail}")
        raise


def _consume_envelope(data: bytes, prefix: bytes, container: str, label: str) -> object:
    """Full-consume ``var gName = <JSON>;`` with an optional final LF or CRLF."""

    if not data.startswith(prefix):
        _refuse(f"input-envelope-prefix:{label}")
    body_start = len(prefix)
    tail = data[body_start:]
    stripped_end = len(tail)
    while stripped_end > 0 and tail[stripped_end - 1 : stripped_end] in (
        b" ",
        b"\t",
        b"\r",
        b"\n",
    ):
        stripped_end -= 1
    body = tail[:stripped_end]
    if not body.endswith(b";"):
        _refuse(f"input-envelope-terminator:{label}")
    json_part = body[:-1]
    trailing = tail[len(json_part) + 1 :]
    if trailing not in (b"", b"\n", b"\r\n"):
        _refuse(f"input-envelope-trailing:{label}")
    text = _decode_source_utf8(json_part, label)
    if not text.lstrip().startswith(container):
        _refuse(f"input-envelope-container:{label}")
    value = _parse_labeled_json(text, label)
    if container == "[" and type(value) is not list:
        _refuse(f"input-envelope-container:{label}")
    if container == "{" and type(value) is not dict:
        _refuse(f"input-envelope-container:{label}")
    return normalize_marked_numbers(value, root_path="")


def parse_command_definitions(data: bytes) -> list[Any]:
    """Full-consume the exact ``var gCommands`` array envelope."""

    value = _consume_envelope(data, COMMAND_ENVELOPE_PREFIX, "[", "command_definitions.js")
    return cast(list[Any], value)


def parse_common_schema_definitions(data: bytes) -> dict[str, Any]:
    """Full-consume the exact ``var gSchemaDefinitions`` object envelope."""

    value = _consume_envelope(data, COMMON_ENVELOPE_PREFIX, "{", "common_schema_definitions.js")
    return cast(dict[str, Any], value)


def validate_source_size(name: str, data: bytes) -> None:
    """Refuse empty or oversized upstream inputs before any parsing."""

    limit = LICENSE_MAX_BYTES if name == "LICENSE" else SOURCE_MAX_BYTES
    if not data or len(data) > limit:
        _refuse(f"input-size:{name}")


def _has_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    value = metadata[key]
    if (
        type(value) is not str
        or not value
        or _has_surrogate(value)
        or any(
            ord(character) < _CONTROL_CHARACTER_LIMIT or ord(character) == 0x7F
            for character in value
        )
    ):
        raise TapirSnapshotMetadataError(f"invalid_tapir_metadata_field:{key}")
    return value


def validate_observed_assets(value: object) -> dict[str, list[Any]]:
    """Validate the exact observed-asset record; empty sets mean no evidence."""

    if type(value) is not dict or frozenset(value) != OBSERVED_ASSET_KEYS:
        _metadata_refuse("invalid_tapir_observed_assets")
    mapping = cast(dict[str, Any], value)
    majors = mapping["majors"]
    platforms = mapping["platforms"]
    if type(majors) is not list or any(type(major) is not int for major in majors):
        _metadata_refuse("invalid_tapir_observed_assets")
    if any(major < 1 or major > MAX_OBSERVED_MAJOR for major in majors):
        _metadata_refuse("invalid_tapir_observed_assets")
    if majors != sorted(set(majors)):
        _metadata_refuse("invalid_tapir_observed_assets")
    if type(platforms) is not list or any(
        type(platform) is not str or platform not in OBSERVED_ASSET_PLATFORMS
        for platform in platforms
    ):
        _metadata_refuse("invalid_tapir_observed_assets")
    if platforms != sorted(set(platforms)):
        _metadata_refuse("invalid_tapir_observed_assets")
    return {"majors": list(majors), "platforms": list(platforms)}


def validate_tapir_metadata(metadata_value: object) -> dict[str, Any]:
    """Validate the snapshot metadata shape shared by every distribution.

    Packaged documents carry the exact packaged key set; cache documents add
    ``distribution`` with the exact user-cache marker plus a validated
    observed-assets record. Cache provenance is pinned to the fixed upstream
    repository and cache layout under the MIT policy with the runtime
    generator identity and a tag equal to the provider version.
    """

    if type(metadata_value) is not dict:
        raise TapirSnapshotMetadataError("malformed_tapir_metadata")
    metadata = cast(dict[str, Any], metadata_value)
    keys = frozenset(metadata)
    if keys == TAPIR_PACKAGED_METADATA_KEYS:
        distribution = PACKAGED_DISTRIBUTION
    elif keys == TAPIR_CACHE_METADATA_KEYS:
        marked = metadata["distribution"]
        if type(marked) is not str or marked != CACHE_DISTRIBUTION:
            raise TapirSnapshotMetadataError("invalid_tapir_distribution")
        distribution = marked
    else:
        raise TapirSnapshotMetadataError("unexpected_tapir_metadata_keys")
    if metadata["format"] != TAPIR_METADATA_FORMAT:
        raise TapirSnapshotMetadataError("unexpected_tapir_metadata_format")
    if metadata["provider"] != PROVIDER_IDENTITY:
        raise TapirSnapshotMetadataError("unexpected_tapir_metadata_provider")
    provider_version = _metadata_text(metadata, "provider_version")
    if not is_stable_release_version(provider_version):
        raise TapirSnapshotMetadataError("invalid_tapir_provider_version")
    package_path = _metadata_text(metadata, "package_path")
    if not package_path.endswith(".json") or _SAFE_RELATIVE_PATH_RE.fullmatch(package_path) is None:
        raise TapirSnapshotMetadataError("invalid_tapir_package_path")
    repository = _metadata_text(metadata, "upstream_repository")
    if not repository.startswith("https://") or len(repository) <= len("https://"):
        raise TapirSnapshotMetadataError("invalid_tapir_upstream_repository")
    tag = _metadata_text(metadata, "upstream_tag")
    if not is_stable_release_version(tag):
        raise TapirSnapshotMetadataError("invalid_tapir_upstream_tag")
    commit = _metadata_text(metadata, "upstream_commit")
    if _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise TapirSnapshotMetadataError("invalid_tapir_upstream_commit")
    license_name = _metadata_text(metadata, "license")
    generator = _metadata_text(metadata, "generator")
    inputs_value = metadata["inputs"]
    if type(inputs_value) is not dict or frozenset(inputs_value) != TAPIR_INPUT_NAMES:
        raise TapirSnapshotMetadataError("invalid_tapir_input_set")
    inputs = cast(dict[str, Any], inputs_value)
    for name in sorted(inputs):
        digest = inputs[name]
        if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise TapirSnapshotMetadataError("invalid_tapir_input_sha256")
    if distribution == CACHE_DISTRIBUTION:
        # Cached snapshots carry fixed direct-update provenance: the pinned
        # upstream repository and user-cache layout, the MIT license policy,
        # exactly the runtime generator identity, and a tag equal to the
        # provider version. Observed assets are validated exactly.
        if repository != TAPIR_UPSTREAM_REPOSITORY:
            raise TapirSnapshotMetadataError("invalid_tapir_upstream_repository")
        if package_path != CACHE_PACKAGE_PATH:
            raise TapirSnapshotMetadataError("invalid_tapir_package_path")
        if license_name != PINNED_LICENSE_NAME:
            raise TapirSnapshotMetadataError("invalid_tapir_license")
        if generator != GENERATOR_IDENTITY:
            raise TapirSnapshotMetadataError("invalid_tapir_generator")
        if tag != provider_version:
            raise TapirSnapshotMetadataError("invalid_tapir_upstream_tag")
        validate_observed_assets(metadata["observed_assets"])
    else:
        del license_name, generator
    return metadata


def require_packaged_provenance(metadata: dict[str, Any]) -> None:
    """Pin packaged metadata to the tracked 1.5.8 release-generator identity."""

    require_distribution(metadata, PACKAGED_DISTRIBUTION)
    expected = snapshot_metadata(
        provider_version=PACKAGED_PROVIDER_VERSION,
        package_path=PACKAGED_PACKAGE_PATH,
        upstream_repository=TAPIR_UPSTREAM_REPOSITORY,
        upstream_tag=PACKAGED_PROVIDER_VERSION,
        upstream_commit=PACKAGED_UPSTREAM_COMMIT,
        license_name=PINNED_LICENSE_NAME,
        input_hashes=dict(PACKAGED_INPUT_SHA256),
        generator=GENERATOR_SCRIPT_IDENTITY,
    )
    if metadata != expected:
        _metadata_refuse("unpinned_packaged_provenance")


def require_distribution(metadata: dict[str, Any], distribution: str) -> None:
    """Refuse snapshots whose validated distribution differs from ``distribution``."""

    observed = str(metadata.get("distribution", PACKAGED_DISTRIBUTION))
    if observed != distribution:
        raise TapirSnapshotMetadataError("unexpected_tapir_distribution")


def tapir_provenance(metadata: dict[str, Any]) -> tuple[str, ...]:
    """Derive the durable provenance tuple consumed by the immutable registry."""

    inputs = cast(dict[str, Any], metadata["inputs"])
    distribution = str(metadata.get("distribution", PACKAGED_DISTRIBUTION))
    entries = [
        f"path:{metadata['package_path']}",
        f"distribution:{distribution}",
        f"upstream:{metadata['upstream_repository']}",
        f"tag:{metadata['upstream_tag']}",
        f"commit:{metadata['upstream_commit']}",
        *(f"input-sha256:{name}={inputs[name]}" for name in sorted(inputs)),
        f"license:{metadata['license']}",
        f"generator:{metadata['generator']}",
    ]
    if distribution == CACHE_DISTRIBUTION:
        assets = validate_observed_assets(metadata["observed_assets"])
        majors = ",".join(str(major) for major in assets["majors"])
        platforms = ",".join(cast(list[str], assets["platforms"]))
        entries.append(f"observed-majors:{majors}")
        entries.append(f"observed-platforms:{platforms}")
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class TapirSnapshotIdentity:
    """The durable monotonic evidence for one accepted snapshot document."""

    version: str
    distribution: str
    package_path: str
    upstream_repository: str
    upstream_tag: str
    upstream_commit: str
    license_name: str
    source_sha256: str
    input_hashes: dict[str, str]
    observed_majors: tuple[int, ...] = ()
    observed_platforms: tuple[str, ...] = ()


def identity_from_metadata(metadata: dict[str, Any], source_bytes: bytes) -> TapirSnapshotIdentity:
    """Project validated metadata and exact source bytes into durable identity."""

    inputs = cast(dict[str, Any], metadata["inputs"])
    observed_majors: tuple[int, ...] = ()
    observed_platforms: tuple[str, ...] = ()
    if str(metadata.get("distribution", PACKAGED_DISTRIBUTION)) == CACHE_DISTRIBUTION:
        assets = validate_observed_assets(metadata["observed_assets"])
        observed_majors = tuple(int(major) for major in assets["majors"])
        observed_platforms = tuple(str(platform) for platform in assets["platforms"])
    return TapirSnapshotIdentity(
        version=str(metadata["provider_version"]),
        distribution=str(metadata.get("distribution", PACKAGED_DISTRIBUTION)),
        package_path=str(metadata["package_path"]),
        upstream_repository=str(metadata["upstream_repository"]),
        upstream_tag=str(metadata["upstream_tag"]),
        upstream_commit=str(metadata["upstream_commit"]),
        license_name=str(metadata["license"]),
        source_sha256=sha256_hex(source_bytes),
        input_hashes={str(name): str(inputs[name]) for name in sorted(inputs)},
        observed_majors=observed_majors,
        observed_platforms=observed_platforms,
    )


def _validate_snapshot_semantics(
    commands: dict[str, Any], common_schemas: dict[str, Any], provider_version: str
) -> None:
    """Validate the canonical command records a snapshot reader consumes."""

    if not commands or not common_schemas:
        _metadata_refuse("invalid_snapshot_shape")
    for mapping_name, schema in common_schemas.items():
        if (
            type(mapping_name) is not str
            or not mapping_name
            or len(mapping_name) > MAX_NAME_LENGTH
            or mapping_name != mapping_name.strip()
            or _has_surrogate(mapping_name)
            or any(
                ord(character) < _CONTROL_CHARACTER_LIMIT or ord(character) == 0x7F
                for character in mapping_name
            )
            or type(schema) is not dict
        ):
            _metadata_refuse("invalid_snapshot_shape")
    for mapping_name, record_value in commands.items():
        if type(record_value) is not dict:
            _metadata_refuse("invalid_snapshot_shape")
        record = cast(dict[str, Any], record_value)
        name = record.get("name")
        category = record.get("category")
        description = record.get("description")
        version = record.get("version")
        if (
            frozenset(record) != SNAPSHOT_COMMAND_KEYS
            or type(mapping_name) is not str
            or name != mapping_name
            or type(name) is not str
            or len(name) > MAX_NAME_LENGTH
            or _COMMAND_NAME_RE.fullmatch(name) is None
            or type(category) is not str
            or not category
            or len(category) > MAX_NAME_LENGTH
            or category != category.strip()
            or _has_surrogate(category)
            or any(
                ord(character) < _CONTROL_CHARACTER_LIMIT or ord(character) == 0x7F
                for character in category
            )
            or type(description) is not str
            or len(description) > MAX_DESCRIPTION_LENGTH
            or _has_surrogate(description)
            or record.get("api") != PROVIDER_IDENTITY
            or (record.get("parameters") is not None and type(record["parameters"]) is not dict)
            or (record.get("returns") is not None and type(record["returns"]) is not dict)
        ):
            _metadata_refuse("invalid_snapshot_commands")
        try:
            validate_semver(version)
            if compare_semver(version, provider_version) > 0:
                _metadata_refuse("invalid_snapshot_commands")
        except SemverValidationError as exc:
            raise TapirSnapshotMetadataError("invalid_snapshot_commands") from exc
    try:
        _validate_ref_closure(cast(dict[str, dict[str, Any]], commands), common_schemas)
        _validate_get_add_on_version(cast(dict[str, dict[str, Any]], commands))
    except TapirSourceError as exc:
        raise TapirSnapshotMetadataError("invalid_snapshot_commands") from exc


def _validate_document_root(payload: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one canonical serialized snapshot document end to end.

    Requires the exact top-level key set, byte-identical canonical
    serialization (rejecting noncanonical floats or formatting), typed
    containers, ElementType projection consistency, validated metadata with
    its distribution-specific provenance pins. Registry closure is applied by
    the calling reader through :func:`load_provider_snapshot`.
    """

    if not payload or len(payload) > SNAPSHOT_MAX_BYTES:
        _metadata_refuse("snapshot-size")
    try:
        root = strict_json_bytes(payload)
    except TapirSourceError as exc:
        raise TapirSnapshotMetadataError("malformed_tapir_json") from exc
    if type(root) is not dict or frozenset(root) != SNAPSHOT_DOCUMENT_KEYS:
        _metadata_refuse("unexpected_snapshot_keys")
    root_mapping = cast(dict[str, Any], root)
    try:
        canonical = (json.dumps(root_mapping, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TapirSnapshotMetadataError("noncanonical_snapshot_bytes") from exc
    if canonical != payload:
        _metadata_refuse("noncanonical_snapshot_bytes")
    commands = root_mapping["commands"]
    common_schemas = root_mapping["common_schemas"]
    element_types = root_mapping["element_types"]
    if type(commands) is not dict or not all(isinstance(v, dict) for v in commands.values()):
        _metadata_refuse("invalid_snapshot_shape")
    if type(common_schemas) is not dict or not all(
        isinstance(v, dict) for v in common_schemas.values()
    ):
        _metadata_refuse("invalid_snapshot_shape")
    if (
        type(element_types) is not list
        or not element_types
        or any(
            type(name) is not str
            or not name
            or len(name) > MAX_NAME_LENGTH
            or name != name.strip()
            or _has_surrogate(name)
            or any(
                ord(character) < _CONTROL_CHARACTER_LIMIT or ord(character) == 0x7F
                for character in name
            )
            for name in element_types
        )
    ):
        _metadata_refuse("invalid_snapshot_shape")
    element_schema = common_schemas.get(ELEMENT_TYPES_SOURCE_SCHEMA)
    if (
        type(element_schema) is not dict
        or element_schema.get("enum") != element_types
        or len(set(element_types)) != len(element_types)
    ):
        _metadata_refuse("inconsistent_element_types")
    metadata_value = root_mapping["_metadata"]
    if type(metadata_value) is not dict:
        _metadata_refuse("malformed_tapir_metadata")
    metadata = validate_tapir_metadata(metadata_value)
    _validate_snapshot_semantics(
        cast(dict[str, Any], commands),
        cast(dict[str, Any], common_schemas),
        str(metadata["provider_version"]),
    )
    return root_mapping, metadata


def validate_snapshot_document(payload: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Public strict snapshot-document reader used by every distribution."""

    return _validate_document_root(payload)


def load_tapir_identity(source_bytes: bytes) -> tuple[str, tuple[str, ...], TapirSnapshotIdentity]:
    """Validate exact snapshot bytes; return version, provenance, and identity."""

    _root, metadata = _validate_document_root(source_bytes)
    return (
        str(metadata["provider_version"]),
        tapir_provenance(metadata),
        identity_from_metadata(metadata, source_bytes),
    )


def load_packaged_identity(
    source_bytes: bytes,
) -> tuple[str, tuple[str, ...], TapirSnapshotIdentity]:
    """Validate exact packaged snapshot bytes; derive pinned registry identity.

    The packaged reader additionally pins the metadata to the tracked
    1.5.8 release-generator provenance; drift fails closed.
    """

    _root, metadata = _validate_document_root(source_bytes)
    require_packaged_provenance(metadata)
    return (
        str(metadata["provider_version"]),
        tapir_provenance(metadata),
        identity_from_metadata(metadata, source_bytes),
    )


def verify_license_identity(license_bytes: bytes, expected_sha256: str) -> None:
    """Fail closed unless LICENSE bytes retain the pinned upstream identity."""

    validate_source_size("LICENSE", license_bytes)
    if sha256_hex(license_bytes) != expected_sha256:
        _refuse("license-identity-drift")


def _clean_text(value: object, code: str, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or _has_surrogate(value)
        or any(
            ord(character) < _CONTROL_CHARACTER_LIMIT or ord(character) == 0x7F
            for character in value
        )
    ):
        _refuse(code)
    return value


def _validate_command_name(name: object) -> str:
    cleaned = _clean_text(name, "command-name", maximum=MAX_NAME_LENGTH)
    if _COMMAND_NAME_RE.fullmatch(cleaned) is None:
        _refuse("command-name")
    return cleaned


SOURCE_CATEGORY_KEYS: Final[frozenset[str]] = frozenset({"name", "commands"})
SOURCE_COMMAND_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "version", "description", "inputScheme", "outputScheme"}
)


def _command_entries(categories: object, provider_version: str) -> dict[str, dict[str, Any]]:
    """Validate exact category/command shapes and build canonical records."""

    if not isinstance(categories, list) or not categories:
        _refuse("commands-envelope")
    commands: dict[str, dict[str, Any]] = {}
    seen_categories: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            _refuse("category-shape")
        category_value = cast(dict[str, Any], category)
        if frozenset(category_value) != SOURCE_CATEGORY_KEYS:
            _refuse("category-shape")
        category_name = _clean_text(
            category_value.get("name"), "category-shape", maximum=MAX_NAME_LENGTH
        )
        entries = category_value.get("commands")
        if category_name in seen_categories or not isinstance(entries, list):
            _refuse("category-shape")
        seen_categories.add(category_name)
        for entry in entries:
            if not isinstance(entry, dict):
                _refuse("command-shape")
            record = _command_record(
                cast(dict[str, Any], entry), category_name, commands, provider_version
            )
            commands[name_key(record)] = record
    return commands


def name_key(record: dict[str, Any]) -> str:
    """Return the validated command name stored in one canonical record."""

    name = record["name"]
    assert type(name) is str
    return name


def _command_record(
    entry: dict[str, Any],
    category_name: str,
    commands: dict[str, dict[str, Any]],
    provider_version: str,
) -> dict[str, Any]:
    if frozenset(entry) != SOURCE_COMMAND_KEYS:
        _refuse("command-shape")
    name = _validate_command_name(entry["name"])
    if name in commands:
        _refuse(f"duplicate-command:{name}")
    description = entry["description"]
    if (
        type(description) is not str
        or len(description) > MAX_DESCRIPTION_LENGTH
        or _has_surrogate(description)
    ):
        _refuse("command-shape")
    for field in ("inputScheme", "outputScheme"):
        scheme = entry[field]
        if scheme is not None and type(scheme) is not dict:
            _refuse("command-shape")
    version = entry["version"]
    try:
        validate_semver(version)
        above_provider = compare_semver(version, provider_version) > 0
    except SemverValidationError as exc:
        raise TapirSourceError(f"invalid-command-version:{name}") from exc
    if above_provider:
        _refuse(f"command-version-above-provider:{name}")
    record: dict[str, Any] = {
        "category": category_name,
        "description": description,
        "version": version,
    }
    if entry["inputScheme"] is not None:
        record["parameters"] = _normalize_value(entry["inputScheme"], f"{name}.parameters")
    else:
        record["parameters"] = None
    if entry["outputScheme"] is not None:
        record["returns"] = _normalize_value(entry["outputScheme"], f"{name}.returns")
    else:
        record["returns"] = None
    record["api"] = PROVIDER_IDENTITY
    record["name"] = name
    return record


def _pointer_target(ref: str) -> str:
    """Decode one single-token local JSON pointer, refusing any other shape."""

    if not ref.startswith("#/"):
        _refuse(f"external-reference:{ref!r}")
    token = ref[2:]
    if not token or "/" in token:
        _refuse(f"invalid-json-pointer:{ref}")
    decoded: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character == "~":
            if index + 1 >= len(token) or token[index + 1] not in ("0", "1"):
                _refuse(f"invalid-json-pointer:{ref}")
            decoded.append("/" if token[index + 1] == "1" else "~")
            index += 2
            continue
        decoded.append(character)
        index += 1
    return "".join(decoded)


def _collect_refs(node: object, targets: set[str]) -> None:
    """Collect local one-token ``#/`` reference targets into ``targets``."""

    if isinstance(node, dict):
        for key, child in cast(dict[str, Any], node).items():
            if key == "$ref":
                if type(child) is not str:
                    _refuse(f"external-reference:{child!r}")
                targets.add(_pointer_target(child))
                continue
            _collect_refs(child, targets)
    elif isinstance(node, list):
        for child in node:
            _collect_refs(child, targets)


def _validate_ref_closure(
    commands: dict[str, dict[str, Any]], common_schemas: dict[str, Any]
) -> None:
    """Require every local ``$ref`` to close inside the shared definitions."""

    definition_names = frozenset(common_schemas)
    roots: list[tuple[str, object]] = [
        *(("command", payload) for payload in commands.values()),
        *((name, schema) for name, schema in common_schemas.items()),
    ]
    for origin, payload in roots:
        targets: set[str] = set()
        _collect_refs(payload, targets)
        missing = sorted(target for target in targets if target not in definition_names)
        if missing:
            _refuse(f"dangling-reference:{origin}:{missing[0]}")


def _common_schemas_document(raw_common: object) -> tuple[dict[str, Any], list[str]]:
    """Normalize shared definitions and extract the exact ElementType enum."""

    if not isinstance(raw_common, dict) or not raw_common:
        _refuse("common-schemas-envelope")
    common_raw = cast(dict[str, Any], raw_common)
    for name, schema in common_raw.items():
        _clean_text(name, "common-schema-shape", maximum=MAX_NAME_LENGTH)
        if not isinstance(schema, dict):
            _refuse("common-schema-shape")
    common_schemas = {
        name: _normalize_value(schema, f"common.{name}") for name, schema in common_raw.items()
    }
    element_types_schema = common_schemas.get(ELEMENT_TYPES_SOURCE_SCHEMA)
    element_types = (
        element_types_schema.get("enum") if isinstance(element_types_schema, dict) else None
    )
    if (
        not isinstance(element_types, list)
        or not element_types
        or any(type(element_type) is not str for element_type in element_types)
    ):
        _refuse(f"missing-{ELEMENT_TYPES_SOURCE_SCHEMA}-enum")
    names = [
        _clean_text(
            item,
            f"missing-{ELEMENT_TYPES_SOURCE_SCHEMA}-enum",
            maximum=MAX_NAME_LENGTH,
        )
        for item in element_types
    ]
    if len(set(names)) != len(names):
        _refuse(f"missing-{ELEMENT_TYPES_SOURCE_SCHEMA}-enum")
    return common_schemas, names


def _validate_get_add_on_version(commands: dict[str, dict[str, Any]]) -> None:
    """Require the exact null-input/version-output contract of GetAddOnVersion."""

    command = commands.get(REQUIRED_COMMAND_NAME)
    if command is None:
        _refuse(f"missing-command:{REQUIRED_COMMAND_NAME}")
    if command.get("parameters") is not None:
        _refuse("invalid-version-input")
    returns = command.get("returns")
    if not isinstance(returns, dict):
        _refuse("missing-version-output")
    required = returns.get("required")
    properties = returns.get("properties")
    if (
        returns.get("type") != "object"
        or not isinstance(required, list)
        or "version" not in required
        or not isinstance(properties, dict)
        or not isinstance(properties.get("version"), dict)
        or cast(dict[str, Any], properties["version"]).get("type") != "string"
    ):
        _refuse("missing-version-output")


def assemble_tapir_document(
    categories: object, raw_common: object, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Validate parsed upstream values and assemble the canonical document.

    This is the single shared assembly path: shapes, versions, reference
    closure, and the required GetAddOnVersion contract are all enforced here,
    so no entry point can bypass them.
    """

    validated_metadata = validate_tapir_metadata(metadata)
    common_schemas, element_types = _common_schemas_document(raw_common)
    commands = _command_entries(categories, str(validated_metadata["provider_version"]))
    _validate_ref_closure(commands, common_schemas)
    _validate_get_add_on_version(commands)
    return {
        "commands": commands,
        "element_types": element_types,
        "common_schemas": common_schemas,
        "_metadata": validated_metadata,
    }


def snapshot_metadata(
    *,
    provider_version: str,
    package_path: str,
    upstream_repository: str,
    upstream_tag: str,
    upstream_commit: str,
    license_name: str,
    input_hashes: dict[str, str],
    generator: str = GENERATOR_IDENTITY,
    distribution: str | None = None,
    observed_assets: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical ``_metadata`` record for a transformed snapshot.

    ``distribution=None`` produces the packaged key set without a distribution
    field; any other value embeds the explicit distribution marker plus the
    observed-asset record required for user-cache documents.
    """

    metadata: dict[str, Any] = {
        "format": TAPIR_METADATA_FORMAT,
        "provider": PROVIDER_IDENTITY,
        "provider_version": provider_version,
    }
    if distribution is not None:
        metadata["distribution"] = distribution
        metadata["observed_assets"] = dict(observed_assets or {"majors": [], "platforms": []})
    metadata.update(
        {
            "package_path": package_path,
            "upstream_repository": upstream_repository,
            "upstream_tag": upstream_tag,
            "upstream_commit": upstream_commit,
            "license": license_name,
            "inputs": dict(sorted(input_hashes.items())),
            "generator": generator,
        }
    )
    return metadata


def transform_inputs(
    command_definitions: bytes,
    common_schema_definitions: bytes,
    *,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Validate exact upstream inputs and normalize the canonical document.

    This is the one shared runtime-and-maintainer transform: exact envelopes,
    strict JSON, declared input hashes, shapes, versions, reference closure,
    and the GetAddOnVersion contract are all enforced here.
    """

    validate_source_size("command_definitions.js", command_definitions)
    validate_source_size("common_schema_definitions.js", common_schema_definitions)
    declared = validate_tapir_metadata(metadata)["inputs"]
    computed = {
        "command_definitions.js": sha256_hex(command_definitions),
        "common_schema_definitions.js": sha256_hex(common_schema_definitions),
    }
    for name in sorted(computed):
        if cast(dict[str, Any], declared)[name] != computed[name]:
            _refuse(f"input-sha256-mismatch:{name}")
    categories = parse_command_definitions(command_definitions)
    raw_common = parse_common_schema_definitions(common_schema_definitions)
    return assemble_tapir_document(categories, raw_common, metadata)


def serialize_tapir_snapshot(document: dict[str, Any]) -> bytes:
    """Emit canonical deterministic bytes validated through the immutable registry."""

    try:
        text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        payload = text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        _refuse(f"document-envelope:{type(exc).__name__}")
    if len(payload) > SNAPSHOT_MAX_BYTES:
        _refuse("snapshot-size")
    try:
        _root, metadata = _validate_document_root(payload)
    except TapirSnapshotMetadataError as exc:
        code = exc.code
        _refuse(f"validation-failed:{code}")
    provider_version = str(metadata["provider_version"])
    try:
        snapshot = load_provider_snapshot(
            payload,
            provider=PROVIDER_IDENTITY,
            provider_version=provider_version,
            distribution=(
                f"{metadata.get('distribution', PACKAGED_DISTRIBUTION)} {metadata['package_path']}"
            ),
            provenance=tapir_provenance(metadata),
        )
    except SchemaRegistryError as exc:
        _refuse(f"validation-failed:{exc.code}")
    expected_commands = len(document["commands"])
    if (
        snapshot.command_count != expected_commands
        or len(snapshot.capability_ids) != expected_commands
        or snapshot.definition_count != len(document["common_schemas"])
        or snapshot.provider_version != provider_version
    ):
        _refuse("snapshot-identity-mismatch")
    return payload


__all__ = [
    "CACHE_DISTRIBUTION",
    "CACHE_PACKAGE_PATH",
    "ELEMENT_TYPES_SOURCE_SCHEMA",
    "GENERATOR_IDENTITY",
    "GENERATOR_SCRIPT_IDENTITY",
    "LICENSE_MAX_BYTES",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_JSON_NUMBER_LENGTH",
    "MAX_NAME_LENGTH",
    "OBSERVED_ASSET_KEYS",
    "OBSERVED_ASSET_PLATFORMS",
    "PACKAGED_DISTRIBUTION",
    "PACKAGED_INPUT_SHA256",
    "PACKAGED_PACKAGE_PATH",
    "PACKAGED_PROVIDER_VERSION",
    "PACKAGED_UPSTREAM_COMMIT",
    "PINNED_LICENSE_NAME",
    "PINNED_LICENSE_SHA256",
    "PROVIDER_IDENTITY",
    "SNAPSHOT_COMMAND_KEYS",
    "SNAPSHOT_DOCUMENT_KEYS",
    "SNAPSHOT_MAX_BYTES",
    "SOURCE_CATEGORY_KEYS",
    "SOURCE_COMMAND_KEYS",
    "SOURCE_MAX_BYTES",
    "TAPIR_CACHE_METADATA_KEYS",
    "TAPIR_METADATA_FORMAT",
    "TAPIR_PACKAGED_METADATA_KEYS",
    "TAPIR_UPSTREAM_REPOSITORY",
    "TapirSnapshotIdentity",
    "TapirSnapshotMetadataError",
    "TapirSourceError",
    "assemble_tapir_document",
    "identity_from_metadata",
    "load_packaged_identity",
    "load_tapir_identity",
    "normalize_marked_numbers",
    "parse_command_definitions",
    "parse_common_schema_definitions",
    "require_distribution",
    "require_packaged_provenance",
    "serialize_tapir_snapshot",
    "sha256_hex",
    "snapshot_metadata",
    "strict_json_bytes",
    "strict_json_text",
    "tapir_provenance",
    "transform_inputs",
    "validate_observed_assets",
    "validate_snapshot_document",
    "validate_source_size",
    "validate_tapir_metadata",
    "verify_license_identity",
]
