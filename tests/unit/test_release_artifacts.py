from __future__ import annotations

import base64
import hashlib
import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.verify_release_artifacts import (
    VerificationError,
    _archive_parts,
    _parse_wheel_filename,
    stable_version_from_tag,
    verify_release_artifacts,
)

_PROJECT_NAME = "archicad-mcp"
_DEFAULT_WHEEL = (
    b"Wheel-Version: 1.0\n"
    b"Generator: release verification fixture\n"
    b"Root-Is-Purelib: true\n"
    b"Tag: py3-none-any\n"
)
_DEFAULT_PYPROJECT = b"""\
[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"
"""


def _default_license_files(metadata_standard: str) -> tuple[str, ...]:
    version = tuple(int(part) for part in metadata_standard.split("."))
    return ("LICENSE",) if version >= (2, 4) else ()


def _core_metadata(
    version: str,
    project_name: str = _PROJECT_NAME,
    *,
    metadata_standard: str = "2.4",
    license_files: tuple[str, ...] | None = None,
    license_expression: str | None = None,
    extra_fields: str = "",
) -> bytes:
    effective_license_files = (
        _default_license_files(metadata_standard) if license_files is None else license_files
    )
    expression = (
        f"License-Expression: {license_expression}\n" if license_expression is not None else ""
    )
    licenses = "".join(f"License-File: {name}\n" for name in effective_license_files)
    return (
        f"Metadata-Version: {metadata_standard}\n"
        f"Name: {project_name}\n"
        f"Version: {version}\n"
        f"{expression}"
        f"{licenses}"
        f"{extra_fields}"
        "Summary: release verification fixture\n\n"
    ).encode()


def _record_hash(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _member_name(member: zipfile.ZipInfo | str) -> str:
    return member.filename if isinstance(member, zipfile.ZipInfo) else member


def _record_payload(
    entries: list[tuple[zipfile.ZipInfo | str, bytes]],
    record_name: str,
    corruption: str | None,
) -> bytes:
    rows = [
        [name, _record_hash(payload), str(len(payload))]
        for member, payload in entries
        if not (name := _member_name(member)).endswith("/")
        and name not in {f"{record_name}.jws", f"{record_name}.p7s"}
    ]
    package_row = next(row for row in rows if row[0] == "archicad_mcp/__init__.py")
    if corruption == "missing-row":
        rows.remove(package_row)
    elif corruption == "extra-row":
        rows.append(["ghost.py", _record_hash(b"ghost"), "5"])
    elif corruption == "duplicate-row":
        rows.append(package_row.copy())
    elif corruption == "bad-digest":
        package_row[1] = "sha256=" + "A" * 43
    elif corruption == "bad-size":
        package_row[2] = str(int(package_row[2]) + 1)
    rows.append([record_name, "", ""])
    if corruption == "self-values":
        rows[-1] = [record_name, _record_hash(b"record"), "6"]
    return "".join(",".join(row) + "\n" for row in rows).encode()


def _write_wheel(
    path: Path,
    metadata_version: str,
    *,
    project_name: str = _PROJECT_NAME,
    dist_info: str | None = None,
    malformed: bool = False,
    extra_members: list[tuple[zipfile.ZipInfo | str, bytes]] | None = None,
    extra_fields: str = "",
    metadata_standard: str = "2.4",
    license_files: tuple[str, ...] | None = None,
    license_expression: str | None = None,
    license_payload: bytes = b"fixture license\n",
    include_licenses: bool = True,
    include_wheel: bool = True,
    include_record: bool = True,
    wheel_payload: bytes = _DEFAULT_WHEEL,
    record_corruption: str | None = None,
    record_override: bytes | None = None,
) -> None:
    if malformed:
        path.write_bytes(b"not a zip archive")
        return
    license_files = (
        _default_license_files(metadata_standard) if license_files is None else license_files
    )
    metadata_root = dist_info or f"archicad_mcp-{metadata_version}.dist-info"
    entries: list[tuple[zipfile.ZipInfo | str, bytes]] = [
        ("archicad_mcp/__init__.py", b"__version__ = 'fixture'\n"),
        (
            f"{metadata_root}/METADATA",
            _core_metadata(
                metadata_version,
                project_name,
                metadata_standard=metadata_standard,
                license_files=license_files,
                license_expression=license_expression,
                extra_fields=extra_fields,
            ),
        ),
    ]
    if include_wheel:
        entries.append((f"{metadata_root}/WHEEL", wheel_payload))
    if include_licenses:
        entries.extend(
            (f"{metadata_root}/licenses/{license_file}", license_payload)
            for license_file in license_files
            if _is_safe_fixture_path(license_file)
        )
    entries.extend(extra_members or [])

    record_name = f"{metadata_root}/RECORD"
    if include_record:
        record = record_override or _record_payload(entries, record_name, record_corruption)
        entries.append((record_name, record))

    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, payload in entries:
            archive.writestr(member, payload)


def _is_safe_fixture_path(value: str) -> bool:
    return (
        value != ""
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _tar_member(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    return member, payload


def _write_sdist(
    path: Path,
    metadata_version: str,
    *,
    project_name: str = _PROJECT_NAME,
    root: str | None = None,
    extra_members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None,
    extra_fields: str = "",
    metadata_standard: str = "2.4",
    license_files: tuple[str, ...] | None = None,
    license_expression: str | None = None,
    license_payload: bytes = b"fixture license\n",
    include_licenses: bool = True,
    include_pyproject: bool = True,
    pyproject_payload: bytes = _DEFAULT_PYPROJECT,
) -> None:
    license_files = (
        _default_license_files(metadata_standard) if license_files is None else license_files
    )
    archive_root = root or f"archicad_mcp-{metadata_version}"
    metadata = _core_metadata(
        metadata_version,
        project_name,
        metadata_standard=metadata_standard,
        license_files=license_files,
        license_expression=license_expression,
        extra_fields=extra_fields,
    )
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = [
        _tar_member(f"{archive_root}/PKG-INFO", metadata),
    ]
    if include_pyproject:
        entries.append(_tar_member(f"{archive_root}/pyproject.toml", pyproject_payload))
    if include_licenses:
        entries.extend(
            _tar_member(f"{archive_root}/{license_file}", license_payload)
            for license_file in license_files
            if _is_safe_fixture_path(license_file)
        )
    entries.extend(extra_members or [])

    with tarfile.open(path, mode="w:gz") as archive:
        for member, payload in entries:
            if payload is not None:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            else:
                archive.addfile(member)


def _write_pair(dist_dir: Path, version: str = "1.2.3") -> tuple[Path, Path]:
    dist_dir.mkdir()
    wheel = dist_dir / f"archicad_mcp-{version}-py3-none-any.whl"
    sdist = dist_dir / f"archicad_mcp-{version}.tar.gz"
    _write_wheel(wheel, version)
    _write_sdist(sdist, version)
    return wheel, sdist


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3.4",
        "v1.2.3rc1",
        "v1.2.3+local",
        "v1.2.3-dev",
        "v1.2.-3",
        "v1.2.3\nforged=value",
    ],
)
def test_malformed_or_nonstable_tags_are_rejected(tag: str) -> None:
    with pytest.raises(VerificationError):
        stable_version_from_tag(tag)


@pytest.mark.parametrize("version", ["1.2.3.dev1", "1.2.3rc1", "1.2.3+local"])
def test_production_rejects_dev_prerelease_and_local_versions(
    tmp_path: Path,
    version: str,
) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, version)

    with pytest.raises(VerificationError, match="does not match required version"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME, tag="v1.2.3")


@pytest.mark.parametrize("version", ["1.2.3", "1.2.4.dev7"])
def test_valid_stable_and_dev_pairs_have_one_safe_identity(tmp_path: Path, version: str) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, version)

    result = verify_release_artifacts(
        dist_dir,
        _PROJECT_NAME,
        expected_version=version,
        tag=f"v{version}" if version == "1.2.3" else None,
    )

    assert result.project_name == _PROJECT_NAME
    assert result.version == version
    assert result.wheel == f"archicad_mcp-{version}-py3-none-any.whl"
    assert result.sdist == f"archicad_mcp-{version}.tar.gz"
    assert len(result.artifact_set_sha256) == 64
    assert set(result.artifact_set_sha256) <= set("0123456789abcdef")


def test_tag_and_artifact_metadata_must_match(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir, "1.2.4")

    with pytest.raises(VerificationError, match="does not match required version"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME, tag="v1.2.3")


def test_wheel_and_sdist_metadata_must_match(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(dist_dir / "archicad_mcp-1.2.4.tar.gz", "1.2.4")

    with pytest.raises(VerificationError, match="versions differ"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_missing_artifact_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")

    with pytest.raises(VerificationError, match="exactly one wheel and one source distribution"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_duplicate_artifact_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _write_pair(dist_dir)
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-1-py3-none-any.whl", "1.2.3")

    with pytest.raises(VerificationError, match=r"found 2 wheel\(s\)"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_malformed_artifact_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    _, sdist = _write_pair(dist_dir)
    wheel = dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl"
    _write_wheel(wheel, "1.2.3", malformed=True)
    assert sdist.exists()

    with pytest.raises(VerificationError, match="cannot read wheel"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_newline_in_wheel_build_tag_cannot_inject_an_output() -> None:
    forged = Path("archicad_mcp-1.2.3-1\nforged=value-py3-none-any.whl")

    with pytest.raises(VerificationError, match="ASCII control"):
        _parse_wheel_filename(forged)


@pytest.mark.parametrize(
    ("project_name", "version", "extra_fields"),
    [
        ("archicad-mcp\x01", "1.2.3", ""),
        (_PROJECT_NAME, "01.2.3", ""),
        (_PROJECT_NAME, "1.2.3", "Name: second-name\n"),
        (_PROJECT_NAME, "1.2.3", "Version: 9.9.9\n"),
    ],
)
def test_malformed_or_duplicate_core_metadata_fields_are_rejected(
    tmp_path: Path,
    project_name: str,
    version: str,
    extra_fields: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        version,
        project_name=project_name,
        extra_fields=extra_fields,
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_unsupported_core_metadata_version_is_rejected(tmp_path: Path, artifact: str) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        metadata_standard="9.9" if artifact == "wheel" else "2.4",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        metadata_standard="9.9" if artifact == "sdist" else "2.4",
    )

    with pytest.raises(VerificationError, match="unsupported Core Metadata-Version"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_sdist_core_metadata_version_must_be_modern(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        metadata_standard="2.1",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        metadata_standard="2.1",
    )

    with pytest.raises(VerificationError, match=r"requires Core Metadata-Version >= 2.2"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_supported_wheel_core_metadata_2_1_remains_valid(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        metadata_standard="2.1",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        metadata_standard="2.2",
    )

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.version == "1.2.3"


def test_current_core_metadata_2_5_pair_remains_valid(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        metadata_standard="2.5",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        metadata_standard="2.5",
    )

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.version == "1.2.3"


def test_wheel_dist_info_must_match_filename_identity(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        dist_info="other_project-1.2.3.dist-info",
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"unrelated|exactly one"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_unrelated_dist_info_directory_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=[("unrelated-9.9.9.dist-info/WHEEL", b"Wheel-Version: 1.0\n")],
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="unrelated"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_sdist_root_must_match_filename_identity(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        root="wrong_root-1.2.3",
    )

    with pytest.raises(VerificationError, match=r"outside root|exactly one"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize(
    "member_name",
    ["../escape", "/absolute", "pkg/../../escape", "C:/windows-drive"],
)
def test_wheel_path_traversal_and_non_posix_paths_are_rejected(
    tmp_path: Path,
    member_name: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=[(member_name, b"bad")],
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"safe relative|path traversal"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_backslash_archive_member_is_rejected_before_extraction() -> None:
    with pytest.raises(VerificationError, match="safe relative"):
        _archive_parts("pkg\\file", "wheel archive member")


def test_sdist_path_traversal_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    traversal = tarfile.TarInfo("archicad_mcp-1.2.3/../escape")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=[(traversal, b"bad")],
    )

    with pytest.raises(VerificationError, match="path traversal"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_duplicate_wheel_metadata_member_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    duplicate = "archicad_mcp-1.2.3.dist-info/METADATA"
    with pytest.warns(UserWarning, match="Duplicate name"):
        _write_wheel(
            dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
            "1.2.3",
            extra_members=[(duplicate, _core_metadata("1.2.3"))],
        )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="duplicate archive member"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_duplicate_sdist_metadata_member_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    duplicate = tarfile.TarInfo("archicad_mcp-1.2.3/PKG-INFO")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=[(duplicate, _core_metadata("1.2.3"))],
    )

    with pytest.raises(VerificationError, match="duplicate archive member"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_zip_symlink_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    symlink = zipfile.ZipInfo("archicad_mcp/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=[(symlink, b"target")],
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="not a regular file"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_sdist_links_and_devices_are_rejected(tmp_path: Path, member_type: bytes) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    special = tarfile.TarInfo("archicad_mcp-1.2.3/special")
    special.type = member_type
    special.linkname = "archicad_mcp-1.2.3/PKG-INFO"
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=[(special, None)],
    )

    with pytest.raises(VerificationError, match="not a regular file or directory"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("missing", ["WHEEL", "RECORD"])
def test_wheel_requires_all_structural_metadata_files(tmp_path: Path, missing: str) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        include_wheel=missing != "WHEEL",
        include_record=missing != "RECORD",
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="METADATA, WHEEL, and RECORD"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_sdist_requires_root_pyproject(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        include_pyproject=False,
    )

    with pytest.raises(VerificationError, match=r"pyproject\.toml"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize(
    "wheel_payload",
    [
        b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n",
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py2-none-any\n",
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n",
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\nTag: py3-none-any\n",
    ],
)
def test_wheel_headers_and_tags_must_match_pure_filename(
    tmp_path: Path,
    wheel_payload: bytes,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        wheel_payload=wheel_payload,
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"Wheel-Version|Root-Is-Purelib|Tag"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize(
    ("wheel_name", "wheel_payload"),
    [
        (
            "archicad_mcp-1.2.3-py3-none-any.whl",
            _DEFAULT_WHEEL + b"Build: 1\n",
        ),
        ("archicad_mcp-1.2.3-1-py3-none-any.whl", _DEFAULT_WHEEL),
        (
            "archicad_mcp-1.2.3-1-py3-none-any.whl",
            _DEFAULT_WHEEL + b"Build: 2\n",
        ),
        (
            "archicad_mcp-1.2.3-1-py3-none-any.whl",
            _DEFAULT_WHEEL + b"Build: 1\nBuild: 1\n",
        ),
    ],
)
def test_wheel_build_header_must_match_filename(
    tmp_path: Path,
    wheel_name: str,
    wheel_payload: bytes,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / wheel_name, "1.2.3", wheel_payload=wheel_payload)
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="Build"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_tagged_wheel_build_header_and_record_are_valid(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel_name = "archicad_mcp-1.2.3-1-py3-none-any.whl"
    _write_wheel(
        dist_dir / wheel_name,
        "1.2.3",
        wheel_payload=_DEFAULT_WHEEL + b"Build: 1\n",
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.wheel == wheel_name


@pytest.mark.parametrize(
    "corruption",
    ["missing-row", "extra-row", "duplicate-row", "bad-digest", "bad-size", "self-values"],
)
def test_record_rows_hashes_and_sizes_are_strict(
    tmp_path: Path,
    corruption: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        record_corruption=corruption,
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"RECORD|record"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("record", [b"not,three\n", b"path,hash,size,extra\n", b"\xff,hash,size\n"])
def test_record_must_be_strict_three_column_utf8_csv(tmp_path: Path, record: bytes) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        record_override=record,
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"RECORD|CSV"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize("problem", ["missing", "traversal"])
def test_declared_license_files_must_be_safe_and_present(
    tmp_path: Path,
    artifact: str,
    problem: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    license_files = ("../LICENSE",) if problem == "traversal" else ("LICENSE",)
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_files=license_files if artifact == "wheel" else ("LICENSE",),
        include_licenses=not (artifact == "wheel" and problem == "missing"),
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_files=license_files if artifact == "sdist" else ("LICENSE",),
        include_licenses=not (artifact == "sdist" and problem == "missing"),
    )

    with pytest.raises(VerificationError, match=r"License-File|license"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_declared_license_files_must_be_strict_utf8_text(
    tmp_path: Path,
    artifact: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    invalid = b"license\xff\n"
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_payload=invalid if artifact == "wheel" else b"fixture license\n",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_payload=invalid if artifact == "sdist" else b"fixture license\n",
    )

    with pytest.raises(VerificationError, match="strict UTF-8"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_license_file_path_sets_must_match_across_formats(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_files=("LICENSE",),
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_files=("COPYING",),
    )

    with pytest.raises(VerificationError, match="path sets differ"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_license_file_path_order_is_ignored_across_formats(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_expression="MIT",
        license_files=("LICENSE", "COPYING"),
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_expression="MIT",
        license_files=("COPYING", "LICENSE"),
    )

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.version == "1.2.3"


def test_license_file_content_digests_must_match_across_formats(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_expression="MIT",
        license_payload=b"MIT License\n",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_expression="MIT",
        license_payload=b"Apache License\n",
    )

    with pytest.raises(VerificationError, match="exact byte digests"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_license_expressions_must_match_across_formats(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_expression="MIT",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_expression="Apache-2.0",
    )

    with pytest.raises(VerificationError, match="License-Expression"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_duplicate_license_expression_is_rejected(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_expression="MIT",
        extra_fields="License-Expression: Apache-2.0\n",
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_expression="MIT",
    )

    with pytest.raises(VerificationError, match="more than one License-Expression"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("metadata_standard", ["2.1", "2.2", "2.3"])
def test_wheel_pep639_fields_require_core_metadata_2_4(
    tmp_path: Path,
    metadata_standard: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        metadata_standard=metadata_standard,
        license_expression="MIT",
        license_files=("LICENSE",),
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match=r"requires Core Metadata-Version >= 2.4"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("metadata_standard", ["2.2", "2.3"])
def test_sdist_pep639_fields_require_core_metadata_2_4(
    tmp_path: Path,
    metadata_standard: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        metadata_standard=metadata_standard,
        license_expression="MIT",
        license_files=("LICENSE",),
    )

    with pytest.raises(VerificationError, match=r"requires Core Metadata-Version >= 2.4"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("license_expression", ["", " MIT", "MIT ", "MIT\x01"])
def test_license_expression_must_be_safe_text(
    tmp_path: Path,
    license_expression: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_expression=license_expression,
    )
    _write_sdist(dist_dir / "archicad_mcp-1.2.3.tar.gz", "1.2.3")

    with pytest.raises(VerificationError, match="License-Expression"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_valid_utf8_license_files_are_read_in_both_formats(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    license_payload = "Copyright © 2026\n許諾条件\n".encode()
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        license_payload=license_payload,
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        license_payload=license_payload,
    )

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.version == "1.2.3"


@pytest.mark.parametrize(
    "pyproject",
    [
        b"not = [valid",
        b"[project]\nname = 'fixture'\n",
        b"[build-system]\nrequires = ['hatchling']\nbuild-backend = 'other.build'\n",
        b"[build-system]\nrequires = ['hatchling']\nbuild-backend = 'hatchling.build'\n",
        b"[build-system]\nrequires = ['hatchling garbage', 'hatch-vcs']\n"
        b"build-backend = 'hatchling.build'\n",
        b"[build-system]\nrequires = ['hatchling', 'hatch-vcs', 'setuptools']\n"
        b"build-backend = 'hatchling.build'\n",
        b"[build-system]\nrequires = ['hatchling', 'hatch-vcs']\n"
        b"build-backend = 'hatchling.build'\nbackend-path = ['.']\n",
        b"[build-system]\nrequires = ['hatchling>=1', 'hatch-vcs']\n"
        b"build-backend = 'hatchling.build'\n",
        b"[build-system]\nrequires = ['hatchling', 'hatchling']\n"
        b"build-backend = 'hatchling.build'\n",
        b"[build-system]\nbuild-backend = 'hatchling.build'\n",
    ],
)
def test_sdist_pyproject_must_prove_expected_pep517_rebuildability(
    tmp_path: Path,
    pyproject: bytes,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        pyproject_payload=pyproject,
    )

    with pytest.raises(VerificationError, match=r"TOML|build-system|backend|requirements"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


def test_sdist_build_system_allows_required_requirements_in_either_order(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_wheel(dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl", "1.2.3")
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        pyproject_payload=(
            b"[build-system]\n"
            b"requires = ['hatch-vcs', 'hatchling']\n"
            b"build-backend = 'hatchling.build'\n"
        ),
    )

    result = verify_release_artifacts(dist_dir, _PROJECT_NAME)

    assert result.version == "1.2.3"


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_archive_member_count_bound_is_enforced(tmp_path: Path, artifact: str) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    wheel_extras: list[tuple[zipfile.ZipInfo | str, bytes]] = [
        (f"archicad_mcp/empty_{index}.txt", b"") for index in range(240)
    ]
    sdist_extras: list[tuple[tarfile.TarInfo, bytes | None]] = [
        _tar_member(f"archicad_mcp-1.2.3/empty_{index}.txt", b"") for index in range(120)
    ]
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=wheel_extras if artifact == "wheel" else None,
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=sdist_extras if artifact == "sdist" else None,
    )

    with pytest.raises(VerificationError, match="member-count bound"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_archive_per_member_size_bound_is_enforced(tmp_path: Path, artifact: str) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    oversized = b"x" * (2 * 1024 * 1024 + 1)
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=[("archicad_mcp/oversized.bin", oversized)] if artifact == "wheel" else None,
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=[_tar_member("archicad_mcp-1.2.3/oversized.bin", oversized)]
        if artifact == "sdist"
        else None,
    )

    with pytest.raises(VerificationError, match="member-size bound"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_archive_total_uncompressed_size_bound_is_enforced(
    tmp_path: Path,
    artifact: str,
) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    payload = b"x" * (2 * 1024 * 1024)
    wheel_extras: list[tuple[zipfile.ZipInfo | str, bytes]] = [
        (f"archicad_mcp/large_{index}.bin", payload) for index in range(5)
    ]
    sdist_extras: list[tuple[tarfile.TarInfo, bytes | None]] = [
        _tar_member(f"archicad_mcp-1.2.3/large_{index}.bin", payload) for index in range(5)
    ]
    _write_wheel(
        dist_dir / "archicad_mcp-1.2.3-py3-none-any.whl",
        "1.2.3",
        extra_members=wheel_extras if artifact == "wheel" else None,
    )
    _write_sdist(
        dist_dir / "archicad_mcp-1.2.3.tar.gz",
        "1.2.3",
        extra_members=sdist_extras if artifact == "sdist" else None,
    )

    with pytest.raises(VerificationError, match="total-uncompressed-size bound"):
        verify_release_artifacts(dist_dir, _PROJECT_NAME)
