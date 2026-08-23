#!/usr/bin/env python3
"""Perform retry-safe release transactions against GitHub and Python package indexes."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

_GITHUB_API_VERSION = "2026-03-10"
_GITHUB_ACCEPT = "application/vnd.github+json"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_OUTPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASCII_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_ERROR_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
_MAX_RELEASE_MARKER_BYTES = 64 * 1024
_RELEASE_MARKER_PREFIX = "<!-- archicad-mcp-release-transaction:"
_RELEASE_MARKER = re.compile(r"<!-- archicad-mcp-release-transaction:v1:([0-9a-f]+) -->")


class ReleaseOperationError(RuntimeError):
    """Report a release state that cannot be continued safely."""


class AmbiguousMutationError(ReleaseOperationError):
    """Report a mutation whose server-side outcome must be observed on a rerun."""


class HttpStatusError(ReleaseOperationError):
    """Report an unexpected HTTP status without exposing authorization material."""

    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        self.method = method
        self.url = url
        self.status = status
        detail = body[:512].decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
        super().__init__(f"{method} {url} returned HTTP {status}: {detail}")


@dataclass(frozen=True)
class HttpResponse:
    """Bounded HTTP response data."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class LocalAsset:
    """One exact local distribution file."""

    name: str
    path: Path
    sha256: str
    size: int
    package_type: str


@dataclass(frozen=True)
class RemoteSource:
    """Current remote release source identity."""

    tag_object_sha: str
    source_sha: str
    master_sha: str
    tag_object_type: str


@dataclass(frozen=True)
class ReleaseRecord:
    """Fields required to resume one GitHub release."""

    release_id: int
    tag_name: str
    target_commitish: str
    name: str
    body: str
    draft: bool
    prerelease: bool


@dataclass(frozen=True)
class ReleaseAssetRecord:
    """Fields required to reconcile one GitHub release asset."""

    asset_id: int
    name: str
    digest: str | None
    state: str
    size: int


@dataclass(frozen=True)
class ReleaseAssetInventory:
    """Exact uploaded assets and safely recoverable failed-upload remnants."""

    uploaded: frozenset[str]
    starters: tuple[ReleaseAssetRecord, ...]
    names: frozenset[str]


@dataclass(frozen=True)
class ReleaseContent:
    """Persisted generated release content recovered from its transaction marker."""

    name: str
    body: str
    generated_body: str


class ApiClient:
    """Small bounded HTTP client with safe read retries and mutation ambiguity reporting."""

    def __init__(
        self,
        *,
        token: str | None = None,
        github_api: bool = True,
        timeout: float = 20.0,
        read_attempts: int = 4,
        retry_delay: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0 or read_attempts < 1 or retry_delay < 0:
            raise ValueError("invalid HTTP retry configuration")
        self._token = token
        self._github_api = github_api
        self._timeout = timeout
        self._read_attempts = read_attempts
        self._retry_delay = retry_delay
        self._sleeper = sleeper

    def request(
        self,
        method: str,
        url: str,
        *,
        expected: Iterable[int],
        json_body: Mapping[str, object] | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        retry_safe: bool,
        max_bytes: int = _MAX_JSON_BYTES,
    ) -> HttpResponse:
        """Issue one request, retrying only operations explicitly declared safe."""
        if json_body is not None and raw_body is not None:
            raise ValueError("an HTTP request cannot have both JSON and raw bodies")
        expected_statuses = frozenset(expected)
        if not expected_statuses:
            raise ValueError("at least one expected HTTP status is required")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")

        payload = raw_body
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
            content_type = "application/json"

        attempts = self._read_attempts if retry_safe else 1
        last_error: BaseException | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url=url, data=payload, method=method)
            request.add_header(
                "Accept",
                _GITHUB_ACCEPT if self._github_api else "application/json",
            )
            request.add_header("User-Agent", "archicad-mcp-release-operations")
            if self._github_api:
                request.add_header("X-GitHub-Api-Version", _GITHUB_API_VERSION)
            if content_type is not None:
                request.add_header("Content-Type", content_type)
            if self._token is not None:
                request.add_unredirected_header("Authorization", f"Bearer {self._token}")

            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as opened:
                    status = opened.status
                    body = opened.read(max_bytes + 1)
                    headers = {key.lower(): value for key, value in opened.headers.items()}
                if len(body) > max_bytes:
                    raise ReleaseOperationError(f"{method} {url} returned an oversized response")
                if status not in expected_statuses:
                    raise HttpStatusError(method, url, status, body)
                return HttpResponse(status, headers, body)
            except urllib.error.HTTPError as exc:
                body = exc.read(_MAX_ERROR_BYTES + 1)
                if exc.code in expected_statuses:
                    return HttpResponse(
                        exc.code,
                        {key.lower(): value for key, value in exc.headers.items()},
                        body[:_MAX_ERROR_BYTES],
                    )
                status_error = HttpStatusError(method, url, exc.code, body[:_MAX_ERROR_BYTES])
                if retry_safe and exc.code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    last_error = status_error
                    self._sleeper(self._retry_delay)
                    continue
                if not retry_safe and exc.code in _RETRYABLE_STATUS:
                    raise AmbiguousMutationError(
                        f"{method} {url} returned HTTP {exc.code}; observe server state on rerun"
                    ) from status_error
                raise status_error from exc
            except (
                ConnectionError,
                TimeoutError,
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
            ) as exc:
                if retry_safe and attempt + 1 < attempts:
                    last_error = exc
                    self._sleeper(self._retry_delay)
                    continue
                if not retry_safe:
                    raise AmbiguousMutationError(
                        f"{method} {url} lost its response; observe server state on rerun"
                    ) from exc
                last_error = exc
                break

        raise ReleaseOperationError(f"{method} {url} failed after bounded retries") from last_error

    def json(
        self,
        method: str,
        url: str,
        *,
        expected: Iterable[int],
        json_body: Mapping[str, object] | None = None,
        retry_safe: bool,
    ) -> tuple[int, object]:
        """Return a strictly decoded bounded JSON response."""
        response = self.request(
            method,
            url,
            expected=expected,
            json_body=json_body,
            retry_safe=retry_safe,
        )
        if not response.body:
            return response.status, None
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseOperationError(f"{method} {url} returned invalid JSON") from exc
        return response.status, cast(object, payload)


def _fail(message: str) -> NoReturn:
    raise ReleaseOperationError(message)


def _safe_text(value: str, source: str, *, allow_empty: bool = False) -> str:
    if (not value and not allow_empty) or _ASCII_CONTROL.search(value) is not None:
        _fail(f"{source} contains an empty value or ASCII control character")
    return value


def _safe_multiline_text(value: str, source: str) -> str:
    if "\r" in value or any(
        ord(character) < 32 and character not in {"\n", "\t"} for character in value
    ):
        _fail(f"{source} contains unsupported control characters")
    return value


def _repository_parts(repository: str) -> tuple[str, str]:
    _safe_text(repository, "repository")
    parts = repository.split("/")
    if len(parts) != 2 or any(_REPOSITORY_COMPONENT.fullmatch(part) is None for part in parts):
        _fail(f"invalid GitHub repository identity: {repository!r}")
    return parts[0], parts[1]


def _require_sha(value: str, source: str, pattern: re.Pattern[str] = _COMMIT_SHA) -> str:
    _safe_text(value, source)
    if pattern.fullmatch(value) is None:
        _fail(f"{source} is not a lowercase hexadecimal digest of the required length")
    return value


def _github_api_url(api_base: str, repository: str, suffix: str) -> str:
    owner, name = _repository_parts(repository)
    return (
        f"{api_base.rstrip('/')}/repos/{urllib.parse.quote(owner, safe='')}"
        f"/{urllib.parse.quote(name, safe='')}/{suffix.lstrip('/')}"
    )


def _json_object(payload: object, source: str) -> dict[str, object]:
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        _fail(f"{source} is not a JSON object")
    return cast(dict[str, object], payload)


def _json_list(payload: object, source: str) -> list[object]:
    if not isinstance(payload, list):
        _fail(f"{source} is not a JSON array")
    return cast(list[object], payload)


def _field(record: Mapping[str, object], name: str, expected_type: type[Any], source: str) -> Any:
    value = record.get(name)
    if expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(f"{source}.{name} has an invalid type")
    elif not isinstance(value, expected_type):
        _fail(f"{source}.{name} has an invalid type")
    return value


def _write_outputs(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if _OUTPUT_KEY.fullmatch(key) is None:
                _fail(f"invalid GitHub output key: {key!r}")
            _safe_text(value, f"GitHub output {key}", allow_empty=True)
            stream.write(f"{key}={value}\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_assets(
    dist_dir: Path, wheel_name: str, sdist_name: str
) -> tuple[LocalAsset, LocalAsset]:
    for name, label in ((wheel_name, "wheel filename"), (sdist_name, "sdist filename")):
        _safe_text(name, label)
        if Path(name).name != name:
            _fail(f"{label} must be a basename")
    if not wheel_name.endswith(".whl") or not sdist_name.endswith(".tar.gz"):
        _fail("expected one .whl filename and one .tar.gz filename")
    if not dist_dir.is_dir():
        _fail(f"distribution directory does not exist: {dist_dir}")
    entries = sorted(dist_dir.iterdir(), key=lambda candidate: candidate.name)
    if {entry.name for entry in entries} != {wheel_name, sdist_name}:
        _fail("distribution directory does not contain exactly the expected wheel and sdist")
    if any(entry.is_symlink() or not entry.is_file() for entry in entries):
        _fail("distribution directory must contain regular files only")

    wheel = dist_dir / wheel_name
    sdist = dist_dir / sdist_name
    return (
        LocalAsset(wheel.name, wheel, _sha256_file(wheel), wheel.stat().st_size, "bdist_wheel"),
        LocalAsset(sdist.name, sdist, _sha256_file(sdist), sdist.stat().st_size, "sdist"),
    )


def _parse_rfc3339(value: str, source: str) -> datetime:
    _safe_text(value, source)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseOperationError(f"{source} is not an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        _fail(f"{source} lacks a timezone")
    return parsed.astimezone(UTC)


def _flat_zip_entries(payload: bytes) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                name = member.filename.removesuffix("/")
                _safe_text(name, "artifact archive member")
                parts = PurePosixPath(name).parts
                if (
                    member.is_dir()
                    or len(parts) != 1
                    or parts[0] in {"", ".", ".."}
                    or "\\" in member.filename
                    or member.filename.startswith("/")
                ):
                    _fail("run artifact archive must contain flat regular files only")
                mode_type = stat.S_IFMT(member.external_attr >> 16)
                if mode_type not in {0, stat.S_IFREG}:
                    _fail(f"run artifact member is not regular: {name}")
                if name in entries:
                    _fail(f"run artifact contains a duplicate member: {name}")
                with archive.open(member) as stream:
                    entries[name] = stream.read()
            bad_member = archive.testzip()
            if bad_member is not None:
                _fail(f"run artifact archive contains corrupt member: {bad_member}")
    except ReleaseOperationError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ReleaseOperationError("cannot read downloaded run artifact archive") from exc
    if not entries:
        _fail("downloaded run artifact is empty")
    return entries


def reuse_run_artifact(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    run_id: int,
    artifact_name: str,
    expected_workflow_head_sha: str,
    dist_dir: Path,
    github_output: Path,
    now: datetime | None = None,
) -> bool:
    """Download the sole valid run artifact when one already exists."""
    if run_id < 1:
        _fail("workflow run ID must be positive")
    _safe_text(artifact_name, "run artifact name")
    _require_sha(expected_workflow_head_sha, "expected workflow head SHA")
    query = urllib.parse.urlencode({"name": artifact_name, "per_page": "100"})
    url = _github_api_url(
        api_base,
        repository,
        f"actions/runs/{run_id}/artifacts?{query}",
    )
    _, payload = client.json("GET", url, expected={200}, retry_safe=True)
    root = _json_object(payload, "workflow-run artifact response")
    artifacts = _json_list(root.get("artifacts"), "workflow-run artifact response.artifacts")
    total_count = _field(root, "total_count", int, "workflow-run artifact response")
    if total_count != len(artifacts):
        _fail("workflow-run artifact response was truncated or internally inconsistent")
    if not artifacts:
        _write_outputs(
            github_output,
            {"found": "false", "artifact_id": "", "artifact_digest": ""},
        )
        return False
    if len(artifacts) != 1:
        _fail(f"expected at most one run artifact named {artifact_name!r}; found {len(artifacts)}")

    artifact = _json_object(artifacts[0], "workflow-run artifact")
    if _field(artifact, "name", str, "workflow-run artifact") != artifact_name:
        _fail("workflow-run artifact API returned a mismatched artifact name")
    artifact_id = _field(artifact, "id", int, "workflow-run artifact")
    if artifact_id < 1:
        _fail("workflow-run artifact ID must be positive")
    if _field(artifact, "expired", bool, "workflow-run artifact"):
        _fail("the retained workflow-run artifact is expired")
    expires_at = _parse_rfc3339(
        _field(artifact, "expires_at", str, "workflow-run artifact"),
        "workflow-run artifact expiry",
    )
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    if expires_at <= current_time:
        _fail("the retained workflow-run artifact has reached its expiry time")
    digest = _field(artifact, "digest", str, "workflow-run artifact")
    if _SHA256_DIGEST.fullmatch(digest) is None:
        _fail("workflow-run artifact is missing a valid SHA-256 digest")
    workflow_run = _json_object(artifact.get("workflow_run"), "workflow-run artifact.workflow_run")
    if _field(workflow_run, "id", int, "workflow-run artifact.workflow_run") != run_id:
        _fail("workflow-run artifact belongs to a different run ID")
    if (
        _field(workflow_run, "head_sha", str, "workflow-run artifact.workflow_run")
        != expected_workflow_head_sha
    ):
        _fail("workflow-run artifact belongs to a different workflow head SHA")
    if dist_dir.exists():
        _fail("distribution destination already exists before artifact reuse")

    download_url = _github_api_url(
        api_base,
        repository,
        f"actions/artifacts/{artifact_id}/zip",
    )
    response = client.request(
        "GET",
        download_url,
        expected={200},
        retry_safe=True,
        max_bytes=_MAX_ARTIFACT_BYTES,
    )
    archive_digest = hashlib.sha256(response.body).hexdigest()
    if digest != f"sha256:{archive_digest}":
        _fail("downloaded run artifact archive does not match its GitHub SHA-256 digest")
    entries = _flat_zip_entries(response.body)
    dist_dir.mkdir(parents=False)
    for name, contents in entries.items():
        (dist_dir / name).write_bytes(contents)

    _write_outputs(
        github_output,
        {
            "found": "true",
            "artifact_id": str(artifact_id),
            "artifact_digest": digest.removeprefix("sha256:"),
        },
    )
    return True


def _run_git(
    cwd: Path, arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip().replace("\r", " ").replace("\n", " ")
        _fail(f"git {' '.join(arguments[:2])} failed: {detail}")
    return result


def verify_remote_source(
    repository_dir: Path,
    *,
    tag: str,
    source_sha: str,
    expected_tag_object_sha: str | None = None,
    remote: str = "origin",
    base_branch: str = "master",
) -> RemoteSource:
    """Fetch and verify the current production tag and source reachability from remote master."""
    if _STABLE_TAG.fullmatch(_safe_text(tag, "release tag")) is None:
        _fail("production release tag is not exact stable SemVer")
    _require_sha(source_sha, "release source SHA")
    if expected_tag_object_sha is not None:
        _require_sha(expected_tag_object_sha, "expected tag object SHA")
    if _REPOSITORY_COMPONENT.fullmatch(_safe_text(remote, "git remote")) is None:
        _fail("invalid git remote name")
    if _REPOSITORY_COMPONENT.fullmatch(_safe_text(base_branch, "base branch")) is None:
        _fail("invalid base branch name")
    if not repository_dir.is_dir():
        _fail(f"repository directory does not exist: {repository_dir}")

    head_sha = _run_git(repository_dir, ["rev-parse", "HEAD"]).stdout.strip()
    if head_sha != source_sha:
        _fail("checked-out HEAD does not match the expected release source SHA")

    namespace = secrets.token_hex(12)
    tag_ref = f"refs/release-validation/{namespace}/tag"
    master_ref = f"refs/release-validation/{namespace}/master"
    _run_git(
        repository_dir,
        [
            "fetch",
            "--atomic",
            "--no-tags",
            remote,
            f"+refs/tags/{tag}:{tag_ref}",
            f"+refs/heads/{base_branch}:{master_ref}",
        ],
    )
    tag_object_sha = _run_git(repository_dir, ["rev-parse", "--verify", tag_ref]).stdout.strip()
    tag_commit_sha = _run_git(
        repository_dir,
        ["rev-parse", "--verify", f"{tag_ref}^{{commit}}"],
    ).stdout.strip()
    master_sha = _run_git(repository_dir, ["rev-parse", "--verify", master_ref]).stdout.strip()
    object_type = _run_git(repository_dir, ["cat-file", "-t", tag_object_sha]).stdout.strip()
    for value, label in (
        (tag_object_sha, "remote tag object SHA"),
        (tag_commit_sha, "remote peeled tag commit SHA"),
        (master_sha, "remote master SHA"),
    ):
        _require_sha(value, label)
    if object_type not in {"commit", "tag"}:
        _fail(f"remote release tag points to an unsupported object type: {object_type!r}")
    if tag_commit_sha != source_sha:
        _fail("current remote release tag peels to a different source commit")
    if expected_tag_object_sha is not None and tag_object_sha != expected_tag_object_sha:
        _fail("current remote release tag object changed after the build gate")
    ancestor = _run_git(
        repository_dir,
        ["merge-base", "--is-ancestor", source_sha, master_ref],
        check=False,
    )
    if ancestor.returncode == 1:
        _fail("release source is not reachable from current origin/master")
    if ancestor.returncode != 0:
        _fail("git could not determine release source reachability from current origin/master")
    return RemoteSource(tag_object_sha, source_sha, master_sha, object_type)


def _query_index_files(
    client: ApiClient,
    *,
    api_base: str,
    project_name: str,
    version: str,
    expected: Mapping[str, LocalAsset],
) -> frozenset[str]:
    _safe_text(project_name, "index project name")
    _safe_text(version, "index version")
    url = (
        f"{api_base.rstrip('/')}/pypi/{urllib.parse.quote(project_name, safe='')}"
        f"/{urllib.parse.quote(version, safe='')}/json"
    )
    status, payload = client.json("GET", url, expected={200, 404}, retry_safe=True)
    if status == 404:
        return frozenset()
    root = _json_object(payload, "package index response")
    files = _json_list(root.get("urls"), "package index response.urls")
    observed: set[str] = set()
    for index, raw_file in enumerate(files):
        record = _json_object(raw_file, f"package index response.urls[{index}]")
        filename = _field(record, "filename", str, f"package index response.urls[{index}]")
        _safe_text(filename, "package index filename")
        if filename in observed:
            _fail(f"package index returned duplicate filename {filename!r}")
        observed.add(filename)
        local = expected.get(filename)
        if local is None:
            _fail(f"package index release contains unexpected file {filename!r}")
        package_type = _field(
            record,
            "packagetype",
            str,
            f"package index response.urls[{index}]",
        )
        if package_type != local.package_type:
            _fail(f"package index file {filename!r} has an unexpected package type")
        digests = _json_object(
            record.get("digests"),
            f"package index response.urls[{index}].digests",
        )
        digest = _field(
            digests,
            "sha256",
            str,
            f"package index response.urls[{index}].digests",
        )
        if _SHA256.fullmatch(digest) is None or digest != local.sha256:
            _fail(f"package index file {filename!r} does not match the retained SHA-256")
    return frozenset(observed)


def reconcile_package_index(
    client: ApiClient,
    *,
    api_base: str,
    project_name: str,
    version: str,
    dist_dir: Path,
    wheel_name: str,
    sdist_name: str,
    publish_dir: Path | None,
    require_complete: bool,
    attempts: int = 1,
    retry_delay: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[LocalAsset, ...]:
    """Validate index state and optionally stage only files that are not already present."""
    if attempts < 1 or retry_delay < 0:
        raise ValueError("invalid package-index retry configuration")
    assets = _local_assets(dist_dir, wheel_name, sdist_name)
    expected = {asset.name: asset for asset in assets}
    missing_names: set[str] = set(expected)
    for attempt in range(attempts):
        observed = _query_index_files(
            client,
            api_base=api_base,
            project_name=project_name,
            version=version,
            expected=expected,
        )
        missing_names = set(expected).difference(observed)
        if not missing_names:
            return ()
        if not require_complete:
            break
        if attempt + 1 < attempts:
            sleeper(retry_delay)
    if require_complete:
        _fail(
            "package index did not converge to the exact wheel and sdist after bounded retries; "
            f"missing {sorted(missing_names)!r}"
        )
    if publish_dir is None:
        _fail("a clean publish directory is required when staging missing package files")
    if publish_dir.exists():
        _fail("publish directory already exists and cannot be proven clean")
    publish_dir.mkdir(parents=False)
    missing = tuple(asset for asset in assets if asset.name in missing_names)
    for asset in missing:
        destination = publish_dir / asset.name
        shutil.copyfile(asset.path, destination)
        if _sha256_file(destination) != asset.sha256:
            _fail(f"staged package file changed while copying: {asset.name}")
    return missing


def _release_from_json(payload: object, source: str) -> ReleaseRecord:
    record = _json_object(payload, source)
    release_id = _field(record, "id", int, source)
    if release_id < 1:
        _fail(f"{source}.id must be positive")
    values = {
        "tag_name": _field(record, "tag_name", str, source),
        "target_commitish": _field(record, "target_commitish", str, source),
        "name": _field(record, "name", str, source),
        "body": _field(record, "body", str, source),
    }
    for key, value in values.items():
        if key == "body":
            _safe_multiline_text(value, f"{source}.{key}")
        else:
            _safe_text(value, f"{source}.{key}")
    return ReleaseRecord(
        release_id=release_id,
        tag_name=values["tag_name"],
        target_commitish=values["target_commitish"],
        name=values["name"],
        body=values["body"],
        draft=_field(record, "draft", bool, source),
        prerelease=_field(record, "prerelease", bool, source),
    )


def _matching_release(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    tag: str,
) -> ReleaseRecord | None:
    matches: list[ReleaseRecord] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"per_page": "100", "page": str(page)})
        url = _github_api_url(api_base, repository, f"releases?{query}")
        _, payload = client.json("GET", url, expected={200}, retry_safe=True)
        raw_releases = _json_list(payload, "GitHub releases response")
        releases = [
            _release_from_json(raw_release, f"GitHub releases response[{index}]")
            for index, raw_release in enumerate(raw_releases)
        ]
        matches.extend(release for release in releases if release.tag_name == tag)
        if len(raw_releases) < 100:
            break
    else:
        _fail("GitHub release listing exceeded the bounded pagination limit")
    if len(matches) > 1:
        _fail(f"GitHub returned multiple releases for tag {tag!r}")
    return matches[0] if matches else None


def _asset_from_json(payload: object, source: str) -> ReleaseAssetRecord:
    record = _json_object(payload, source)
    asset_id = _field(record, "id", int, source)
    size = _field(record, "size", int, source)
    if asset_id < 1 or size < 0:
        _fail(f"{source} has an invalid ID or size")
    name = _field(record, "name", str, source)
    state = _field(record, "state", str, source)
    if "digest" not in record:
        _fail(f"{source}.digest is missing")
    digest_raw = record.get("digest")
    if digest_raw is not None and not isinstance(digest_raw, str):
        _fail(f"{source}.digest has an invalid type")
    digest = digest_raw
    _safe_text(name, f"{source}.name")
    _safe_text(state, f"{source}.state")
    if digest is not None:
        _safe_text(digest, f"{source}.digest")
    return ReleaseAssetRecord(asset_id, name, digest, state, size)


def _release_assets(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    release_id: int,
) -> list[ReleaseAssetRecord]:
    assets: list[ReleaseAssetRecord] = []
    for page in range(1, 11):
        query = urllib.parse.urlencode({"per_page": "100", "page": str(page)})
        url = _github_api_url(
            api_base,
            repository,
            f"releases/{release_id}/assets?{query}",
        )
        _, payload = client.json("GET", url, expected={200}, retry_safe=True)
        raw_assets = _json_list(payload, "GitHub release assets response")
        assets.extend(
            _asset_from_json(raw_asset, f"GitHub release assets response[{index}]")
            for index, raw_asset in enumerate(raw_assets)
        )
        if len(raw_assets) < 100:
            break
    else:
        _fail("GitHub release asset listing exceeded the bounded pagination limit")
    return assets


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _marker_asset_records(local_assets: Iterable[LocalAsset]) -> list[dict[str, object]]:
    return [
        {"name": asset.name, "sha256": asset.sha256, "size": asset.size}
        for asset in sorted(local_assets, key=lambda candidate: candidate.name)
    ]


def _build_release_content(
    *,
    tag: str,
    source_sha: str,
    curated_segment: str,
    curated_sha256: str,
    generated_name: str,
    generated_body: str,
    local_assets: tuple[LocalAsset, LocalAsset],
) -> ReleaseContent:
    generated_segment = generated_body.strip("\n")
    if _RELEASE_MARKER_PREFIX in curated_segment or _RELEASE_MARKER_PREFIX in generated_segment:
        _fail("release notes contain the reserved transaction marker prefix")
    marker_record: dict[str, object] = {
        "assets": _marker_asset_records(local_assets),
        "curated_sha256": curated_sha256,
        "generated_body_sha256": _sha256_text(generated_segment),
        "generated_title_sha256": _sha256_text(generated_name),
        "schema": 1,
        "source_sha": source_sha,
        "tag": tag,
    }
    marker_bytes = json.dumps(
        marker_record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(marker_bytes) > _MAX_RELEASE_MARKER_BYTES:
        _fail("release transaction marker is unexpectedly large")
    marker = f"{_RELEASE_MARKER_PREFIX}v1:{marker_bytes.hex()} -->"
    public_parts = [part for part in (curated_segment, generated_segment) if part]
    public_body = "\n\n".join(public_parts)
    exact_body = f"{public_body}\n\n{marker}" if public_body else marker
    return ReleaseContent(generated_name, exact_body, generated_segment)


def _parse_release_content(
    release: ReleaseRecord,
    *,
    tag: str,
    source_sha: str,
    curated_segment: str,
    curated_sha256: str,
    local_assets: tuple[LocalAsset, LocalAsset],
) -> ReleaseContent:
    if release.body.count(_RELEASE_MARKER_PREFIX) != 1:
        _fail("GitHub release body must contain exactly one transaction marker")
    marker_match = _RELEASE_MARKER.search(release.body)
    if marker_match is None or marker_match.end() != len(release.body):
        _fail("GitHub release transaction marker is malformed or is not the final body segment")
    if marker_match.start() == 0:
        public_body = ""
    elif release.body[marker_match.start() - 2 : marker_match.start()] == "\n\n":
        public_body = release.body[: marker_match.start() - 2]
    else:
        _fail("GitHub release transaction marker lacks its exact body separator")

    encoded_marker = marker_match.group(1)
    if len(encoded_marker) % 2 or len(encoded_marker) > _MAX_RELEASE_MARKER_BYTES * 2:
        _fail("GitHub release transaction marker has an invalid encoded length")
    try:
        marker_bytes = bytes.fromhex(encoded_marker)
        marker_payload = json.loads(marker_bytes.decode("utf-8", errors="strict"))
        canonical = json.dumps(
            marker_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (UnicodeDecodeError, ValueError, RecursionError, OverflowError) as exc:
        raise ReleaseOperationError(
            "GitHub release transaction marker is not canonical JSON"
        ) from exc
    if canonical != marker_bytes:
        _fail("GitHub release transaction marker is not in canonical form")
    marker = _json_object(marker_payload, "GitHub release transaction marker")
    required_fields = {
        "assets",
        "curated_sha256",
        "generated_body_sha256",
        "generated_title_sha256",
        "schema",
        "source_sha",
        "tag",
    }
    if set(marker) != required_fields:
        _fail("GitHub release transaction marker fields are incomplete or unexpected")
    if _field(marker, "schema", int, "GitHub release transaction marker") != 1:
        _fail("GitHub release transaction marker schema is unsupported")
    marker_tag = _field(marker, "tag", str, "GitHub release transaction marker")
    marker_source = _field(marker, "source_sha", str, "GitHub release transaction marker")
    marker_curated = _field(marker, "curated_sha256", str, "GitHub release transaction marker")
    title_hash = _field(
        marker,
        "generated_title_sha256",
        str,
        "GitHub release transaction marker",
    )
    body_hash = _field(
        marker,
        "generated_body_sha256",
        str,
        "GitHub release transaction marker",
    )
    for value, label in (
        (marker_curated, "curated-note SHA-256"),
        (title_hash, "generated-title SHA-256"),
        (body_hash, "generated-body SHA-256"),
    ):
        if _SHA256.fullmatch(value) is None:
            _fail(f"GitHub release transaction marker has an invalid {label}")
    if marker_tag != tag or marker_source != source_sha or marker_curated != curated_sha256:
        _fail("GitHub release transaction marker does not match tag, source, or curated notes")

    raw_assets = _json_list(marker.get("assets"), "GitHub release transaction marker.assets")
    marker_assets: list[dict[str, object]] = []
    for index, raw_asset in enumerate(raw_assets):
        marker_asset = _json_object(
            raw_asset,
            f"GitHub release transaction marker.assets[{index}]",
        )
        if set(marker_asset) != {"name", "sha256", "size"}:
            _fail("GitHub release transaction marker has malformed asset fields")
        name = _field(
            marker_asset,
            "name",
            str,
            f"GitHub release transaction marker.assets[{index}]",
        )
        sha256 = _field(
            marker_asset,
            "sha256",
            str,
            f"GitHub release transaction marker.assets[{index}]",
        )
        size = _field(
            marker_asset,
            "size",
            int,
            f"GitHub release transaction marker.assets[{index}]",
        )
        if _SHA256.fullmatch(sha256) is None or size < 0:
            _fail("GitHub release transaction marker has an invalid asset digest or size")
        marker_assets.append({"name": name, "sha256": sha256, "size": size})
    if marker_assets != _marker_asset_records(local_assets):
        _fail("GitHub release transaction marker does not match the retained release assets")
    if title_hash != _sha256_text(release.name):
        _fail("GitHub release title does not match its transaction marker")

    if curated_segment:
        generated_prefix = f"{curated_segment}\n\n"
        if public_body == curated_segment:
            generated_segment = ""
        elif public_body.startswith(generated_prefix):
            generated_segment = public_body.removeprefix(generated_prefix)
        else:
            _fail("GitHub release curated body segment does not match the retained notes")
    else:
        generated_segment = public_body
    if generated_segment != generated_segment.strip("\n"):
        _fail("GitHub release generated body segment is not canonically delimited")
    if body_hash != _sha256_text(generated_segment):
        _fail("GitHub release generated body does not match its transaction marker")
    return ReleaseContent(release.name, release.body, generated_segment)


def _validate_release_identity(
    release: ReleaseRecord,
    *,
    tag: str,
    source_sha: str,
    curated_segment: str,
    curated_sha256: str,
    local_assets: tuple[LocalAsset, LocalAsset],
) -> ReleaseContent:
    if release.tag_name != tag:
        _fail("GitHub release tag does not match the requested tag")
    if release.target_commitish != source_sha:
        _fail("GitHub release target does not match the exact source SHA")
    if release.prerelease:
        _fail("stable GitHub release is unexpectedly marked as a prerelease")
    return _parse_release_content(
        release,
        tag=tag,
        source_sha=source_sha,
        curated_segment=curated_segment,
        curated_sha256=curated_sha256,
        local_assets=local_assets,
    )


def _validate_release_assets(
    observed: list[ReleaseAssetRecord],
    expected: Mapping[str, LocalAsset],
    *,
    require_complete: bool,
    allow_starter: bool,
) -> ReleaseAssetInventory:
    names: set[str] = set()
    asset_ids: set[int] = set()
    uploaded: set[str] = set()
    starters: list[ReleaseAssetRecord] = []
    for asset in observed:
        if asset.name in names:
            _fail(f"GitHub release contains duplicate asset name {asset.name!r}")
        if asset.asset_id in asset_ids:
            _fail(f"GitHub release contains duplicate asset ID {asset.asset_id}")
        names.add(asset.name)
        asset_ids.add(asset.asset_id)
        local = expected.get(asset.name)
        if local is None:
            _fail(f"GitHub release contains unexpected asset {asset.name!r}")
        if asset.state == "uploaded":
            if _SHA256_DIGEST.fullmatch(asset.digest or "") is None:
                _fail(f"GitHub release asset {asset.name!r} lacks a valid uploaded digest")
            if asset.digest != f"sha256:{local.sha256}" or asset.size != local.size:
                _fail(f"GitHub release asset {asset.name!r} does not match the retained file")
            uploaded.add(asset.name)
        elif asset.state == "starter":
            if not allow_starter:
                _fail(f"GitHub release asset {asset.name!r} is an incomplete starter remnant")
            if asset.size != 0 or asset.digest is not None:
                _fail(f"GitHub release starter asset {asset.name!r} has ambiguous content")
            starters.append(asset)
        else:
            _fail(f"GitHub release asset {asset.name!r} has unsupported state {asset.state!r}")
    if require_complete and (uploaded != set(expected) or starters):
        _fail("published GitHub release does not contain exactly the wheel and sdist")
    return ReleaseAssetInventory(frozenset(uploaded), tuple(starters), frozenset(names))


def _generate_release_notes(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    tag: str,
    source_sha: str,
) -> tuple[str, str]:
    url = _github_api_url(api_base, repository, "releases/generate-notes")
    _, payload = client.json(
        "POST",
        url,
        expected={200},
        json_body={"tag_name": tag, "target_commitish": source_sha},
        retry_safe=True,
    )
    record = _json_object(payload, "generated release notes response")
    name = _field(record, "name", str, "generated release notes response")
    body = _field(record, "body", str, "generated release notes response")
    _safe_text(name, "generated release name")
    _safe_multiline_text(body, "generated release body")
    return name, body


def _observe_release(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    tag: str,
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> ReleaseRecord | None:
    for attempt in range(attempts):
        release = _matching_release(
            client,
            api_base=api_base,
            repository=repository,
            tag=tag,
        )
        if release is not None:
            return release
        if attempt + 1 < attempts:
            sleeper(retry_delay)
    return None


def _observe_asset_names(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    release_id: int,
    expected: Mapping[str, LocalAsset],
    required_name: str,
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> bool:
    for attempt in range(attempts):
        assets = _release_assets(
            client,
            api_base=api_base,
            repository=repository,
            release_id=release_id,
        )
        inventory = _validate_release_assets(
            assets,
            expected,
            require_complete=False,
            allow_starter=True,
        )
        if required_name in inventory.uploaded:
            return True
        if attempt + 1 < attempts:
            sleeper(retry_delay)
    return False


def _observe_asset_absence(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    release_id: int,
    expected: Mapping[str, LocalAsset],
    required_name: str,
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> bool:
    for attempt in range(attempts):
        assets = _release_assets(
            client,
            api_base=api_base,
            repository=repository,
            release_id=release_id,
        )
        inventory = _validate_release_assets(
            assets,
            expected,
            require_complete=False,
            allow_starter=True,
        )
        if required_name not in inventory.names:
            return True
        if attempt + 1 < attempts:
            sleeper(retry_delay)
    return False


def _delete_starter_asset(
    client: ApiClient,
    *,
    api_base: str,
    repository: str,
    release_id: int,
    starter: ReleaseAssetRecord,
    expected: Mapping[str, LocalAsset],
    attempts: int,
    retry_delay: float,
    sleeper: Callable[[float], None],
) -> None:
    delete_url = _github_api_url(
        api_base,
        repository,
        f"releases/assets/{starter.asset_id}",
    )
    ambiguous: AmbiguousMutationError | None = None
    try:
        client.request(
            "DELETE",
            delete_url,
            expected={204, 404},
            retry_safe=False,
        )
    except AmbiguousMutationError as exc:
        ambiguous = exc
    absent = _observe_asset_absence(
        client,
        api_base=api_base,
        repository=repository,
        release_id=release_id,
        expected=expected,
        required_name=starter.name,
        attempts=attempts,
        retry_delay=retry_delay,
        sleeper=sleeper,
    )
    if absent:
        return
    if ambiguous is not None:
        raise AmbiguousMutationError(
            f"GitHub starter asset deletion for {starter.name!r} was not observable; rerun"
        ) from ambiguous
    _fail(f"deleted GitHub starter asset remains observable: {starter.name!r}")


def transact_github_release(
    client: ApiClient,
    *,
    api_base: str,
    uploads_base: str,
    repository: str,
    tag: str,
    source_sha: str,
    dist_dir: Path,
    wheel_name: str,
    sdist_name: str,
    curated_notes: Path,
    observe_attempts: int = 4,
    observe_delay: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Create or resume an exact draft-first GitHub release transaction."""
    if _STABLE_TAG.fullmatch(_safe_text(tag, "GitHub release tag")) is None:
        _fail("GitHub release tag is not exact stable SemVer")
    _require_sha(source_sha, "GitHub release source SHA")
    if observe_attempts < 1 or observe_delay < 0:
        raise ValueError("invalid release observation retry configuration")
    local_assets = _local_assets(dist_dir, wheel_name, sdist_name)
    expected = {asset.name: asset for asset in local_assets}
    if not curated_notes.is_file() or curated_notes.is_symlink():
        _fail(f"curated release notes file is not a regular file: {curated_notes}")
    curated_bytes = curated_notes.read_bytes()
    try:
        curated_body = curated_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReleaseOperationError("curated release notes are not valid UTF-8") from exc
    _safe_multiline_text(curated_body, "curated release notes")
    curated_segment = curated_body.strip("\n")
    curated_sha256 = hashlib.sha256(curated_bytes).hexdigest()
    if _RELEASE_MARKER_PREFIX in curated_segment:
        _fail("curated release notes contain the reserved transaction marker prefix")

    release = _matching_release(
        client,
        api_base=api_base,
        repository=repository,
        tag=tag,
    )
    if release is None:
        generated_name, generated_body = _generate_release_notes(
            client,
            api_base=api_base,
            repository=repository,
            tag=tag,
            source_sha=source_sha,
        )
        content = _build_release_content(
            tag=tag,
            source_sha=source_sha,
            curated_segment=curated_segment,
            curated_sha256=curated_sha256,
            generated_name=generated_name,
            generated_body=generated_body,
            local_assets=local_assets,
        )
        create_url = _github_api_url(api_base, repository, "releases")
        try:
            client.json(
                "POST",
                create_url,
                expected={201},
                json_body={
                    "tag_name": tag,
                    "target_commitish": source_sha,
                    "name": content.name,
                    "body": content.body,
                    "draft": True,
                    "prerelease": False,
                    "generate_release_notes": False,
                },
                retry_safe=False,
            )
        except (AmbiguousMutationError, HttpStatusError) as exc:
            release = _observe_release(
                client,
                api_base=api_base,
                repository=repository,
                tag=tag,
                attempts=observe_attempts,
                retry_delay=observe_delay,
                sleeper=sleeper,
            )
            if release is None:
                raise AmbiguousMutationError(
                    "GitHub release creation was not observable; rerun to reconcile before retrying"
                ) from exc
        else:
            release = _observe_release(
                client,
                api_base=api_base,
                repository=repository,
                tag=tag,
                attempts=observe_attempts,
                retry_delay=observe_delay,
                sleeper=sleeper,
            )
            if release is None:
                _fail("created GitHub draft release was not observable")

    _validate_release_identity(
        release,
        tag=tag,
        source_sha=source_sha,
        curated_segment=curated_segment,
        curated_sha256=curated_sha256,
        local_assets=local_assets,
    )
    observed_assets = _release_assets(
        client,
        api_base=api_base,
        repository=repository,
        release_id=release.release_id,
    )
    inventory = _validate_release_assets(
        observed_assets,
        expected,
        require_complete=not release.draft,
        allow_starter=release.draft,
    )
    if not release.draft:
        return "already-published"

    for starter in inventory.starters:
        _delete_starter_asset(
            client,
            api_base=api_base,
            repository=repository,
            release_id=release.release_id,
            starter=starter,
            expected=expected,
            attempts=observe_attempts,
            retry_delay=observe_delay,
            sleeper=sleeper,
        )
    if inventory.starters:
        observed_assets = _release_assets(
            client,
            api_base=api_base,
            repository=repository,
            release_id=release.release_id,
        )
        inventory = _validate_release_assets(
            observed_assets,
            expected,
            require_complete=False,
            allow_starter=False,
        )
    present = inventory.uploaded

    for asset in local_assets:
        if asset.name in present:
            continue
        query = urllib.parse.urlencode({"name": asset.name})
        owner, name = _repository_parts(repository)
        upload_url = (
            f"{uploads_base.rstrip('/')}/repos/{urllib.parse.quote(owner, safe='')}"
            f"/{urllib.parse.quote(name, safe='')}/releases/{release.release_id}/assets?{query}"
        )
        content_type = (
            "application/zip" if asset.package_type == "bdist_wheel" else "application/gzip"
        )
        try:
            client.request(
                "POST",
                upload_url,
                expected={201},
                raw_body=asset.path.read_bytes(),
                content_type=content_type,
                retry_safe=False,
            )
        except (AmbiguousMutationError, HttpStatusError) as exc:
            observed = _observe_asset_names(
                client,
                api_base=api_base,
                repository=repository,
                release_id=release.release_id,
                expected=expected,
                required_name=asset.name,
                attempts=observe_attempts,
                retry_delay=observe_delay,
                sleeper=sleeper,
            )
            if not observed:
                raise AmbiguousMutationError(
                    f"GitHub asset upload for {asset.name!r} was not observable; rerun to reconcile"
                ) from exc
        else:
            observed = _observe_asset_names(
                client,
                api_base=api_base,
                repository=repository,
                release_id=release.release_id,
                expected=expected,
                required_name=asset.name,
                attempts=observe_attempts,
                retry_delay=observe_delay,
                sleeper=sleeper,
            )
            if not observed:
                _fail(f"uploaded GitHub release asset was not observable: {asset.name}")

    complete_assets = _release_assets(
        client,
        api_base=api_base,
        repository=repository,
        release_id=release.release_id,
    )
    _validate_release_assets(
        complete_assets,
        expected,
        require_complete=True,
        allow_starter=False,
    )

    publish_url = _github_api_url(api_base, repository, f"releases/{release.release_id}")
    try:
        client.json(
            "PATCH",
            publish_url,
            expected={200},
            json_body={"draft": False},
            retry_safe=False,
        )
    except (AmbiguousMutationError, HttpStatusError) as exc:
        published = _observe_release(
            client,
            api_base=api_base,
            repository=repository,
            tag=tag,
            attempts=observe_attempts,
            retry_delay=observe_delay,
            sleeper=sleeper,
        )
        if published is None or published.draft:
            raise AmbiguousMutationError(
                "GitHub release publication was not observable; rerun to reconcile"
            ) from exc
    else:
        published = _observe_release(
            client,
            api_base=api_base,
            repository=repository,
            tag=tag,
            attempts=observe_attempts,
            retry_delay=observe_delay,
            sleeper=sleeper,
        )
        if published is None or published.draft:
            _fail("published GitHub release was not observable")

    _validate_release_identity(
        published,
        tag=tag,
        source_sha=source_sha,
        curated_segment=curated_segment,
        curated_sha256=curated_sha256,
        local_assets=local_assets,
    )
    final_assets = _release_assets(
        client,
        api_base=api_base,
        repository=repository,
        release_id=published.release_id,
    )
    _validate_release_assets(
        final_assets,
        expected,
        require_complete=True,
        allow_starter=False,
    )
    return "published"


def _index_base(repository: str) -> str:
    if repository == "pypi":
        return "https://pypi.org"
    if repository == "testpypi":
        return "https://test.pypi.org"
    _fail(f"unsupported Python package index: {repository!r}")


def _required_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _fail("GITHUB_TOKEN is required for this GitHub operation")
    if _ASCII_CONTROL.search(token) is not None:
        _fail("GITHUB_TOKEN contains an ASCII control character")
    return token


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("verify-source")
    source.add_argument("--repository-dir", type=Path, default=Path.cwd())
    source.add_argument("--tag", required=True)
    source.add_argument("--source-sha", required=True)
    source.add_argument("--expected-tag-object-sha")
    source.add_argument("--remote", default="origin")
    source.add_argument("--base-branch", default="master")
    source.add_argument("--github-output", type=Path)

    artifact = subparsers.add_parser("reuse-run-artifact")
    artifact.add_argument("--repository", required=True)
    artifact.add_argument("--run-id", required=True, type=int)
    artifact.add_argument("--artifact-name", required=True)
    artifact.add_argument("--expected-workflow-head-sha", required=True)
    artifact.add_argument("--dist-dir", required=True, type=Path)
    artifact.add_argument("--github-output", required=True, type=Path)

    package_index = subparsers.add_parser("reconcile-index")
    package_index.add_argument("--repository", choices=("pypi", "testpypi"), required=True)
    package_index.add_argument("--project-name", required=True)
    package_index.add_argument("--version", required=True)
    package_index.add_argument("--dist-dir", required=True, type=Path)
    package_index.add_argument("--wheel", required=True)
    package_index.add_argument("--sdist", required=True)
    package_index.add_argument("--publish-dir", type=Path)
    package_index.add_argument("--github-output", type=Path)
    package_index.add_argument("--require-complete", action="store_true")
    package_index.add_argument("--attempts", type=int, default=1)
    package_index.add_argument("--retry-delay", type=float, default=0.0)

    github_release = subparsers.add_parser("github-release")
    github_release.add_argument("--repository", required=True)
    github_release.add_argument("--tag", required=True)
    github_release.add_argument("--source-sha", required=True)
    github_release.add_argument("--dist-dir", required=True, type=Path)
    github_release.add_argument("--wheel", required=True)
    github_release.add_argument("--sdist", required=True)
    github_release.add_argument("--curated-notes", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch one release operation."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "verify-source":
            remote_source = verify_remote_source(
                args.repository_dir,
                tag=args.tag,
                source_sha=args.source_sha,
                expected_tag_object_sha=args.expected_tag_object_sha,
                remote=args.remote,
                base_branch=args.base_branch,
            )
            if args.github_output is not None:
                _write_outputs(
                    args.github_output,
                    {
                        "tag_object_sha": remote_source.tag_object_sha,
                        "source_sha": remote_source.source_sha,
                        "master_sha": remote_source.master_sha,
                        "tag_object_type": remote_source.tag_object_type,
                    },
                )
            sys.stdout.write(
                f"verified remote tag {args.tag} at {remote_source.tag_object_sha} "
                f"peeling to {remote_source.source_sha} on origin/master\n"
            )
        elif args.command == "reuse-run-artifact":
            reused = reuse_run_artifact(
                ApiClient(token=_required_token()),
                api_base="https://api.github.com",
                repository=args.repository,
                run_id=args.run_id,
                artifact_name=args.artifact_name,
                expected_workflow_head_sha=args.expected_workflow_head_sha,
                dist_dir=args.dist_dir,
                github_output=args.github_output,
            )
            sys.stdout.write(
                "reused retained run artifact\n" if reused else "no run artifact exists\n"
            )
        elif args.command == "reconcile-index":
            missing = reconcile_package_index(
                ApiClient(github_api=False),
                api_base=_index_base(args.repository),
                project_name=args.project_name,
                version=args.version,
                dist_dir=args.dist_dir,
                wheel_name=args.wheel,
                sdist_name=args.sdist,
                publish_dir=args.publish_dir,
                require_complete=args.require_complete,
                attempts=args.attempts,
                retry_delay=args.retry_delay,
            )
            if args.github_output is not None:
                _write_outputs(
                    args.github_output,
                    {
                        "publish_needed": "true" if missing else "false",
                        "missing_count": str(len(missing)),
                    },
                )
            sys.stdout.write(
                "package index contains the exact release files\n"
                if not missing
                else f"staged {len(missing)} missing package file(s)\n"
            )
        elif args.command == "github-release":
            outcome = transact_github_release(
                ApiClient(token=_required_token()),
                api_base="https://api.github.com",
                uploads_base="https://uploads.github.com",
                repository=args.repository,
                tag=args.tag,
                source_sha=args.source_sha,
                dist_dir=args.dist_dir,
                wheel_name=args.wheel,
                sdist_name=args.sdist,
                curated_notes=args.curated_notes,
            )
            sys.stdout.write(f"GitHub release transaction outcome: {outcome}\n")
        else:
            _fail(f"unsupported release operation: {args.command!r}")
    except (OSError, ReleaseOperationError, ValueError) as exc:
        sys.stderr.write(f"release operation failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
