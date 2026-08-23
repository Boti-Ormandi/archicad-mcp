"""Bounded direct acquisition of stable Tapir releases from GitHub.

The repository is fixed to ``ENZYME-APD/tapir-archicad-automation`` on
``github.com``. First acquisition trusts that public repository, GitHub TLS,
and the stable release metadata presented on first use; no token, credential,
hosted feed, or signature machinery is involved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, NoReturn, cast
from urllib.parse import parse_qsl, quote, urlsplit

from archicad_mcp.schemas.semver import compare_semver, is_stable_release_version

GITHUB_OWNER: Final[str] = "ENZYME-APD"
GITHUB_REPOSITORY: Final[str] = "tapir-archicad-automation"
UPSTREAM_REPOSITORY_URL: Final[str] = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
RELEASES_API_URL: Final[str] = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"
)
RAW_BASE_URL: Final[str] = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
COMMAND_DEFINITIONS_PATH: Final[str] = "docs/archicad-addon/command_definitions.js"
COMMON_SCHEMA_DEFINITIONS_PATH: Final[str] = "docs/archicad-addon/common_schema_definitions.js"
LICENSE_PATH: Final[str] = "LICENSE"

MAX_API_RESPONSE_BYTES: Final[int] = 4 * 1024 * 1024
SOURCE_MAX_BYTES: Final[int] = 16 * 1024 * 1024
LICENSE_MAX_BYTES: Final[int] = 64 * 1024
REQUEST_TIMEOUT_SECONDS: Final[float] = 10.0
ATTEMPT_TIMEOUT_SECONDS: Final[float] = 60.0
MAX_RELEASE_PAGES: Final[int] = 5
MAX_PEEL_DEPTH: Final[int] = 4
PER_PAGE: Final[int] = 100
GITHUB_API_VERSION: Final[str] = "2022-11-28"

_ASSET_GRAMMAR_RE: Final[re.Pattern[str]] = re.compile(
    r"^TapirAddOn_AC([1-9][0-9]{0,3})_(Mac\.zip|Win\.apx)$"
)
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_RELEASES_PATH: Final[str] = f"/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/releases"


class GitHubReleaseError(ValueError):
    """A bounded acquisition failure represented by a stable code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise GitHubReleaseError(code)


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """One completed bounded HTTP exchange without redirect following."""

    status: int
    headers: Mapping[str, str]
    body: bytes | None


FetchFn = Callable[[str, int], Awaitable["FetchOutcome"]]


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """One selected stable release identity."""

    tag: str
    version: str


@dataclass(frozen=True, slots=True)
class ObservedAssets:
    """The supported-major/platform set observed in release assets."""

    majors: tuple[int, ...]
    platforms: tuple[str, ...]

    def as_json(self) -> dict[str, list[Any]]:
        return {"majors": list(self.majors), "platforms": sorted(self.platforms)}


EMPTY_OBSERVED_ASSETS = ObservedAssets(majors=(), platforms=())


@dataclass(frozen=True, slots=True)
class TapirReleaseAcquisition:
    """Complete verified acquisition payload for one stable upstream release."""

    tag: str
    version: str
    commit: str
    command_definitions: bytes
    common_schema_definitions: bytes
    license_bytes: bytes
    release_etag: str | None
    observed_assets: ObservedAssets


def _user_agent() -> str:
    from archicad_mcp import __version__

    return f"archicad-mcp/{__version__}"


def github_request_headers(stored_etag: str | None) -> dict[str, str]:
    """Return the exact bounded request headers for the releases listing."""

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": _user_agent(),
    }
    if stored_etag is not None:
        headers["If-None-Match"] = stored_etag
    return headers


def minimal_request_headers(url: str) -> dict[str, str]:
    """Return the bounded non-conditional headers for API and raw requests."""

    headers = {"User-Agent": _user_agent()}
    if urlsplit(url).netloc == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
    return headers


def releases_page_one_url() -> str:
    return f"{RELEASES_API_URL}?per_page={PER_PAGE}&page=1"


def raw_source_url(commit: str, path: str) -> str:
    return f"{RAW_BASE_URL}/{commit}/{path}"


def git_ref_url(tag: str) -> str:
    return (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
        f"/git/ref/tags/{quote(tag, safe='')}"
    )


def git_tag_url(sha: str) -> str:
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/git/tags/{sha}"


def _header(outcome_headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in outcome_headers.items():
        if key.lower() == lowered and value != "":
            return value
    return None


def _release_page_number(url: str) -> int:
    """Require the exact repository release-list URL and return its page."""

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "api.github.com":
        _fail("pagination-host-drift")
    if parts.path != _RELEASES_PATH or parts.fragment:
        _fail("pagination-drift")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    params = dict(pairs)
    if len(params) != len(pairs) or frozenset(params) != {"per_page", "page"}:
        _fail("pagination-drift")
    page_text = params["page"]
    if (
        params["per_page"] != str(PER_PAGE)
        or not page_text.isascii()
        or not page_text.isdigit()
        or page_text.startswith("0")
        or len(page_text) > len(str(MAX_RELEASE_PAGES + 1))
    ):
        _fail("pagination-drift")
    page = int(page_text)
    if not 1 <= page <= MAX_RELEASE_PAGES + 1:
        _fail("pagination-drift")
    return page


def parse_link_next(header_value: str | None) -> str | None:
    """Return the same-origin ``rel=next`` target from one Link header.

    Host- or path-drifting targets are a stable pagination refusal, never a
    silent stop; a ``next`` target without the documented angle-bracket form
    is refused as layout drift.
    """

    if header_value is None:
        return None
    for segment in header_value.split(","):
        parts = segment.split(";")
        if len(parts) < 2:
            continue
        target = parts[0].strip()
        for parameter in parts[1:]:
            key, separator, value = parameter.partition("=")
            if separator and key.strip() == "rel" and value.strip().strip('"') == "next":
                if not (target.startswith("<") and target.endswith(">")):
                    _fail("pagination-drift")
                candidate = target[1:-1]
                _release_page_number(candidate)
                return candidate
    return None


def _reject_json_constant(raw: str) -> NoReturn:
    raise ValueError(f"forbidden-constant:{raw}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate-json-key:{key}")
        result[key] = value
    return result


def _decode_json_body(outcome: FetchOutcome, label: str) -> Any:
    body = outcome.body
    if not body:
        _fail(f"empty-response:{label}")
    if len(body) > MAX_API_RESPONSE_BYTES:
        _fail("response-size")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(f"invalid-utf8:{label}")
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        del exc
        _fail(f"malformed-json:{label}")


def _checked_status(outcome: FetchOutcome, label: str) -> FetchOutcome:
    status = outcome.status
    if status in (403, 429):
        _fail("rate-limit")
    if 300 <= status < 400:
        _fail("redirect-refused")
    if status == 404:
        _fail(f"missing:{label}")
    if status != 200:
        _fail(f"http-{status}:{label}")
    return outcome


async def list_all_releases(
    fetch: FetchFn,
    *,
    stored_etag: str | None,
) -> tuple[list[Any] | None, str | None]:
    """Conditionally list bounded release pages; entries ``None`` means current.

    Page one carries the stored ETag; ``304`` short-circuits the whole listing
    without further requests. Only same-origin GitHub ``Link rel=next`` targets
    are followed; drifting targets are refused, and a ``next`` link after the
    final bounded page is an explicit pagination-limit failure.
    """

    url: str = releases_page_one_url()
    etag = stored_etag
    entries: list[Any] = []
    page_one_etag: str | None = None
    for page in range(1, MAX_RELEASE_PAGES + 1):
        outcome = await fetch(url, MAX_API_RESPONSE_BYTES)
        if page == 1 and outcome.status == 304 and etag is not None:
            return None, etag
        _checked_status(outcome, f"releases-page-{page}")
        if page == 1:
            page_one_etag = _header(outcome.headers, "ETag")
            etag = page_one_etag
        page_entries = _decode_json_body(outcome, f"releases-page-{page}")
        if type(page_entries) is not list:
            _fail(f"layout-drift:releases-page-{page}")
        if len(page_entries) > PER_PAGE:
            _fail(f"layout-drift:releases-page-{page}")
        entries.extend(page_entries)
        if len(entries) > MAX_RELEASE_PAGES * PER_PAGE:
            _fail("pagination-limit-exceeded")
        next_url = parse_link_next(_header(outcome.headers, "Link"))
        if next_url is None:
            return entries, page_one_etag
        if _release_page_number(next_url) != page + 1:
            _fail("pagination-drift")
        if page == MAX_RELEASE_PAGES:
            _fail("pagination-limit-exceeded")
        url = next_url
    raise AssertionError("unreachable pagination loop exit")


def observed_release_assets(entry: Mapping[str, Any]) -> ObservedAssets:
    """Read the supported-major/platform set from documented asset names only.

    The selected release's asset list must be well formed: every asset entry
    is an object carrying a string ``name``; unrelated valid names that do
    not match the documented grammar are ignored.
    """

    assets = entry.get("assets")
    if not isinstance(assets, list):
        _fail("layout-drift:release-assets")
    majors: set[int] = set()
    platforms: set[str] = set()
    for asset in assets:
        if type(asset) is not dict:
            _fail("layout-drift:release-asset")
        name = cast(dict[str, Any], asset).get("name")
        if type(name) is not str:
            _fail("layout-drift:release-asset")
        match = _ASSET_GRAMMAR_RE.fullmatch(name)
        if match is not None:
            majors.add(int(match.group(1)))
            platforms.add("macos" if match.group(2) == "Mac.zip" else "windows")
    return ObservedAssets(majors=tuple(sorted(majors)), platforms=tuple(sorted(platforms)))


def select_stable_release(entries: Sequence[Any]) -> ReleaseCandidate | None:
    """Filter stable bare SemVer tags and select the strict SemVer maximum.

    Every listing entry must carry correctly typed ``tag_name``, ``draft``,
    ``prerelease``, and ``assets`` fields; mistyped entries are layout drift,
    never silent skips.
    """

    best: ReleaseCandidate | None = None
    for entry in entries:
        if type(entry) is not dict:
            _fail("layout-drift:release-entry")
        candidate = cast(Mapping[str, Any], entry)
        tag = candidate.get("tag_name")
        draft = candidate.get("draft")
        prerelease = candidate.get("prerelease")
        assets = candidate.get("assets")
        if (
            type(tag) is not str
            or type(draft) is not bool
            or type(prerelease) is not bool
            or type(assets) is not list
        ):
            _fail("layout-drift:release-entry")
        if draft or prerelease or not is_stable_release_version(tag):
            continue
        if best is None or compare_semver(tag, best.version) > 0:
            best = ReleaseCandidate(tag=tag, version=tag)
    return best


def _object_identity(payload: object, label: str) -> tuple[str, str]:
    """Extract one 40-hex object SHA and its Git object kind."""

    if type(payload) is not dict or type(cast(dict[str, Any], payload).get("object")) is not dict:
        _fail(f"layout-drift:{label}")
    mapping = cast(dict[str, Any], payload)
    target = cast(dict[str, Any], mapping["object"])
    sha = target.get("sha")
    kind = target.get("type")
    if type(sha) is not str or _SHA_RE.fullmatch(sha) is None or type(kind) is not str:
        _fail(f"layout-drift:{label}")
    return sha, kind


async def resolve_tag_commit(fetch: FetchFn, tag: str) -> str:
    """Resolve one exact tag through the ref API and peel annotated tags."""

    outcome = await fetch(git_ref_url(tag), MAX_API_RESPONSE_BYTES)
    sha, kind = _object_identity(
        _decode_json_body(_checked_status(outcome, "release-ref"), "release-ref"),
        "release-ref",
    )
    for _ in range(MAX_PEEL_DEPTH):
        if kind == "commit":
            return sha
        if kind != "tag":
            _fail("commit-not-resolved")
        peel = await fetch(git_tag_url(sha), MAX_API_RESPONSE_BYTES)
        sha, kind = _object_identity(
            _decode_json_body(_checked_status(peel, "tag-peel"), "tag-peel"),
            "tag-peel",
        )
    _fail("peel-depth-exceeded")


async def _fetch_raw(fetch: FetchFn, commit: str, path: str, maximum: int) -> bytes:
    url = raw_source_url(commit, path)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "raw.githubusercontent.com"
        or not parts.path.startswith(f"/{GITHUB_OWNER}/{GITHUB_REPOSITORY}/{commit}/")
    ):
        _fail("host-drift")
    outcome = await fetch(url, maximum)
    _checked_status(outcome, path)
    body = outcome.body
    if not body:
        _fail(f"empty-response:{path}")
    if len(body) > maximum:
        _fail("response-size")
    return body


async def fetch_release_inputs(fetch: FetchFn, commit: str) -> tuple[bytes, bytes, bytes]:
    """Fetch exactly the three pinned raw paths at the peeled commit."""

    command_bytes = await _fetch_raw(fetch, commit, COMMAND_DEFINITIONS_PATH, SOURCE_MAX_BYTES)
    common_bytes = await _fetch_raw(fetch, commit, COMMON_SCHEMA_DEFINITIONS_PATH, SOURCE_MAX_BYTES)
    license_bytes = await _fetch_raw(fetch, commit, LICENSE_PATH, LICENSE_MAX_BYTES)
    return command_bytes, common_bytes, license_bytes


async def acquire_latest_stable_release(
    fetch: FetchFn,
    *,
    stored_etag: str | None,
) -> TapirReleaseAcquisition | None:
    """Acquire the newest stable release payload, or ``None`` when current.

    The listing is conditional on the stored page-one ETag; ``304`` means the
    known listing is still current and no further requests are made. Selection,
    tag resolution, peeling, and raw input fetches follow the bounded protocol;
    every failure raises :class:`GitHubReleaseError` with a stable code.
    """

    entries, etag = await list_all_releases(fetch, stored_etag=stored_etag)
    if entries is None:
        return None
    selected = select_stable_release(entries)
    if selected is None:
        _fail("no-stable-release")
    selected_entry = next(
        entry
        for entry in entries
        if type(entry) is dict
        and cast(Mapping[str, Any], entry).get("tag_name") == selected.tag
        and cast(Mapping[str, Any], entry).get("draft") is False
        and cast(Mapping[str, Any], entry).get("prerelease") is False
    )
    # Asset provenance describes exactly the selected release; assets of any
    # other listing entry never leak into the acquisition record.
    observed_assets = observed_release_assets(cast(Mapping[str, Any], selected_entry))
    commit = await resolve_tag_commit(fetch, selected.tag)
    command_bytes, common_bytes, license_bytes = await fetch_release_inputs(fetch, commit)
    return TapirReleaseAcquisition(
        tag=selected.tag,
        version=selected.version,
        commit=commit,
        command_definitions=command_bytes,
        common_schema_definitions=common_bytes,
        license_bytes=license_bytes,
        release_etag=etag,
        observed_assets=observed_assets,
    )


__all__ = [
    "ATTEMPT_TIMEOUT_SECONDS",
    "COMMAND_DEFINITIONS_PATH",
    "COMMON_SCHEMA_DEFINITIONS_PATH",
    "EMPTY_OBSERVED_ASSETS",
    "GITHUB_API_VERSION",
    "GITHUB_OWNER",
    "GITHUB_REPOSITORY",
    "LICENSE_MAX_BYTES",
    "LICENSE_PATH",
    "MAX_API_RESPONSE_BYTES",
    "MAX_PEEL_DEPTH",
    "MAX_RELEASE_PAGES",
    "PER_PAGE",
    "RAW_BASE_URL",
    "RELEASES_API_URL",
    "REQUEST_TIMEOUT_SECONDS",
    "SOURCE_MAX_BYTES",
    "UPSTREAM_REPOSITORY_URL",
    "FetchFn",
    "FetchOutcome",
    "GitHubReleaseError",
    "ObservedAssets",
    "ReleaseCandidate",
    "TapirReleaseAcquisition",
    "acquire_latest_stable_release",
    "fetch_release_inputs",
    "git_ref_url",
    "git_tag_url",
    "github_request_headers",
    "list_all_releases",
    "minimal_request_headers",
    "observed_release_assets",
    "parse_link_next",
    "raw_source_url",
    "releases_page_one_url",
    "resolve_tag_commit",
    "select_stable_release",
]
