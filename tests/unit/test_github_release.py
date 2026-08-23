"""Focused fake-GitHub contract tests for bounded release acquisition."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from archicad_mcp.schemas.github_release import (
    COMMAND_DEFINITIONS_PATH,
    COMMON_SCHEMA_DEFINITIONS_PATH,
    GITHUB_OWNER,
    GITHUB_REPOSITORY,
    LICENSE_PATH,
    MAX_RELEASE_PAGES,
    PER_PAGE,
    FetchOutcome,
    GitHubReleaseError,
    acquire_latest_stable_release,
    git_ref_url,
    git_tag_url,
    github_request_headers,
    observed_release_assets,
    parse_link_next,
    raw_source_url,
    releases_page_one_url,
    select_stable_release,
)

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"

COMMAND_JS = (
    b'var gCommands = [{"name":"C","commands":[{"name":"GetAddOnVersion","version":"0.1.0"}]}];'
)
COMMON_JS = b'var gSchemaDefinitions = {"ElementType":{"enum":["Wall"]}};'
LICENSE_BYTES = b"MIT License\n"


def release_entry(tag: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "TapirAddOn_AC27_Mac.zip"},
            {"name": "TapirAddOn_AC28_Win.apx"},
        ],
    }
    entry.update(overrides)
    return entry


class FakeGitHub:
    """Route-bound fake transport recording every requested URL."""

    def __init__(self, routes: Mapping[str, FetchOutcome]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    async def __call__(self, url: str, maximum: int) -> FetchOutcome:
        self.calls.append(url)
        del maximum
        outcome = self.routes.get(url)
        if outcome is None:
            return FetchOutcome(status=404, headers={}, body=b"not found")
        return outcome


def ok(body: object, headers: Mapping[str, str] | None = None) -> FetchOutcome:
    payload = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
    return FetchOutcome(status=200, headers=dict(headers or {}), body=payload)


def ref_outcome(sha: str, kind: str) -> FetchOutcome:
    return ok({"ref": "refs/tags/1.6.0", "object": {"sha": sha, "type": kind}})


def tag_outcome(target_sha: str, target_kind: str = "commit") -> FetchOutcome:
    return ok({"tag": "1.6.0", "object": {"sha": target_sha, "type": target_kind}})


def link_header(next_url: str | None) -> dict[str, str]:
    if next_url is None:
        return {}
    return {"Link": f'<{next_url}>; rel="next"'}


def default_routes(
    *,
    tags: list[dict[str, object]],
    commit: str = COMMIT,
    page_one_headers: Mapping[str, str] | None = None,
) -> dict[str, FetchOutcome]:
    return {
        releases_page_one_url(): ok(tags, {"ETag": '"etag-1"', **(page_one_headers or {})}),
        git_ref_url("1.6.0"): ref_outcome(commit, "commit"),
        raw_source_url(commit, COMMAND_DEFINITIONS_PATH): ok(COMMAND_JS),
        raw_source_url(commit, COMMON_SCHEMA_DEFINITIONS_PATH): ok(COMMON_JS),
        raw_source_url(commit, LICENSE_PATH): ok(LICENSE_BYTES),
    }


async def test_selection_prefers_semver_maximum_over_creation_order() -> None:
    routes = default_routes(
        tags=[
            release_entry("1.6.0"),
            release_entry("ac-addon-2024-01"),
            release_entry("1.10.0"),
            release_entry("1.9.0"),
            release_entry("2.0.0-beta.1", prerelease=True),
            release_entry("9.9.9", draft=True),
            release_entry("1.2.3.4"),
        ]
    )
    routes[git_ref_url("1.10.0")] = ref_outcome(COMMIT, "commit")
    fake = FakeGitHub(routes)
    acquisition = await acquire_latest_stable_release(fake, stored_etag=None)
    assert acquisition is not None
    assert acquisition.tag == "1.10.0"
    assert acquisition.commit == COMMIT
    assert acquisition.release_etag == '"etag-1"'
    assert acquisition.observed_assets.majors == (27, 28)
    assert acquisition.observed_assets.platforms == ("macos", "windows")
    assert fake.calls[0] == releases_page_one_url()
    assert len(fake.calls) == 5  # listing + ref + three raw fetches


async def test_304_short_circuits_without_further_requests() -> None:
    fake = FakeGitHub({releases_page_one_url(): FetchOutcome(status=304, headers={}, body=None)})
    acquisition = await acquire_latest_stable_release(fake, stored_etag='"stored"')
    assert acquisition is None
    assert fake.calls == [releases_page_one_url()]


def test_request_headers_carry_conditional_and_api_metadata() -> None:
    headers = github_request_headers('"e"')
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["If-None-Match"] == '"e"'
    assert headers["User-Agent"].startswith("archicad-mcp/")
    bare = github_request_headers(None)
    assert "If-None-Match" not in bare


def test_link_next_parsing_refuses_host_or_path_drift() -> None:
    good = f"{API_BASE}/releases?per_page=100&page=2"
    assert parse_link_next(f'<{good}>; rel="next"') == good
    evil = "https://evil.example/repos/x/y/releases?page=2"
    with pytest.raises(GitHubReleaseError) as caught:
        parse_link_next(f'<{evil}>; rel="next"')
    assert caught.value.code == "pagination-host-drift"
    offpath = f"{API_BASE}/orgs/other/releases?page=2"
    with pytest.raises(GitHubReleaseError) as caught:
        parse_link_next(f'<{offpath}>; rel="next"')
    assert caught.value.code == "pagination-drift"
    with pytest.raises(GitHubReleaseError) as caught:
        parse_link_next('<https://api.github.com/x>; rel="next"')
    assert caught.value.code == "pagination-drift"
    assert parse_link_next(f'<{good}>; rel="prev"') is None
    assert parse_link_next(None) is None


async def test_pagination_follows_same_origin_rel_next(monkeypatch: pytest.MonkeyPatch) -> None:
    page_two = f"{API_BASE}/releases?per_page=100&page=2"
    routes = {
        releases_page_one_url(): ok(
            [release_entry("1.6.0")], {"Link": f'<{page_two}>; rel="next"'}
        ),
        page_two: ok([release_entry("1.7.0"), release_entry("2.0.0-rc1", prerelease=True)]),
        git_ref_url("1.7.0"): ref_outcome(COMMIT, "commit"),
        raw_source_url(COMMIT, COMMAND_DEFINITIONS_PATH): ok(COMMAND_JS),
        raw_source_url(COMMIT, COMMON_SCHEMA_DEFINITIONS_PATH): ok(COMMON_JS),
        raw_source_url(COMMIT, LICENSE_PATH): ok(LICENSE_BYTES),
    }
    fake = FakeGitHub(routes)
    acquisition = await acquire_latest_stable_release(fake, stored_etag=None)
    assert acquisition is not None and acquisition.tag == "1.7.0"
    assert fake.calls[:2] == [releases_page_one_url(), page_two]


async def test_pagination_limit_after_page_five_is_explicit_failure() -> None:
    # Regression: silent truncation after the bounded page count hid drift.
    routes: dict[str, FetchOutcome] = {}
    for page in range(1, MAX_RELEASE_PAGES + 1):
        url = (
            releases_page_one_url()
            if page == 1
            else f"{API_BASE}/releases?per_page=100&page={page}"
        )
        next_url = f"{API_BASE}/releases?per_page=100&page={page + 1}"
        routes[url] = ok([release_entry("1.6.0")], link_header(next_url))
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "pagination-limit-exceeded"


async def test_pagination_host_drift_is_a_stable_refusal_not_truncation() -> None:
    evil = "https://evil.example/repos/x/y/releases?page=2"
    routes = {
        releases_page_one_url(): ok([release_entry("1.6.0")], link_header(evil)),
    }
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "pagination-host-drift"


@pytest.mark.parametrize(
    "query",
    [
        "page=2&per_page=100",
        "per_page=100&page=3",
    ],
)
def test_pagination_query_is_order_independent_but_exact(query: str) -> None:
    good = f"{API_BASE}/releases?{query}"
    assert parse_link_next(f'<{good}>; rel="next"') == good


@pytest.mark.parametrize(
    "query",
    [
        "per_page=50&page=2",
        "per_page=100&page=2&foo=bar",
        "page=2",
        "per_page=100&page=abc",
        "per_page=100&page=0",
        "per_page=100&page=999",
        "per_page=100&page=\u0660",
        "",
    ],
)
def test_pagination_query_drift_is_refused(query: str) -> None:
    drifted = f"{API_BASE}/releases?{query}" if query else f"{API_BASE}/releases"
    with pytest.raises(GitHubReleaseError) as caught:
        parse_link_next(f'<{drifted}>; rel="next"')
    assert caught.value.code == "pagination-drift"


async def test_pagination_must_advance_one_page_at_a_time() -> None:
    jumped = f"{API_BASE}/releases?per_page=100&page=4"
    routes = {
        releases_page_one_url(): ok([release_entry("1.6.0")], link_header(jumped)),
    }
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "pagination-drift"


async def test_oversized_pages_and_totals_are_bounded() -> None:
    oversized = [release_entry("1.6.0") for _ in range(PER_PAGE + 1)]
    fake = FakeGitHub({releases_page_one_url(): ok(oversized)})
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(fake, stored_etag=None)
    assert caught.value.code == "layout-drift:releases-page-1"

    # Five full pages followed by another next link stay explicitly bounded.
    routes: dict[str, FetchOutcome] = {}
    for page in range(1, MAX_RELEASE_PAGES + 1):
        url = (
            releases_page_one_url()
            if page == 1
            else f"{API_BASE}/releases?per_page=100&page={page}"
        )
        routes[url] = ok(
            [release_entry("1.6.0")],
            link_header(f"{API_BASE}/releases?per_page=100&page={page + 1}"),
        )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "pagination-limit-exceeded"


async def test_api_json_rejects_duplicate_keys_and_nan() -> None:
    duplicate = FakeGitHub(
        {
            releases_page_one_url(): FetchOutcome(
                status=200,
                headers={},
                body=b'[{"tag_name": "1.6.0", "tag_name": "1.6.0"}]',
            )
        }
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(duplicate, stored_etag=None)
    assert caught.value.code == "malformed-json:releases-page-1"
    nan = FakeGitHub({releases_page_one_url(): FetchOutcome(status=200, headers={}, body=b"[NaN]")})
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(nan, stored_etag=None)
    assert caught.value.code == "malformed-json:releases-page-1"


async def test_asset_provenance_comes_only_from_the_selected_release() -> None:
    # Regression: aggregating every entry's assets invented compatibility.
    selected = release_entry("2.0.0", assets=[{"name": "TapirAddOn_AC30_Win.apx"}])
    other_newer_looking = release_entry("1.6.0", assets=[{"name": "TapirAddOn_AC99_Mac.zip"}])
    routes = default_routes(tags=[other_newer_looking, selected])
    routes[git_ref_url("2.0.0")] = ref_outcome(COMMIT, "commit")
    routes.update(RAW_ROUTES)
    fake = FakeGitHub(routes)
    acquisition = await acquire_latest_stable_release(fake, stored_etag=None)
    assert acquisition is not None and acquisition.tag == "2.0.0"
    assert acquisition.observed_assets.majors == (30,)
    assert acquisition.observed_assets.platforms == ("windows",)


def hex_sha(marker: str) -> str:
    """Return a distinct valid 40-hex object id for one fake chain node."""

    return (marker + "0" * 40)[:40]


def peel_routes(chain: list[tuple[str, str]], final_commit: str) -> dict[str, FetchOutcome]:
    """Build ref/peel routes for an annotated chain ending at ``final_commit``."""

    routes: dict[str, FetchOutcome] = {}
    for index, (sha, kind) in enumerate(chain):
        if index == 0:
            routes[git_ref_url("1.6.0")] = ref_outcome(sha, kind)
        if kind != "tag":
            break
        target_sha, target_kind = (
            chain[index + 1] if index + 1 < len(chain) else (final_commit, "commit")
        )
        routes[git_tag_url(sha)] = tag_outcome(target_sha, target_kind)
    return routes


RAW_ROUTES = {
    raw_source_url(COMMIT, COMMAND_DEFINITIONS_PATH): ok(COMMAND_JS),
    raw_source_url(COMMIT, COMMON_SCHEMA_DEFINITIONS_PATH): ok(COMMON_JS),
    raw_source_url(COMMIT, LICENSE_PATH): ok(LICENSE_BYTES),
}


async def test_annotated_tag_peels_to_exact_commit() -> None:
    annotated = hex_sha("b")
    routes = default_routes(tags=[release_entry("1.6.0")])
    routes.update(peel_routes([(annotated, "tag")], COMMIT))
    routes.update(RAW_ROUTES)
    fake = FakeGitHub(routes)
    acquisition = await acquire_latest_stable_release(fake, stored_etag=None)
    assert acquisition is not None and acquisition.commit == COMMIT
    assert git_tag_url(annotated) in fake.calls


async def test_nested_annotation_peels_within_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = [(hex_sha(chr(98 + i)), "tag") for i in range(3)]
    routes = peel_routes(chain, COMMIT)
    routes[releases_page_one_url()] = ok([release_entry("1.6.0")])
    routes.update(RAW_ROUTES)
    fake = FakeGitHub(routes)
    monkeypatch.setattr("archicad_mcp.schemas.github_release.MAX_PEEL_DEPTH", 4)
    acquisition = await acquire_latest_stable_release(fake, stored_etag=None)
    assert acquisition is not None and acquisition.commit == COMMIT


async def test_peeling_beyond_depth_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    chain = [(hex_sha(chr(98 + i)), "tag") for i in range(3)]
    routes = peel_routes(chain, COMMIT)
    routes[releases_page_one_url()] = ok([release_entry("1.6.0")])
    monkeypatch.setattr("archicad_mcp.schemas.github_release.MAX_PEEL_DEPTH", 2)
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "peel-depth-exceeded"


async def test_non_tag_object_kind_is_refused() -> None:
    routes = default_routes(tags=[release_entry("1.6.0")], commit=COMMIT)
    routes[git_ref_url("1.6.0")] = ref_outcome("f" * 40, "blob")
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "commit-not-resolved"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (403, "rate-limit"),
        (429, "rate-limit"),
        (302, "redirect-refused"),
        (404, "missing:"),
        (500, "http-500:"),
    ],
)
async def test_listing_status_faults_map_to_stable_codes(status: int, expected_code: str) -> None:
    fake = FakeGitHub(
        {releases_page_one_url(): FetchOutcome(status=status, headers={}, body=b"{}")}
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(fake, stored_etag=None)
    assert caught.value.code.startswith(expected_code)


async def test_raw_fetch_faults_are_bounded() -> None:
    base = default_routes(tags=[release_entry("1.6.0")])
    base[git_ref_url("1.6.0")] = ref_outcome(COMMIT, "commit")
    empty = dict(base)
    empty[raw_source_url(COMMIT, COMMAND_DEFINITIONS_PATH)] = FetchOutcome(
        status=200, headers={}, body=b""
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(empty), stored_etag=None)
    assert caught.value.code == "empty-response:docs/archicad-addon/command_definitions.js"

    redirect = dict(base)
    redirect[raw_source_url(COMMIT, COMMON_SCHEMA_DEFINITIONS_PATH)] = FetchOutcome(
        status=302, headers={"Location": "https://evil.example/x"}, body=None
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(redirect), stored_etag=None)
    assert caught.value.code == "redirect-refused"

    oversize = dict(base)
    oversize[raw_source_url(COMMIT, LICENSE_PATH)] = ok(b"x" * (64 * 1024 + 1))
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(oversize), stored_etag=None)
    assert caught.value.code == "response-size"


async def test_missing_raw_path_and_malformed_listing_are_diagnostics() -> None:
    routes = default_routes(tags=[release_entry("1.6.0")])
    routes[git_ref_url("1.6.0")] = ref_outcome(COMMIT, "commit")
    del routes[raw_source_url(COMMIT, LICENSE_PATH)]
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(FakeGitHub(routes), stored_etag=None)
    assert caught.value.code == "missing:LICENSE"

    malformed = FakeGitHub(
        {releases_page_one_url(): FetchOutcome(status=200, headers={}, body=b"{not json")}
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(malformed, stored_etag=None)
    # Diagnostics are stable and never embed Python parser messages.
    assert caught.value.code == "malformed-json:releases-page-1"

    drift = FakeGitHub({releases_page_one_url(): ok({"not": "a list"})})
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(drift, stored_etag=None)
    assert caught.value.code == "layout-drift:releases-page-1"


async def test_no_eligible_stable_release_is_a_stable_diagnostic() -> None:
    fake = FakeGitHub(
        {
            releases_page_one_url(): ok(
                [
                    release_entry("ac-addon-x"),
                    release_entry("1.0.0-rc.1", prerelease=True),
                ]
            ),
            git_ref_url("1.6.0"): ref_outcome(COMMIT, "commit"),
            raw_source_url(COMMIT, COMMAND_DEFINITIONS_PATH): ok(COMMAND_JS),
            raw_source_url(COMMIT, COMMON_SCHEMA_DEFINITIONS_PATH): ok(COMMON_JS),
            raw_source_url(COMMIT, LICENSE_PATH): ok(LICENSE_BYTES),
        }
    )
    with pytest.raises(GitHubReleaseError) as caught:
        await acquire_latest_stable_release(fake, stored_etag=None)
    assert caught.value.code == "no-stable-release"
    assert all("evil" not in call for call in fake.calls)


def test_select_stable_release_sensitivity_to_wrong_orderings() -> None:
    entries = [release_entry("1.10.0"), release_entry("1.9.0"), release_entry("2.0.0")]
    best = select_stable_release(entries)
    assert best is not None and best.version == "2.0.0"
    # A creation-order implementation would wrongly pick the first entry.
    assert best.version != entries[0]["tag_name"]


def test_asset_grammar_only_matches_documented_names() -> None:
    observed = observed_release_assets(
        {
            "assets": [
                {"name": "TapirAddOn_AC27_Mac.zip"},
                {"name": "TapirAddOn_AC28_Win.apx"},
                {"name": "source.tar.gz"},
                {"name": "TapirAddOn_AC0_Mac.zip"},
            ]
        }
    )
    assert observed.majors == (27, 28)
    assert observed.platforms == ("macos", "windows")


def test_selected_release_assets_fail_closed_on_untyped_entries() -> None:
    with pytest.raises(GitHubReleaseError) as caught:
        observed_release_assets({"assets": [{"name": "TapirAddOn_AC27_Mac.zip"}, 42]})
    assert caught.value.code == "layout-drift:release-asset"
    with pytest.raises(GitHubReleaseError) as caught:
        observed_release_assets({"assets": [{"name": 42}]})
    assert caught.value.code == "layout-drift:release-asset"
    with pytest.raises(GitHubReleaseError) as caught:
        observed_release_assets({"assets": "not-a-list"})
    assert caught.value.code == "layout-drift:release-assets"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tag_name": 17},
        {"tag_name": None},
        {"draft": "false"},
        {"draft": None},
        {"prerelease": None},
        {"assets": None},
        {"assets": "not-a-list"},
    ],
)
def test_mistyped_release_entries_are_layout_drift(overrides: dict[str, object]) -> None:
    entry = release_entry("1.6.0")
    for key, value in overrides.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    with pytest.raises(GitHubReleaseError) as caught:
        select_stable_release([entry])
    assert caught.value.code == "layout-drift:release-entry"


def test_non_dict_entries_are_layout_drift() -> None:
    from archicad_mcp.schemas.github_release import GitHubReleaseError as err

    with pytest.raises(err) as caught:
        select_stable_release(["not-a-dict"])
    assert caught.value.code == "layout-drift:release-entry"
