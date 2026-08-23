#!/usr/bin/env python3
"""Verify the wheel and source distribution that form one release artifact set."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import itertools
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass, replace
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import cast

_STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION = re.compile(
    r"""
    ^
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)
_WHEEL_TAG_COMPONENT = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$")
_BUILD_TAG = re.compile(r"^[0-9][A-Za-z0-9_]*$")
_CANONICAL_SIZE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SUPPORTED_CORE_METADATA = frozenset({"2.1", "2.2", "2.3", "2.4", "2.5"})
_MINIMUM_SDIST_CORE_METADATA = "2.2"
_PEP639_CORE_METADATA_VERSION = (2, 4)
_SUPPORTED_WHEEL_VERSION = "1.0"
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_LICENSE_FILE_BYTES = 1024 * 1024
_MAX_WHEEL_HEADER_BYTES = 64 * 1024
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_MAX_PYPROJECT_BYTES = 1024 * 1024
_MIN_MEMBER_BYTES = 2 * 1024 * 1024
_MIN_TOTAL_BYTES = 8 * 1024 * 1024
_HARD_MAX_MEMBERS = 10_000
_HARD_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_HARD_MAX_TOTAL_BYTES = 512 * 1024 * 1024


class VerificationError(ValueError):
    """Report an invalid or ambiguous release artifact set."""


@dataclass(frozen=True)
class ParsedWheelName:
    """Strictly parsed wheel filename components."""

    distribution: str
    version: str
    build_tag: str | None
    python_tag: str
    abi_tag: str
    platform_tag: str

    @property
    def dist_info(self) -> str:
        """Return the required wheel metadata directory name."""
        return f"{self.distribution}-{self.version}.dist-info"


@dataclass(frozen=True)
class ParsedSdistName:
    """Strictly parsed source-distribution filename components."""

    distribution: str
    version: str

    @property
    def root(self) -> str:
        """Return the required source-distribution archive root."""
        return f"{self.distribution}-{self.version}"


@dataclass(frozen=True)
class EmbeddedMetadata:
    """Core metadata read from one distribution artifact."""

    path: Path
    project_name: str
    version: str
    metadata_version: str
    license_expression: str | None
    license_files: tuple[str, ...]
    license_file_digests: tuple[tuple[str, str], ...]
    metadata_member: str
    sha256: str


@dataclass(frozen=True)
class VerificationResult:
    """Verified release artifact names and their shared metadata."""

    project_name: str
    version: str
    wheel: str
    sdist: str
    artifact_set_sha256: str


@dataclass(frozen=True)
class ArchiveLimits:
    """Bounds derived from an archive's compressed size, with hard ceilings."""

    members: int
    member_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class WheelMembers:
    """Validated wheel members required for structural verification."""

    metadata: zipfile.ZipInfo
    wheel: zipfile.ZipInfo
    record: zipfile.ZipInfo
    regular: dict[str, zipfile.ZipInfo]


@dataclass(frozen=True)
class SdistMembers:
    """Validated source-distribution members required for rebuildability checks."""

    metadata: tarfile.TarInfo
    pyproject: tarfile.TarInfo
    regular: dict[str, tarfile.TarInfo]


def _require_safe_text(value: str, source: str, *, allow_empty: bool = False) -> str:
    if (not value and not allow_empty) or _ASCII_CONTROL.search(value) is not None:
        raise VerificationError(f"{source} contains an empty value or ASCII control character")
    return value


def canonicalize_project_name(name: str) -> str:
    """Return the normalized project name used for metadata comparisons."""
    _require_safe_text(name, "project name")
    if _PROJECT_NAME.fullmatch(name) is None:
        raise VerificationError(f"invalid project name: {name!r}")
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_distribution_name(name: str) -> str:
    return canonicalize_project_name(name).replace("-", "_")


def _canonicalize_version(version: str, source: str) -> str:
    _require_safe_text(version, source)
    match = _VERSION.fullmatch(version)
    if match is None:
        raise VerificationError(f"{source} is not a valid PEP 440 version: {version!r}")

    epoch = int(match.group("epoch") or "0")
    release = ".".join(str(int(component)) for component in match.group("release").split("."))
    canonical = f"{epoch}!{release}" if epoch else release

    pre_label = match.group("pre_l")
    if pre_label is not None:
        normalized_pre = {
            "a": "a",
            "alpha": "a",
            "b": "b",
            "beta": "b",
            "c": "rc",
            "pre": "rc",
            "preview": "rc",
            "rc": "rc",
        }[pre_label.lower()]
        canonical += f"{normalized_pre}{int(match.group('pre_n') or '0')}"

    post_number = match.group("post_n1") or match.group("post_n2")
    if match.group("post") is not None:
        canonical += f".post{int(post_number or '0')}"

    if match.group("dev") is not None:
        canonical += f".dev{int(match.group('dev_n') or '0')}"

    local = match.group("local")
    if local is not None:
        canonical += "+" + re.sub(r"[-_]", ".", local.lower())

    if version != canonical:
        raise VerificationError(f"{source} is not in canonical PEP 440 form: {version!r}")
    return canonical


def stable_version_from_tag(tag: str) -> str:
    """Return the version in an exact stable SemVer tag."""
    _require_safe_text(tag, "tag")
    match = _STABLE_TAG.fullmatch(tag)
    if match is None:
        raise VerificationError(f"tag is not exact stable SemVer: {tag!r}")
    return ".".join(match.groups())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_limits(path: Path) -> ArchiveLimits:
    compressed_bytes = path.stat().st_size
    if compressed_bytes < 1:
        raise VerificationError(f"archive is empty: {path.name}")
    return ArchiveLimits(
        members=min(_HARD_MAX_MEMBERS, 64 + compressed_bytes // 512),
        member_bytes=min(
            _HARD_MAX_MEMBER_BYTES,
            max(_MIN_MEMBER_BYTES, compressed_bytes * 128),
        ),
        total_bytes=min(
            _HARD_MAX_TOTAL_BYTES,
            max(_MIN_TOTAL_BYTES, compressed_bytes * 256),
        ),
    )


def _validate_member_size(
    path: Path,
    member_name: str,
    member_size: int,
    limits: ArchiveLimits,
) -> None:
    if member_size < 0 or member_size > limits.member_bytes:
        raise VerificationError(
            f"{path.name}:{member_name} exceeds the proportional member-size bound"
        )


def _parse_headers(payload: bytes, source: str, maximum: int) -> Message:
    if len(payload) > maximum:
        raise VerificationError(f"embedded metadata is unexpectedly large in {source}")
    message = BytesParser(policy=policy.compat32).parsebytes(payload, headersonly=True)
    if message.defects:
        raise VerificationError(f"{source} contains malformed metadata headers")
    return message


def _metadata_values(message: Message, field: str, source: str) -> list[str]:
    values = [str(value) for value in message.get_all(field) or []]
    for value in values:
        if not value or value != value.strip():
            raise VerificationError(f"{source} contains an invalid {field} field")
        _require_safe_text(value, f"{source} {field} field")
    return values


def _single_header_value(message: Message, field: str, source: str) -> str:
    values = _metadata_values(message, field, source)
    if len(values) != 1:
        raise VerificationError(f"{source} must contain exactly one {field} field")
    return values[0]


def _archive_parts(name: str, source: str) -> tuple[str, ...]:
    _require_safe_text(name, source)
    if "\\" in name or name.startswith("/"):
        raise VerificationError(f"{source} is not a safe relative POSIX archive path")
    logical_name = name[:-1] if name.endswith("/") else name
    if not logical_name:
        raise VerificationError(f"{source} is empty")
    raw_parts = logical_name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts) or ":" in raw_parts[0]:
        raise VerificationError(f"{source} contains path traversal or an ambiguous component")
    path = PurePosixPath(logical_name)
    if path.is_absolute() or tuple(path.parts) != tuple(raw_parts):
        raise VerificationError(f"{source} is not a canonical relative POSIX archive path")
    return tuple(raw_parts)


def _parse_core_metadata(
    path: Path,
    member: str,
    payload: bytes,
    *,
    minimum_metadata_version: str | None = None,
) -> EmbeddedMetadata:
    source = f"{path.name}:{member}"
    message = _parse_headers(payload, source, _MAX_METADATA_BYTES)
    metadata_version = _single_header_value(message, "Metadata-Version", source)
    if metadata_version not in _SUPPORTED_CORE_METADATA:
        raise VerificationError(
            f"{source} uses unsupported Core Metadata-Version {metadata_version!r}"
        )
    metadata_version_parts = tuple(int(part) for part in metadata_version.split("."))
    if minimum_metadata_version is not None and (
        metadata_version_parts < tuple(int(part) for part in minimum_metadata_version.split("."))
    ):
        raise VerificationError(
            f"{source} requires Core Metadata-Version >= {minimum_metadata_version}"
        )
    project_name = _single_header_value(message, "Name", source)
    version = _single_header_value(message, "Version", source)
    canonicalize_project_name(project_name)
    _canonicalize_version(version, f"{path.name} metadata version")

    if metadata_version_parts < _PEP639_CORE_METADATA_VERSION and (
        message.get_all("License-Expression") is not None
        or message.get_all("License-File") is not None
    ):
        raise VerificationError(
            f"{source} uses License-Expression or License-File, which requires "
            "Core Metadata-Version >= 2.4"
        )

    license_expressions = _metadata_values(message, "License-Expression", source)
    if len(license_expressions) > 1:
        raise VerificationError(f"{source} contains more than one License-Expression field")
    license_expression = license_expressions[0] if license_expressions else None

    license_files = _metadata_values(message, "License-File", source)
    if len(set(license_files)) != len(license_files):
        raise VerificationError(f"{source} contains duplicate License-File fields")
    for license_file in license_files:
        if license_file.endswith("/"):
            raise VerificationError(f"{source} contains an invalid License-File path")
        _archive_parts(license_file, f"{source} License-File")

    return EmbeddedMetadata(
        path,
        project_name,
        version,
        metadata_version,
        license_expression,
        tuple(license_files),
        (),
        member,
        _sha256(path),
    )


def _parse_wheel_filename(path: Path) -> ParsedWheelName:
    _require_safe_text(path.name, "wheel filename")
    if not path.name.endswith(".whl"):
        raise VerificationError(f"malformed wheel filename: {path.name}")
    parts = path.name.removesuffix(".whl").split("-")
    if len(parts) == 5:
        distribution, version, python_tag, abi_tag, platform_tag = parts
        build_tag = None
    elif len(parts) == 6:
        distribution, version, build_tag, python_tag, abi_tag, platform_tag = parts
        if _BUILD_TAG.fullmatch(build_tag) is None:
            raise VerificationError(f"malformed wheel build tag: {path.name}")
    else:
        raise VerificationError(f"malformed wheel filename: {path.name}")

    if re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*", distribution) is None:
        raise VerificationError(f"malformed wheel distribution component: {path.name}")
    _canonicalize_version(version, f"wheel filename {path.name!r} version")
    if any(
        _WHEEL_TAG_COMPONENT.fullmatch(tag) is None for tag in (python_tag, abi_tag, platform_tag)
    ):
        raise VerificationError(f"malformed wheel compatibility tag: {path.name}")
    return ParsedWheelName(distribution, version, build_tag, python_tag, abi_tag, platform_tag)


def _parse_sdist_filename(path: Path) -> ParsedSdistName:
    _require_safe_text(path.name, "source distribution filename")
    if not path.name.endswith(".tar.gz"):
        raise VerificationError(f"malformed source distribution filename: {path.name}")
    parts = path.name.removesuffix(".tar.gz").split("-")
    if len(parts) != 2:
        raise VerificationError(f"malformed source distribution filename: {path.name}")
    distribution, version = parts
    if re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*", distribution) is None:
        raise VerificationError(f"malformed source distribution project component: {path.name}")
    _canonicalize_version(version, f"source distribution filename {path.name!r} version")
    return ParsedSdistName(distribution, version)


def _validate_zip_members(
    archive: zipfile.ZipFile,
    path: Path,
    required_dist_info: str,
) -> WheelMembers:
    limits = _archive_limits(path)
    members = archive.infolist()
    if len(members) > limits.members:
        raise VerificationError(f"{path.name} exceeds the proportional archive member-count bound")
    total_bytes = 0
    for member in members:
        _validate_member_size(path, member.filename, member.file_size, limits)
        total_bytes += member.file_size
        if total_bytes > limits.total_bytes:
            raise VerificationError(
                f"{path.name} exceeds the proportional total-uncompressed-size bound"
            )

    seen: set[str] = set()
    regular: dict[str, zipfile.ZipInfo] = {}
    for member in members:
        logical_name = member.filename.removesuffix("/")
        if logical_name in seen:
            raise VerificationError(
                f"{path.name} contains a duplicate archive member: {logical_name}"
            )
        seen.add(logical_name)
        parts = _archive_parts(member.filename, f"{path.name} archive member")

        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if member.is_dir():
            if file_type not in {0, stat.S_IFDIR}:
                raise VerificationError(f"{path.name}:{member.filename} has an invalid member type")
        elif file_type not in {0, stat.S_IFREG}:
            raise VerificationError(f"{path.name}:{member.filename} is not a regular file")
        else:
            regular[member.filename] = member
        if member.flag_bits & 0x1:
            raise VerificationError(f"{path.name}:{member.filename} is encrypted")

        dist_info_components = [part for part in parts if part.endswith(".dist-info")]
        if dist_info_components and (
            len(dist_info_components) != 1 or parts[0] != required_dist_info
        ):
            raise VerificationError(
                f"{path.name} contains an unrelated or nested .dist-info directory"
            )

    required_names = {
        "metadata": f"{required_dist_info}/METADATA",
        "wheel": f"{required_dist_info}/WHEEL",
        "record": f"{required_dist_info}/RECORD",
    }
    missing = [name for name in required_names.values() if name not in regular]
    if missing:
        raise VerificationError(
            f"{path.name} must contain exactly one matching METADATA, WHEEL, and RECORD"
        )
    return WheelMembers(
        metadata=regular[required_names["metadata"]],
        wheel=regular[required_names["wheel"]],
        record=regular[required_names["record"]],
        regular=regular,
    )


def _read_zip_payload(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    maximum: int,
    source: str,
) -> bytes:
    with archive.open(member) as stream:
        payload = stream.read(maximum + 1)
    if len(payload) > maximum or len(payload) != member.file_size:
        raise VerificationError(f"{source} exceeds its bounded declared size")
    return payload


def _validate_utf8_text(payload: bytes, source: str) -> None:
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{source} is not strict UTF-8 text") from exc


def _expanded_filename_tags(parsed: ParsedWheelName) -> set[str]:
    return {
        "-".join(parts)
        for parts in itertools.product(
            parsed.python_tag.split("."),
            parsed.abi_tag.split("."),
            parsed.platform_tag.split("."),
        )
    }


def _validate_wheel_headers(payload: bytes, path: Path, parsed: ParsedWheelName) -> None:
    source = f"{path.name}:{parsed.dist_info}/WHEEL"
    message = _parse_headers(payload, source, _MAX_WHEEL_HEADER_BYTES)
    wheel_version = _single_header_value(message, "Wheel-Version", source)
    if wheel_version != _SUPPORTED_WHEEL_VERSION:
        raise VerificationError(f"{source} uses unsupported Wheel-Version {wheel_version!r}")
    purelib = _single_header_value(message, "Root-Is-Purelib", source)
    if purelib != "true":
        raise VerificationError(f"{source} must declare Root-Is-Purelib: true")
    if parsed.abi_tag != "none" or parsed.platform_tag != "any":
        raise VerificationError(f"{path.name} is not the expected pure-Python wheel")

    build_values = _metadata_values(message, "Build", source)
    if parsed.build_tag is None and build_values:
        raise VerificationError(f"{source} must not contain a Build field for an untagged wheel")
    if parsed.build_tag is not None and build_values != [parsed.build_tag]:
        raise VerificationError(
            f"{source} must contain exactly one Build field equal to {parsed.build_tag!r}"
        )

    tags = _metadata_values(message, "Tag", source)
    if not tags or len(tags) != len(set(tags)):
        raise VerificationError(f"{source} must contain unique compatibility Tag fields")
    for tag in tags:
        components = tag.split("-")
        if len(components) != 3 or any(
            re.fullmatch(r"[A-Za-z0-9_]+", component) is None for component in components
        ):
            raise VerificationError(f"{source} contains a malformed compatibility Tag")
    if set(tags) != _expanded_filename_tags(parsed):
        raise VerificationError(f"{source} compatibility Tags do not match the wheel filename")


def _zip_member_digest(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(member) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    if size != member.file_size:
        raise VerificationError(f"wheel member size changed while reading: {member.filename}")
    encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    return encoded, size


def _validate_wheel_record(
    archive: zipfile.ZipFile,
    path: Path,
    parsed: ParsedWheelName,
    members: WheelMembers,
) -> None:
    source = f"{path.name}:{members.record.filename}"
    payload = _read_zip_payload(archive, members.record, _MAX_RECORD_BYTES, source)
    try:
        text = payload.decode("utf-8", errors="strict")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise VerificationError(f"{source} is not strict UTF-8 CSV") from exc

    record_rows: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(rows, start=1):
        if len(row) != 3:
            raise VerificationError(f"{source} row {index} does not contain exactly three fields")
        member_name, encoded_hash, encoded_size = row
        _archive_parts(member_name, f"{source} row {index} path")
        if member_name in record_rows:
            raise VerificationError(f"{source} contains a duplicate row for {member_name!r}")
        _require_safe_text(encoded_hash, f"{source} row {index} hash", allow_empty=True)
        _require_safe_text(encoded_size, f"{source} row {index} size", allow_empty=True)
        record_rows[member_name] = (encoded_hash, encoded_size)

    signatures = {
        f"{parsed.dist_info}/RECORD.jws",
        f"{parsed.dist_info}/RECORD.p7s",
    }
    expected_rows = set(members.regular).difference(signatures)
    observed_rows = set(record_rows)
    if observed_rows != expected_rows:
        extras = sorted(observed_rows.difference(expected_rows))
        missing = sorted(expected_rows.difference(observed_rows))
        raise VerificationError(
            f"{source} rows do not exactly match regular wheel files; "
            f"extras={extras!r}, missing={missing!r}"
        )

    record_hash, record_size = record_rows[members.record.filename]
    if record_hash or record_size:
        raise VerificationError(f"{source} must leave its own hash and size empty")

    for member_name in sorted(expected_rows.difference({members.record.filename})):
        encoded_hash, encoded_size = record_rows[member_name]
        if not encoded_hash.startswith("sha256=") or not _CANONICAL_SIZE.fullmatch(encoded_size):
            raise VerificationError(
                f"{source} row for {member_name!r} lacks a SHA-256 digest or decimal size"
            )
        actual_hash, actual_size = _zip_member_digest(archive, members.regular[member_name])
        if encoded_hash != f"sha256={actual_hash}" or int(encoded_size) != actual_size:
            raise VerificationError(
                f"{source} row for {member_name!r} does not match the archived file"
            )


def _validate_wheel_license_files(
    archive: zipfile.ZipFile,
    path: Path,
    parsed: ParsedWheelName,
    metadata: EmbeddedMetadata,
    regular: dict[str, zipfile.ZipInfo],
) -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    for license_file in metadata.license_files:
        member_name = f"{parsed.dist_info}/licenses/{license_file}"
        member = regular.get(member_name)
        if member is None:
            raise VerificationError(
                f"{path.name} is missing declared wheel License-File {license_file!r}"
            )
        payload = _read_zip_payload(
            archive,
            member,
            _MAX_LICENSE_FILE_BYTES,
            f"{path.name}:{member_name}",
        )
        source = f"{path.name}:{member_name}"
        _validate_utf8_text(payload, source)
        identities.append((license_file, hashlib.sha256(payload).hexdigest()))
    return tuple(sorted(identities))


def _read_wheel_metadata(path: Path, parsed: ParsedWheelName) -> EmbeddedMetadata:
    try:
        with zipfile.ZipFile(path) as archive:
            members = _validate_zip_members(archive, path, parsed.dist_info)
            metadata_payload = _read_zip_payload(
                archive,
                members.metadata,
                _MAX_METADATA_BYTES,
                f"{path.name}:{members.metadata.filename}",
            )
            wheel_payload = _read_zip_payload(
                archive,
                members.wheel,
                _MAX_WHEEL_HEADER_BYTES,
                f"{path.name}:{members.wheel.filename}",
            )
            metadata = _parse_core_metadata(path, members.metadata.filename, metadata_payload)
            _validate_wheel_headers(wheel_payload, path, parsed)
            license_file_digests = _validate_wheel_license_files(
                archive,
                path,
                parsed,
                metadata,
                members.regular,
            )
            metadata = replace(metadata, license_file_digests=license_file_digests)
            _validate_wheel_record(archive, path, parsed, members)
            bad_member = archive.testzip()
            if bad_member is not None:
                raise VerificationError(f"{path.name} contains a corrupt member: {bad_member}")
    except VerificationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"cannot read wheel {path.name}: {exc}") from exc
    return metadata


def _validate_tar_members(
    archive: tarfile.TarFile,
    path: Path,
    required_root: str,
) -> SdistMembers:
    limits = _archive_limits(path)
    seen: set[str] = set()
    regular: dict[str, tarfile.TarInfo] = {}
    total_bytes = 0
    for count, member in enumerate(archive, start=1):
        if count > limits.members:
            raise VerificationError(
                f"{path.name} exceeds the proportional archive member-count bound"
            )
        _validate_member_size(path, member.name, member.size, limits)
        total_bytes += member.size
        if total_bytes > limits.total_bytes:
            raise VerificationError(
                f"{path.name} exceeds the proportional total-uncompressed-size bound"
            )

        logical_name = member.name.removesuffix("/")
        if logical_name in seen:
            raise VerificationError(
                f"{path.name} contains a duplicate archive member: {logical_name}"
            )
        seen.add(logical_name)
        parts = _archive_parts(member.name, f"{path.name} archive member")
        if parts[0] != required_root:
            raise VerificationError(f"{path.name} contains a member outside root {required_root!r}")
        if not (member.isdir() or member.isfile()):
            raise VerificationError(f"{path.name}:{member.name} is not a regular file or directory")
        if member.isfile():
            regular[member.name] = member

    metadata_name = f"{required_root}/PKG-INFO"
    pyproject_name = f"{required_root}/pyproject.toml"
    if metadata_name not in regular:
        raise VerificationError(f"{path.name} must contain exactly one {metadata_name}")
    if pyproject_name not in regular:
        raise VerificationError(f"{path.name} must contain a regular root pyproject.toml")
    return SdistMembers(regular[metadata_name], regular[pyproject_name], regular)


def _read_tar_payload(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    maximum: int,
    source: str,
) -> bytes:
    stream = archive.extractfile(member)
    if stream is None:
        raise VerificationError(f"cannot read {source}")
    with stream:
        payload = stream.read(maximum + 1)
    if len(payload) > maximum or len(payload) != member.size:
        raise VerificationError(f"{source} exceeds its bounded declared size")
    return payload


def _validate_build_system(payload: bytes, source: str) -> None:
    try:
        parsed = cast(dict[str, object], tomllib.loads(payload.decode("utf-8", errors="strict")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise VerificationError(f"{source} is not valid UTF-8 TOML") from exc
    build_system_raw = parsed.get("build-system")
    if not isinstance(build_system_raw, dict) or not all(
        isinstance(key, str) for key in build_system_raw
    ):
        raise VerificationError(f"{source} lacks a valid [build-system] table")
    build_system = cast(dict[str, object], build_system_raw)
    expected_keys = {"requires", "build-backend"}
    if set(build_system) != expected_keys:
        missing = sorted(expected_keys.difference(build_system))
        unexpected = sorted(set(build_system).difference(expected_keys))
        raise VerificationError(
            f"{source} [build-system] keys are not exact; "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )

    backend = build_system["build-backend"]
    requirements = build_system["requires"]
    if backend != "hatchling.build":
        raise VerificationError(f"{source} does not declare the expected hatchling build backend")
    if not isinstance(requirements, list) or len(requirements) != 2:
        raise VerificationError(
            f"{source} must declare exactly the two direct build-system requirements"
        )

    requirement_names: set[str] = set()
    for index, requirement_raw in enumerate(requirements):
        if not isinstance(requirement_raw, str):
            raise VerificationError(f"{source} build-system requirement {index} is not text")
        requirement = _require_safe_text(
            requirement_raw,
            f"{source} build-system requirement {index}",
        )
        if _PROJECT_NAME.fullmatch(requirement) is None:
            raise VerificationError(f"{source} contains a noncanonical build-system requirement")
        requirement_names.add(canonicalize_project_name(requirement))
    if requirement_names != {"hatchling", "hatch-vcs"}:
        raise VerificationError(
            f"{source} must contain exactly one hatchling and one hatch-vcs build-system requirement"
        )


def _validate_sdist_license_files(
    archive: tarfile.TarFile,
    path: Path,
    parsed: ParsedSdistName,
    metadata: EmbeddedMetadata,
    regular: dict[str, tarfile.TarInfo],
) -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    for license_file in metadata.license_files:
        member_name = f"{parsed.root}/{license_file}"
        member = regular.get(member_name)
        if member is None:
            raise VerificationError(
                f"{path.name} is missing declared source License-File {license_file!r}"
            )
        payload = _read_tar_payload(
            archive,
            member,
            _MAX_LICENSE_FILE_BYTES,
            f"{path.name}:{member_name}",
        )
        source = f"{path.name}:{member_name}"
        _validate_utf8_text(payload, source)
        identities.append((license_file, hashlib.sha256(payload).hexdigest()))
    return tuple(sorted(identities))


def _read_sdist_metadata(path: Path, parsed: ParsedSdistName) -> EmbeddedMetadata:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = _validate_tar_members(archive, path, parsed.root)
            metadata_payload = _read_tar_payload(
                archive,
                members.metadata,
                _MAX_METADATA_BYTES,
                f"{path.name}:{members.metadata.name}",
            )
            pyproject_payload = _read_tar_payload(
                archive,
                members.pyproject,
                _MAX_PYPROJECT_BYTES,
                f"{path.name}:{members.pyproject.name}",
            )
            metadata = _parse_core_metadata(
                path,
                members.metadata.name,
                metadata_payload,
                minimum_metadata_version=_MINIMUM_SDIST_CORE_METADATA,
            )
            _validate_build_system(pyproject_payload, f"{path.name}:{members.pyproject.name}")
            license_file_digests = _validate_sdist_license_files(
                archive,
                path,
                parsed,
                metadata,
                members.regular,
            )
            metadata = replace(metadata, license_file_digests=license_file_digests)
    except VerificationError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise VerificationError(f"cannot read source distribution {path.name}: {exc}") from exc
    return metadata


def _validate_wheel_identity(parsed: ParsedWheelName, metadata: EmbeddedMetadata) -> None:
    if parsed.distribution != _wheel_distribution_name(metadata.project_name):
        raise VerificationError(
            f"wheel filename project does not match METADATA: {metadata.path.name}"
        )
    if parsed.version != metadata.version:
        raise VerificationError(
            f"wheel filename version does not match METADATA: {metadata.path.name}"
        )


def _validate_sdist_identity(parsed: ParsedSdistName, metadata: EmbeddedMetadata) -> None:
    if parsed.distribution != _wheel_distribution_name(metadata.project_name):
        raise VerificationError(
            f"source distribution filename project does not match PKG-INFO: {metadata.path.name}"
        )
    if parsed.version != metadata.version:
        raise VerificationError(
            f"source distribution filename version does not match PKG-INFO: {metadata.path.name}"
        )


def _validate_cross_format_license_files(
    wheel: EmbeddedMetadata,
    sdist: EmbeddedMetadata,
) -> None:
    if wheel.license_expression != sdist.license_expression:
        raise VerificationError(
            "wheel and source distribution License-Expression values differ: "
            f"wheel={wheel.license_expression!r}, sdist={sdist.license_expression!r}"
        )

    wheel_paths = set(wheel.license_files)
    sdist_paths = set(sdist.license_files)
    if wheel_paths != sdist_paths:
        raise VerificationError(
            "wheel and source distribution License-File path sets differ: "
            f"wheel={sorted(wheel_paths)!r}, sdist={sorted(sdist_paths)!r}"
        )

    wheel_digests = dict(wheel.license_file_digests)
    sdist_digests = dict(sdist.license_file_digests)
    differences = {
        license_file: (wheel_digests[license_file], sdist_digests[license_file])
        for license_file in sorted(wheel_paths)
        if wheel_digests[license_file] != sdist_digests[license_file]
    }
    if differences:
        raise VerificationError(
            f"wheel and source distribution License-File exact byte digests differ: {differences!r}"
        )


def _artifact_set_digest(artifacts: tuple[EmbeddedMetadata, EmbeddedMetadata]) -> str:
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda candidate: candidate.path.name):
        _require_safe_text(artifact.path.name, "artifact output filename")
        digest.update(artifact.path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifact.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def verify_release_artifacts(
    dist_dir: Path,
    project_name: str,
    *,
    tag: str | None = None,
    expected_version: str | None = None,
) -> VerificationResult:
    """Verify one unambiguous wheel/sdist pair and return its shared identity."""
    canonicalize_project_name(project_name)
    if expected_version is not None:
        _canonicalize_version(expected_version, "expected version")
    if not dist_dir.is_dir():
        raise VerificationError(f"distribution directory does not exist: {dist_dir}")

    entries = sorted(dist_dir.iterdir(), key=lambda path: path.name)
    for entry in entries:
        _require_safe_text(entry.name, "distribution filename")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        raise VerificationError("distribution directory must contain regular files only")
    wheels = [entry for entry in entries if entry.name.endswith(".whl")]
    sdists = [entry for entry in entries if entry.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise VerificationError(
            "expected exactly one wheel and one source distribution; "
            f"found {len(wheels)} wheel(s), {len(sdists)} source distribution(s), "
            f"and {len(entries)} total file(s)"
        )

    parsed_wheel = _parse_wheel_filename(wheels[0])
    parsed_sdist = _parse_sdist_filename(sdists[0])
    wheel = _read_wheel_metadata(wheels[0], parsed_wheel)
    sdist = _read_sdist_metadata(sdists[0], parsed_sdist)
    _validate_cross_format_license_files(wheel, sdist)
    _validate_wheel_identity(parsed_wheel, wheel)
    _validate_sdist_identity(parsed_sdist, sdist)

    expected_name = canonicalize_project_name(project_name)
    artifact_names = {
        canonicalize_project_name(wheel.project_name),
        canonicalize_project_name(sdist.project_name),
    }
    if artifact_names != {expected_name}:
        raise VerificationError(
            f"artifact metadata project names do not both match {project_name!r}"
        )
    if wheel.version != sdist.version:
        raise VerificationError(
            f"wheel and source distribution versions differ: {wheel.version!r} != {sdist.version!r}"
        )

    tag_version = stable_version_from_tag(tag) if tag is not None else None
    if tag_version is not None and expected_version is not None and tag_version != expected_version:
        raise VerificationError(
            f"tag version {tag_version!r} does not match expected version {expected_version!r}"
        )
    required_version = tag_version if tag_version is not None else expected_version
    if required_version is not None and wheel.version != required_version:
        raise VerificationError(
            f"artifact version {wheel.version!r} does not match required version {required_version!r}"
        )

    artifacts = (wheel, sdist)
    result = VerificationResult(
        project_name=wheel.project_name,
        version=wheel.version,
        wheel=wheel.path.name,
        sdist=sdist.path.name,
        artifact_set_sha256=_artifact_set_digest(artifacts),
    )
    for field, value in vars(result).items():
        _require_safe_text(value, f"verification output {field}")
    return result


def _write_github_outputs(path: Path, result: VerificationResult) -> None:
    values = {
        "project_name": result.project_name,
        "version": result.version,
        "wheel": result.wheel,
        "sdist": result.sdist,
        "artifact_set_sha256": result.artifact_set_sha256,
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            _require_safe_text(value, f"GitHub output {key}")
            stream.write(f"{key}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--expected-version")
    parser.add_argument("--github-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run release artifact verification."""
    args = _parser().parse_args(argv)
    try:
        result = verify_release_artifacts(
            args.dist_dir,
            args.project_name,
            tag=args.tag,
            expected_version=args.expected_version,
        )
        if args.github_output is not None:
            _write_github_outputs(args.github_output, result)
    except (OSError, VerificationError) as exc:
        sys.stderr.write(f"release artifact verification failed: {exc}\n")
        return 1

    sys.stdout.write(
        f"verified {result.project_name} {result.version}: "
        f"{result.wheel}, {result.sdist} "
        f"(artifact set sha256 {result.artifact_set_sha256})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
