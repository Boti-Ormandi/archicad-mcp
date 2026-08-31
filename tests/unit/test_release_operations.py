from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import threading
import urllib.parse
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from scripts.release_operations import (
    AmbiguousMutationError,
    ApiClient,
    ReleaseOperationError,
    reconcile_package_index,
    reuse_run_artifact,
    transact_github_release,
    verify_remote_source,
)

_REPOSITORY = "acme/example"
_RUN_ID = 101
_HEAD_SHA = "a" * 40
_WORKFLOW_HEAD_SHA = "d" * 40
_ARTIFACT_NAME = f"release-dist-{_RUN_ID}"
_TAG = "v1.2.3"
_VERSION = "1.2.3"
_WHEEL = "archicad_mcp-1.2.3-py3-none-any.whl"
_SDIST = "archicad_mcp-1.2.3.tar.gz"


@dataclass
class FakeApiState:
    artifact_records: list[dict[str, object]] = field(default_factory=list)
    artifact_zip: bytes = b""
    index_sequences: list[list[dict[str, object]]] = field(default_factory=lambda: [[]])
    index_reads: int = 0
    index_missing: bool = False
    release: dict[str, object] | None = None
    assets: list[dict[str, object]] = field(default_factory=list)
    faults: set[str] = field(default_factory=set)
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)
    mutations: dict[str, int] = field(default_factory=dict)
    create_payload: dict[str, object] | None = None
    release_hidden_reads: int = 0


class FakeHttpServer(ThreadingHTTPServer):
    state: FakeApiState

    def __init__(self, state: FakeApiState) -> None:
        super().__init__(("127.0.0.1", 0), FakeApiHandler)
        self.state = state


class FakeApiHandler(BaseHTTPRequestHandler):
    server: FakeHttpServer

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _json_body(self) -> dict[str, object]:
        payload = json.loads(self._body() or b"{}")
        assert isinstance(payload, dict)
        return cast(dict[str, object], payload)

    def _record(self) -> urllib.parse.SplitResult:
        parsed = urllib.parse.urlsplit(self.path)
        self.server.state.requests.append(
            (self.command, self.path, self.headers.get("X-GitHub-Api-Version"))
        )
        return parsed

    def _respond_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_bytes(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _lose_response(self) -> None:
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def _release_payload(self) -> dict[str, object]:
        release = self.server.state.release
        assert release is not None
        return dict(release)

    def do_GET(self) -> None:
        parsed = self._record()
        state = self.server.state
        if parsed.path.endswith(f"/actions/runs/{_RUN_ID}/artifacts"):
            self._respond_json(
                200,
                {
                    "total_count": len(state.artifact_records),
                    "artifacts": state.artifact_records,
                },
            )
            return
        if parsed.path.endswith("/actions/artifacts/7/zip"):
            self._respond_bytes(200, state.artifact_zip)
            return
        if parsed.path == f"/pypi/archicad-mcp/{_VERSION}/json":
            state.index_reads += 1
            if state.index_missing:
                self._respond_json(404, {"message": "release not found"})
            else:
                position = min(state.index_reads - 1, len(state.index_sequences) - 1)
                self._respond_json(200, {"urls": state.index_sequences[position]})
            return
        if parsed.path.endswith("/releases"):
            if state.release_hidden_reads:
                state.release_hidden_reads -= 1
                self._respond_json(200, [])
            else:
                self._respond_json(200, [] if state.release is None else [self._release_payload()])
            return
        if "/releases/1/assets" in parsed.path:
            self._respond_json(200, state.assets)
            return
        self._respond_json(404, {"message": "not found"})

    def do_POST(self) -> None:
        parsed = self._record()
        state = self.server.state
        if parsed.path.endswith("/releases"):
            payload = self._json_body()
            state.create_payload = dict(payload)
            state.mutations["create"] = state.mutations.get("create", 0) + 1
            state.release = {
                "id": 1,
                "tag_name": payload["tag_name"],
                "target_commitish": payload["target_commitish"],
                "name": payload["name"],
                "body": payload["body"],
                "draft": payload["draft"],
                "prerelease": payload["prerelease"],
            }
            if "create-response-loss-unobservable" in state.faults:
                state.faults.remove("create-response-loss-unobservable")
                state.release_hidden_reads = 2
                self._lose_response()
                return
            if "create-response-loss" in state.faults:
                state.faults.remove("create-response-loss")
                self._lose_response()
                return
            self._respond_json(201, self._release_payload())
            return
        if parsed.path.endswith("/releases/1/assets"):
            query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            asset_name = query["name"][0]
            body = self._body()
            state.mutations[f"upload:{asset_name}"] = (
                state.mutations.get(f"upload:{asset_name}", 0) + 1
            )
            if any(asset["name"] == asset_name for asset in state.assets):
                self._respond_json(422, {"message": "already exists"})
                return
            next_asset_id = (
                max(
                    (cast(int, existing["id"]) for existing in state.assets),
                    default=9,
                )
                + 1
            )
            asset = {
                "id": next_asset_id,
                "name": asset_name,
                "digest": f"sha256:{hashlib.sha256(body).hexdigest()}",
                "state": "uploaded",
                "size": len(body),
            }
            state.assets.append(asset)
            fault = f"upload-response-loss:{asset_name}"
            if fault in state.faults:
                state.faults.remove(fault)
                self._lose_response()
                return
            self._respond_json(201, asset)
            return
        self._respond_json(404, {"message": "not found"})

    def do_DELETE(self) -> None:
        parsed = self._record()
        state = self.server.state
        marker = "/releases/assets/"
        if marker in parsed.path:
            asset_id = int(parsed.path.rsplit("/", maxsplit=1)[1])
            asset = next((item for item in state.assets if item["id"] == asset_id), None)
            if asset is None:
                self._respond_bytes(404, b"")
                return
            asset_name = cast(str, asset["name"])
            state.mutations[f"delete:{asset_name}"] = (
                state.mutations.get(f"delete:{asset_name}", 0) + 1
            )
            still_present = f"delete-still-present:{asset_name}" in state.faults
            if not still_present:
                state.assets.remove(asset)
            response_loss = f"delete-response-loss:{asset_name}"
            if response_loss in state.faults:
                state.faults.remove(response_loss)
                self._lose_response()
                return
            not_found = f"delete-not-found:{asset_name}"
            if not_found in state.faults:
                state.faults.remove(not_found)
                self._respond_bytes(404, b"")
                return
            self._respond_bytes(204, b"")
            return
        self._respond_json(404, {"message": "not found"})

    def do_PATCH(self) -> None:
        parsed = self._record()
        state = self.server.state
        if parsed.path.endswith("/releases/1"):
            payload = self._json_body()
            assert state.release is not None
            state.mutations["publish"] = state.mutations.get("publish", 0) + 1
            state.release["draft"] = payload["draft"]
            if "publish-response-loss" in state.faults:
                state.faults.remove("publish-response-loss")
                self._lose_response()
                return
            self._respond_json(200, self._release_payload())
            return
        self._respond_json(404, {"message": "not found"})


@dataclass(frozen=True)
class RunningFakeApi:
    state: FakeApiState
    base_url: str


@pytest.fixture
def fake_api() -> Iterator[RunningFakeApi]:
    state = FakeApiState()
    server = FakeHttpServer(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    try:
        yield RunningFakeApi(state, f"http://{host}:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _client() -> ApiClient:
    return ApiClient(timeout=2, read_attempts=2, retry_delay=0, sleeper=lambda _: None)


def _artifact_zip(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _artifact_record(payload: bytes, *, artifact_id: int = 7) -> dict[str, object]:
    return {
        "id": artifact_id,
        "name": _ARTIFACT_NAME,
        "expired": False,
        "expires_at": "2099-01-01T00:00:00Z",
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "workflow_run": {"id": _RUN_ID, "head_sha": _WORKFLOW_HEAD_SHA},
    }


def _read_output(path: Path) -> dict[str, str]:
    return dict(line.split("=", maxsplit=1) for line in path.read_text().splitlines())


def test_initial_run_without_artifact_selects_one_build(
    fake_api: RunningFakeApi, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    dist = tmp_path / "dist"

    reused = reuse_run_artifact(
        _client(),
        api_base=fake_api.base_url,
        repository=_REPOSITORY,
        run_id=_RUN_ID,
        artifact_name=_ARTIFACT_NAME,
        expected_workflow_head_sha=_WORKFLOW_HEAD_SHA,
        dist_dir=dist,
        github_output=output,
    )

    assert reused is False
    assert not dist.exists()
    assert _read_output(output) == {
        "found": "false",
        "artifact_id": "",
        "artifact_digest": "",
    }


def test_dispatch_rerun_reuses_artifact_for_workflow_head_not_requested_source(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    assert _HEAD_SHA != _WORKFLOW_HEAD_SHA
    archive = _artifact_zip({_WHEEL: b"wheel", _SDIST: b"sdist"})
    fake_api.state.artifact_zip = archive
    fake_api.state.artifact_records = [_artifact_record(archive)]
    output = tmp_path / "output"
    dist = tmp_path / "dist"

    reused = reuse_run_artifact(
        _client(),
        api_base=fake_api.base_url,
        repository=_REPOSITORY,
        run_id=_RUN_ID,
        artifact_name=_ARTIFACT_NAME,
        expected_workflow_head_sha=_WORKFLOW_HEAD_SHA,
        dist_dir=dist,
        github_output=output,
    )

    assert reused is True
    assert {path.name: path.read_bytes() for path in dist.iterdir()} == {
        _WHEEL: b"wheel",
        _SDIST: b"sdist",
    }
    assert _read_output(output)["artifact_id"] == "7"
    assert _read_output(output)["artifact_digest"] == hashlib.sha256(archive).hexdigest()
    assert all(version == "2026-03-10" for _, _, version in fake_api.state.requests)


def _workflow() -> str:
    return (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")


def test_workflow_is_manual_only_with_exact_inputs_and_safe_default() -> None:
    workflow = _workflow()

    trigger = workflow.split("permissions:", maxsplit=1)[0]
    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert re.search(
        r"mode:\n        description:.*\n        required: true\n        type: choice\n"
        r"        options:\n          - testpypi\n          - production\n        default: testpypi",
        trigger,
    )
    assert re.search(
        r"ref:\n        description:.*\n        required: true\n        type: string", trigger
    )
    assert re.search(
        r"release_body:\n        description:.*\n        required: false\n        type: string",
        trigger,
    )


def test_workflow_validates_dispatch_before_checkout_and_uses_master_workflow_ref() -> None:
    workflow = _workflow()

    validation = workflow.index("- name: Validate dispatch and extract the release body")
    checkout = workflow.index("- name: Check out the validated source")
    assert validation < checkout
    assert 'GITHUB_REF") != "refs/heads/master"' in workflow
    assert 'mode == "production"' in workflow
    assert "stable_tag.fullmatch(requested_ref)" in workflow
    assert 'mode == "testpypi"' in workflow
    assert "commit_sha.fullmatch(requested_ref)" in workflow
    assert 'if body != ""' in workflow
    assert "if not body or body.isspace()" in workflow
    assert "ref: ${{ steps.dispatch.outputs.requested_ref }}" in workflow
    assert '--expected-workflow-head-sha "${{ github.sha }}"' in workflow
    assert '--expected-workflow-head-sha "${{ steps.source.outputs.source_sha }}"' not in workflow


def test_workflow_transports_body_only_through_event_file_and_fixed_temp_file() -> None:
    workflow = _workflow()

    assert "${{ inputs.release_body }}" not in workflow
    assert "${{ github.event.inputs.release_body }}" not in workflow
    assert "RELEASE_BODY:" not in workflow
    assert "release_body=" not in workflow
    assert "release-body.md" in workflow
    assert 'Path(os.environ["GITHUB_EVENT_PATH"])' in workflow
    assert ".write_bytes(body_bytes)" in workflow
    assert "Create or resume the exact draft-first GitHub Release" in workflow
    assert '--release-body-file "$RUNNER_TEMP/release-body.md"' in workflow
    assert "::" not in "\n".join(
        line for line in workflow.splitlines() if "release_body" in line.lower()
    )


def _run_dispatch_validation(
    tmp_path: Path,
    inputs: dict[str, str],
    *,
    workflow_ref: str = "refs/heads/master",
) -> subprocess.CompletedProcess[str]:
    workflow = _workflow()
    inline = workflow.split("python - <<'PY'\n", maxsplit=1)[1].split("\n          PY", maxsplit=1)[
        0
    ]
    script = textwrap.dedent(inline)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "GITHUB_REF": workflow_ref,
            "RUNNER_TEMP": str(tmp_path),
        }
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.parametrize(
    ("inputs", "workflow_ref"),
    [
        ({"mode": "production", "ref": _TAG, "release_body": ""}, "refs/heads/master"),
        (
            {"mode": "production", "ref": _TAG, "release_body": " \n\t"},
            "refs/heads/master",
        ),
        (
            {"mode": "production", "ref": "v1.2", "release_body": "notes"},
            "refs/heads/master",
        ),
        (
            {"mode": "testpypi", "ref": _HEAD_SHA, "release_body": "notes"},
            "refs/heads/master",
        ),
        (
            {"mode": "testpypi", "ref": "A" * 40, "release_body": ""},
            "refs/heads/master",
        ),
        (
            {"mode": "testpypi", "ref": _HEAD_SHA, "release_body": ""},
            "refs/heads/topic",
        ),
    ],
)
def test_dispatch_validation_rejects_invalid_mode_ref_body_combinations(
    tmp_path: Path,
    inputs: dict[str, str],
    workflow_ref: str,
) -> None:
    result = _run_dispatch_validation(tmp_path, inputs, workflow_ref=workflow_ref)

    assert result.returncode != 0
    assert not (tmp_path / "release-body.md").exists()


@pytest.mark.parametrize(
    "inputs",
    [
        {"mode": "testpypi", "ref": _HEAD_SHA},
        {"mode": "testpypi", "ref": _HEAD_SHA, "release_body": ""},
        {"mode": "production", "ref": _TAG, "release_body": "\nNotes π\n"},
    ],
)
def test_dispatch_validation_accepts_exact_contract_and_preserves_body(
    tmp_path: Path,
    inputs: dict[str, str],
) -> None:
    result = _run_dispatch_validation(tmp_path, inputs)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "release-body.md").read_bytes() == inputs.get("release_body", "").encode()
    output = _read_output(tmp_path / "output")
    assert output == {"mode": inputs["mode"], "requested_ref": inputs["ref"]}


def test_workflow_bootstraps_annotated_production_source_and_allows_master_to_advance() -> None:
    workflow = _workflow()

    assert '"+refs/tags/$VALIDATED_REF:$tag_ref"' in workflow
    assert '"+refs/heads/master:$master_ref"' in workflow
    assert 'git cat-file -t "$tag_object_sha"' in workflow
    assert '!= "tag"' in workflow
    assert 'git merge-base --is-ancestor "$head_sha" "$master_ref"' in workflow
    assert '"$head_sha" == "$(git rev-parse --verify "$master_ref")"' not in workflow


def test_workflow_publication_conditions_use_only_validated_release_kind() -> None:
    workflow = _workflow()

    assert "if: needs.build.outputs.release_kind == 'testpypi'" in workflow
    assert workflow.count("if: needs.build.outputs.release_kind == 'production'") == 2
    assert "if: github.event_name" not in workflow
    assert "github.ref_type" not in workflow
    assert "needs:\n      - build\n      - publish-pypi" in workflow


def test_workflow_has_two_pinned_publish_uses_and_no_attestation_opt_out() -> None:
    workflow = _workflow()

    publish_use_lines = [line for line in workflow.splitlines() if "gh-action-pypi-publish" in line]
    pinned_use = "uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    assert len(publish_use_lines) == 2
    assert all(line.strip().startswith(pinned_use) for line in publish_use_lines)
    assert "attestations:" not in workflow


@pytest.mark.parametrize(
    "step_name",
    [
        "Require the exact complete TestPyPI release after bounded retries",
        "Require the exact complete PyPI release after bounded retries",
    ],
)
def test_workflow_post_publish_reconciliation_uses_two_minute_retry_window(
    step_name: str,
) -> None:
    workflow = _workflow()
    marker = f"      - name: {step_name}\n"

    assert workflow.count(marker) == 1
    step = workflow.split(marker, maxsplit=1)[1].split("\n      - name:", maxsplit=1)[0]
    assert re.findall(r"--attempts\s+\S+", step) == ["--attempts 13"]
    assert re.findall(r"--retry-delay\s+\S+", step) == ["--retry-delay 10"]


def test_workflow_preserves_environments_oidc_concurrency_and_exact_artifact_reuse() -> None:
    workflow = _workflow()

    assert "group: release-${{ github.workflow }}-${{ inputs.mode }}-${{ inputs.ref }}" in workflow
    concurrency = workflow.split("concurrency:", maxsplit=1)[1].split("env:", maxsplit=1)[0]
    assert "release_body" not in concurrency
    assert "cancel-in-progress: false" in concurrency
    assert "queue: max" in concurrency
    assert "name: testpypi" in workflow
    assert "name: pypi" in workflow
    assert workflow.count("id-token: write") == 2
    assert "contents: write" in workflow
    assert "reuse-run-artifact" in workflow
    assert "artifact-ids: ${{ needs.build.outputs.artifact_id }}" in workflow
    assert "digest-mismatch: error" in workflow
    assert "steps.existing.outputs.found != 'true'" in workflow


@pytest.mark.parametrize("corruption", ["multiple", "digest", "head", "expired"])
def test_run_artifact_reuse_fails_closed_on_ambiguous_or_mismatched_state(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    corruption: str,
) -> None:
    archive = _artifact_zip({_WHEEL: b"wheel", _SDIST: b"sdist"})
    record = _artifact_record(archive)
    if corruption == "multiple":
        fake_api.state.artifact_records = [record, _artifact_record(archive, artifact_id=8)]
    else:
        if corruption == "digest":
            record["digest"] = f"sha256:{'0' * 64}"
        elif corruption == "head":
            record["workflow_run"] = {"id": _RUN_ID, "head_sha": "b" * 40}
        else:
            record["expired"] = True
        fake_api.state.artifact_records = [record]
    fake_api.state.artifact_zip = archive

    with pytest.raises(ReleaseOperationError):
        reuse_run_artifact(
            _client(),
            api_base=fake_api.base_url,
            repository=_REPOSITORY,
            run_id=_RUN_ID,
            artifact_name=_ARTIFACT_NAME,
            expected_workflow_head_sha=_WORKFLOW_HEAD_SHA,
            dist_dir=tmp_path / "dist",
            github_output=tmp_path / "output",
        )


def _write_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / _WHEEL).write_bytes(b"wheel contents")
    (dist / _SDIST).write_bytes(b"sdist contents")
    return dist


def _index_file(path: Path, package_type: str, *, digest: str | None = None) -> dict[str, object]:
    return {
        "filename": path.name,
        "packagetype": package_type,
        "digests": {"sha256": digest or hashlib.sha256(path.read_bytes()).hexdigest()},
    }


def test_missing_index_release_stages_exactly_both_files(
    fake_api: RunningFakeApi, tmp_path: Path
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.index_missing = True
    publish = tmp_path / "publish"

    missing = reconcile_package_index(
        _client(),
        api_base=fake_api.base_url,
        project_name="archicad-mcp",
        version=_VERSION,
        dist_dir=dist,
        wheel_name=_WHEEL,
        sdist_name=_SDIST,
        publish_dir=publish,
        require_complete=False,
    )

    assert {asset.name for asset in missing} == {_WHEEL, _SDIST}
    assert {path.name: path.read_bytes() for path in publish.iterdir()} == {
        _WHEEL: b"wheel contents",
        _SDIST: b"sdist contents",
    }


def test_partial_index_stages_only_the_missing_file(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.index_sequences = [[_index_file(dist / _WHEEL, "bdist_wheel")]]
    publish = tmp_path / "publish"

    missing = reconcile_package_index(
        _client(),
        api_base=fake_api.base_url,
        project_name="archicad-mcp",
        version=_VERSION,
        dist_dir=dist,
        wheel_name=_WHEEL,
        sdist_name=_SDIST,
        publish_dir=publish,
        require_complete=False,
    )

    assert [asset.name for asset in missing] == [_SDIST]
    assert [path.name for path in publish.iterdir()] == [_SDIST]


def test_complete_index_needs_no_publish_directory(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.index_sequences = [
        [
            _index_file(dist / _WHEEL, "bdist_wheel"),
            _index_file(dist / _SDIST, "sdist"),
        ]
    ]
    publish = tmp_path / "publish"

    missing = reconcile_package_index(
        _client(),
        api_base=fake_api.base_url,
        project_name="archicad-mcp",
        version=_VERSION,
        dist_dir=dist,
        wheel_name=_WHEEL,
        sdist_name=_SDIST,
        publish_dir=publish,
        require_complete=False,
    )

    assert missing == ()
    assert not publish.exists()


@pytest.mark.parametrize("mismatch", ["digest", "unexpected", "type"])
def test_index_mismatch_fails_closed_without_retrying(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    mismatch: str,
) -> None:
    dist = _write_dist(tmp_path)
    record = _index_file(dist / _WHEEL, "bdist_wheel")
    if mismatch == "digest":
        record["digests"] = {"sha256": "0" * 64}
    elif mismatch == "unexpected":
        record["filename"] = "unexpected.whl"
    else:
        record["packagetype"] = "sdist"
    fake_api.state.index_sequences = [[record]]
    sleeps: list[float] = []

    with pytest.raises(ReleaseOperationError):
        reconcile_package_index(
            _client(),
            api_base=fake_api.base_url,
            project_name="archicad-mcp",
            version=_VERSION,
            dist_dir=dist,
            wheel_name=_WHEEL,
            sdist_name=_SDIST,
            publish_dir=None,
            require_complete=True,
            attempts=13,
            retry_delay=10,
            sleeper=sleeps.append,
        )

    assert fake_api.state.index_reads == 1
    assert sleeps == []


def test_post_publish_reconciliation_retries_partial_until_complete(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    wheel = _index_file(dist / _WHEEL, "bdist_wheel")
    sdist = _index_file(dist / _SDIST, "sdist")
    fake_api.state.index_sequences = [[wheel]] * 12 + [[wheel, sdist]]
    sleeps: list[float] = []

    missing = reconcile_package_index(
        _client(),
        api_base=fake_api.base_url,
        project_name="archicad-mcp",
        version=_VERSION,
        dist_dir=dist,
        wheel_name=_WHEEL,
        sdist_name=_SDIST,
        publish_dir=None,
        require_complete=True,
        attempts=13,
        retry_delay=10,
        sleeper=sleeps.append,
    )

    assert missing == ()
    assert fake_api.state.index_reads == 13
    assert sleeps == [10] * 12


def test_post_publish_partial_state_fails_after_bounded_retries(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.index_sequences = [[_index_file(dist / _WHEEL, "bdist_wheel")]]
    sleeps: list[float] = []

    with pytest.raises(ReleaseOperationError, match="did not converge") as error:
        reconcile_package_index(
            _client(),
            api_base=fake_api.base_url,
            project_name="archicad-mcp",
            version=_VERSION,
            dist_dir=dist,
            wheel_name=_WHEEL,
            sdist_name=_SDIST,
            publish_dir=None,
            require_complete=True,
            attempts=13,
            retry_delay=10,
            sleeper=sleeps.append,
        )

    assert f"missing ['{_SDIST}']" in str(error.value)
    assert fake_api.state.index_reads == 13
    assert sleeps == [10] * 12


def _release_asset(path: Path, asset_id: int) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "id": asset_id,
        "name": path.name,
        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "state": "uploaded",
        "size": len(payload),
    }


def _starter_asset(path: Path, asset_id: int) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": path.name,
        "digest": None,
        "state": "starter",
        "size": 0,
    }


def _release_body(tmp_path: Path, content: str = "# Complete release body\n") -> Path:
    body = tmp_path / "release-body.md"
    body.write_bytes(content.encode("utf-8"))
    return body


def _transact(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    dist: Path,
    *,
    release_body_file: Path | None = None,
) -> str:
    return transact_github_release(
        _client(),
        api_base=fake_api.base_url,
        uploads_base=fake_api.base_url,
        repository=_REPOSITORY,
        tag=_TAG,
        source_sha=_HEAD_SHA,
        dist_dir=dist,
        wheel_name=_WHEEL,
        sdist_name=_SDIST,
        release_body_file=release_body_file or _release_body(tmp_path),
        observe_attempts=2,
        observe_delay=0,
        sleeper=lambda _: None,
    )


def _reset_fake_history(fake_api: RunningFakeApi) -> None:
    fake_api.state.mutations.clear()
    fake_api.state.requests.clear()


def _seed_exact_release(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    dist: Path,
    *,
    draft: bool,
) -> None:
    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.release is not None
    fake_api.state.release["draft"] = draft
    _reset_fake_history(fake_api)


def _marker_payload(release: dict[str, object]) -> dict[str, object]:
    body = cast(str, release["body"])
    prefix = "<!-- archicad-mcp-release-transaction:v1:"
    marker_start = body.rfind(prefix)
    assert marker_start >= 0 and body.endswith(" -->")
    marker = json.loads(bytes.fromhex(body[marker_start + len(prefix) : -4]).decode("utf-8"))
    assert isinstance(marker, dict)
    return cast(dict[str, object], marker)


def _rewrite_marker(release: dict[str, object], **updates: object) -> None:
    body = cast(str, release["body"])
    prefix = "<!-- archicad-mcp-release-transaction:v1:"
    marker_start = body.rfind(prefix)
    marker = _marker_payload(release)
    marker.update(updates)
    encoded = json.dumps(marker, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    release["body"] = f"{body[:marker_start]}{prefix}{encoded.encode().hex()} -->"


def test_github_release_uses_exact_body_deterministic_title_and_marker_v1(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    supplied_bytes = "\n## Complete body\n\nUnicode:\tÁrvíztűrő tükörfúrógép\n".encode()
    body_file = tmp_path / "supplied.md"
    body_file.write_bytes(supplied_bytes)

    outcome = _transact(fake_api, tmp_path, dist, release_body_file=body_file)

    assert outcome == "published"
    assert fake_api.state.release is not None
    release = fake_api.state.release
    assert release["draft"] is False
    assert release["name"] == "archicad-mcp v1.2.3"
    stored_body = cast(str, release["body"])
    marker_start = stored_body.index("<!-- archicad-mcp-release-transaction:v1:")
    assert stored_body[:marker_start].encode() == supplied_bytes + b"\n\n"
    assert stored_body.count("<!-- archicad-mcp-release-transaction:v1:") == 1
    marker = _marker_payload(release)
    assert marker["schema"] == 1
    assert marker["tag"] == _TAG
    assert marker["source_sha"] == _HEAD_SHA
    assert marker["curated_sha256"] == hashlib.sha256(supplied_bytes).hexdigest()
    assert marker["generated_body_sha256"] == hashlib.sha256(b"").hexdigest()
    assert marker["generated_title_sha256"] == hashlib.sha256(b"archicad-mcp v1.2.3").hexdigest()
    assert marker["assets"] == [
        {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
        for path in sorted(dist.iterdir(), key=lambda candidate: candidate.name)
    ]
    assert fake_api.state.create_payload is not None
    assert fake_api.state.create_payload["generate_release_notes"] is False
    assert not any("generate-notes" in path for _, path, _ in fake_api.state.requests)
    assert {asset["name"] for asset in fake_api.state.assets} == {_WHEEL, _SDIST}
    assert fake_api.state.mutations == {
        "create": 1,
        f"upload:{_WHEEL}": 1,
        f"upload:{_SDIST}": 1,
        "publish": 1,
    }
    assert all(version == "2026-03-10" for _, _, version in fake_api.state.requests)


@pytest.mark.parametrize("content", [b"", b" \n\t"])
def test_release_body_rejects_empty_or_whitespace_only(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    content: bytes,
) -> None:
    dist = _write_dist(tmp_path)
    body_file = tmp_path / "body.md"
    body_file.write_bytes(content)

    with pytest.raises(ReleaseOperationError, match=r"empty|whitespace"):
        _transact(fake_api, tmp_path, dist, release_body_file=body_file)
    assert fake_api.state.mutations == {}


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x0b", "\r", "\x7f"])
def test_release_body_rejects_unsupported_controls(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    control: str,
) -> None:
    dist = _write_dist(tmp_path)
    body_file = _release_body(tmp_path, f"valid{control}text")

    with pytest.raises(ReleaseOperationError, match="control"):
        _transact(fake_api, tmp_path, dist, release_body_file=body_file)
    assert fake_api.state.mutations == {}


def test_release_body_rejects_reserved_marker_invalid_utf8_and_oversize(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    body_file = tmp_path / "body.md"
    cases = [
        b"text <!-- archicad-mcp-release-transaction: reserved",
        b"invalid \xed\xa0\x80 surrogate",
        b"x" * (60 * 1024 + 1),
    ]

    for content in cases:
        body_file.write_bytes(content)
        with pytest.raises(ReleaseOperationError):
            _transact(fake_api, tmp_path, dist, release_body_file=body_file)
    assert fake_api.state.mutations == {}


def test_release_body_rejects_symlink(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    target = _release_body(tmp_path)
    link = tmp_path / "body-link.md"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ReleaseOperationError, match="non-symlink"):
        _transact(fake_api, tmp_path, dist, release_body_file=link)
    assert fake_api.state.mutations == {}


def test_github_release_create_response_loss_is_observed_and_resumed(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.faults.add("create-response-loss")

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.mutations["create"] == 1


def test_create_response_loss_rerun_reuses_exact_supplied_content(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    fake_api.state.faults.add("create-response-loss-unobservable")

    with pytest.raises(AmbiguousMutationError, match="creation was not observable"):
        _transact(fake_api, tmp_path, dist)
    assert fake_api.state.release is not None
    original_name = fake_api.state.release["name"]
    original_body = fake_api.state.release["body"]

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.release["name"] == original_name
    assert fake_api.state.release["body"] == original_body
    assert fake_api.state.mutations["create"] == 1


def test_github_release_one_asset_response_loss_is_observed_without_reupload(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    fake_api.state.assets = [_release_asset(dist / _WHEEL, 10)]
    fake_api.state.faults.add(f"upload-response-loss:{_SDIST}")

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.mutations.get(f"upload:{_WHEEL}", 0) == 0
    assert fake_api.state.mutations[f"upload:{_SDIST}"] == 1


def test_github_release_publish_response_loss_is_observed_as_success(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    fake_api.state.faults.add("publish-response-loss")

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.mutations["publish"] == 1


def test_existing_exact_draft_resumes_without_changing_title_or_body(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    assert fake_api.state.release is not None
    original_name = fake_api.state.release["name"]
    original_body = fake_api.state.release["body"]
    fake_api.state.assets = [_release_asset(dist / _WHEEL, 10)]

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.release["name"] == original_name
    assert fake_api.state.release["body"] == original_body


def test_exact_published_github_release_is_idempotent_success(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=False)

    assert _transact(fake_api, tmp_path, dist) == "already-published"
    assert fake_api.state.mutations == {}


@pytest.mark.parametrize(
    "mismatch",
    [
        "absent",
        "malformed",
        "duplicate",
        "end-not-marker",
        "source",
        "curated",
        "asset",
        "title",
        "generated-title",
        "generated-body",
        "body",
    ],
)
def test_existing_release_marker_mismatch_fails_without_mutation(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    mismatch: str,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(
        fake_api,
        tmp_path,
        dist,
        draft=mismatch in {"absent", "duplicate", "source", "asset", "body"},
    )
    assert fake_api.state.release is not None
    release = fake_api.state.release
    body = cast(str, release["body"])
    prefix = "<!-- archicad-mcp-release-transaction:v1:"
    marker_start = body.rfind(prefix)
    assert marker_start >= 0
    if mismatch == "absent":
        release["body"] = body[:marker_start].removesuffix("\n\n")
    elif mismatch == "malformed":
        release["body"] = body[: marker_start + len(prefix)] + "zz -->"
    elif mismatch == "duplicate":
        release["body"] = f"{body}\n\n{body[marker_start:]}"
    elif mismatch == "end-not-marker":
        release["body"] = f"{body}\n"
    elif mismatch == "source":
        _rewrite_marker(release, source_sha="b" * 40)
    elif mismatch == "curated":
        _rewrite_marker(release, curated_sha256="0" * 64)
    elif mismatch == "asset":
        marker_hex = body[marker_start + len(prefix) : -4]
        marker = json.loads(bytes.fromhex(marker_hex).decode("utf-8"))
        assert isinstance(marker, dict)
        assets = marker["assets"]
        assert isinstance(assets, list) and isinstance(assets[0], dict)
        assets[0]["sha256"] = "0" * 64
        _rewrite_marker(release, assets=assets)
    elif mismatch == "title":
        release["name"] = "tampered title"
    elif mismatch == "generated-title":
        _rewrite_marker(release, generated_title_sha256="0" * 64)
    elif mismatch == "generated-body":
        _rewrite_marker(release, generated_body_sha256="0" * 64)
    else:
        release["body"] = f"{body[:marker_start]}tampered\n\n{body[marker_start:]}"

    with pytest.raises(ReleaseOperationError, match=r"marker|body|title"):
        _transact(fake_api, tmp_path, dist)
    assert fake_api.state.mutations == {}


def test_changed_release_body_rejects_existing_transaction_without_mutation(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    changed_body = _release_body(tmp_path, "# Changed complete release body\n")

    with pytest.raises(ReleaseOperationError, match="release body"):
        _transact(fake_api, tmp_path, dist, release_body_file=changed_body)
    assert fake_api.state.mutations == {}


def test_changed_asset_rejects_existing_transaction_marker_without_mutation(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    (dist / _WHEEL).write_bytes(b"changed wheel contents")

    with pytest.raises(ReleaseOperationError, match="transaction marker"):
        _transact(fake_api, tmp_path, dist)
    assert fake_api.state.mutations == {}


@pytest.mark.parametrize("fault", [None, "response-loss", "not-found"])
def test_draft_starter_asset_is_deleted_observed_and_reuploaded(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    fault: str | None,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    fake_api.state.assets = [
        _starter_asset(dist / _WHEEL, 10),
        _release_asset(dist / _SDIST, 11),
    ]
    if fault is not None:
        fake_api.state.faults.add(f"delete-{fault}:{_WHEEL}")

    assert _transact(fake_api, tmp_path, dist) == "published"
    assert fake_api.state.mutations[f"delete:{_WHEEL}"] == 1
    assert fake_api.state.mutations[f"upload:{_WHEEL}"] == 1
    assert all(version == "2026-03-10" for _, _, version in fake_api.state.requests)


def test_starter_deletion_must_be_observed_absent_before_upload(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    fake_api.state.assets = [
        _starter_asset(dist / _WHEEL, 10),
        _release_asset(dist / _SDIST, 11),
    ]
    fake_api.state.faults.add(f"delete-still-present:{_WHEEL}")

    with pytest.raises(ReleaseOperationError, match="remains observable"):
        _transact(fake_api, tmp_path, dist)
    assert fake_api.state.mutations[f"delete:{_WHEEL}"] == 1
    assert fake_api.state.mutations.get(f"upload:{_WHEEL}", 0) == 0


@pytest.mark.parametrize(
    "malformation",
    [
        "nonzero",
        "digest",
        "unexpected",
        "duplicate",
        "duplicate-id",
        "state",
        "digest-type",
        "digest-missing",
    ],
)
def test_malformed_or_ambiguous_starter_assets_are_never_deleted(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    malformation: str,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=True)
    starter = _starter_asset(dist / _WHEEL, 20)
    if malformation == "nonzero":
        starter["size"] = 1
        fake_api.state.assets[0] = starter
    elif malformation == "digest":
        starter["digest"] = f"sha256:{'0' * 64}"
        fake_api.state.assets[0] = starter
    elif malformation == "unexpected":
        starter["name"] = "unexpected.whl"
        fake_api.state.assets[0] = starter
    elif malformation == "duplicate":
        fake_api.state.assets.append(starter)
    elif malformation == "duplicate-id":
        starter["id"] = fake_api.state.assets[1]["id"]
        fake_api.state.assets[0] = starter
    elif malformation == "state":
        starter["state"] = "processing"
        fake_api.state.assets[0] = starter
    elif malformation == "digest-type":
        starter["digest"] = 7
        fake_api.state.assets[0] = starter
    else:
        del starter["digest"]
        fake_api.state.assets[0] = starter

    with pytest.raises(ReleaseOperationError):
        _transact(fake_api, tmp_path, dist)
    assert not any(key.startswith("delete:") for key in fake_api.state.mutations)


def test_published_starter_asset_is_never_deleted(
    fake_api: RunningFakeApi,
    tmp_path: Path,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=False)
    fake_api.state.assets[0] = _starter_asset(dist / _WHEEL, 20)

    with pytest.raises(ReleaseOperationError, match="starter"):
        _transact(fake_api, tmp_path, dist)
    assert not any(key.startswith("delete:") for key in fake_api.state.mutations)


@pytest.mark.parametrize("mismatch", ["target", "digest", "extra", "partial-published"])
def test_github_release_mismatch_or_partial_published_state_fails_closed(
    fake_api: RunningFakeApi,
    tmp_path: Path,
    mismatch: str,
) -> None:
    dist = _write_dist(tmp_path)
    _seed_exact_release(fake_api, tmp_path, dist, draft=mismatch != "partial-published")
    assert fake_api.state.release is not None
    if mismatch == "target":
        fake_api.state.release["target_commitish"] = "b" * 40
    elif mismatch == "digest":
        fake_api.state.assets[0]["digest"] = f"sha256:{'0' * 64}"
    elif mismatch == "extra":
        fake_api.state.assets.append(
            {
                "id": 12,
                "name": "extra.txt",
                "digest": f"sha256:{'0' * 64}",
                "state": "uploaded",
                "size": 0,
            }
        )
    else:
        fake_api.state.assets.pop()

    with pytest.raises(ReleaseOperationError):
        _transact(fake_api, tmp_path, dist)
    assert fake_api.state.mutations == {}


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class GitFixture:
    remote: Path
    author: Path
    consumer: Path
    source_sha: str


def _git_fixture(tmp_path: Path) -> GitFixture:
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    consumer = tmp_path / "consumer"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "clone", str(remote), str(author))
    _git(author, "config", "user.name", "Release Test")
    _git(author, "config", "user.email", "release-test@example.invalid")
    (author / "source.txt").write_text("first\n", encoding="utf-8")
    _git(author, "add", "source.txt")
    _git(author, "commit", "-m", "initial")
    _git(author, "branch", "-M", "master")
    _git(author, "push", "origin", "master")
    source_sha = _git(author, "rev-parse", "HEAD")
    _git(tmp_path, "clone", "--branch", "master", str(remote), str(consumer))
    return GitFixture(remote, author, consumer, source_sha)


def test_remote_source_accepts_annotated_tag(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "tag", "-a", _TAG, "-m", "release")
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")

    result = verify_remote_source(
        fixture.consumer,
        tag=_TAG,
        source_sha=fixture.source_sha,
    )

    assert result.source_sha == fixture.source_sha
    assert result.tag_object_type == "tag"


def test_remote_source_rejects_lightweight_tag(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "tag", _TAG)
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")

    with pytest.raises(ReleaseOperationError, match="annotated"):
        verify_remote_source(
            fixture.consumer,
            tag=_TAG,
            source_sha=fixture.source_sha,
        )


def test_remote_source_allows_master_to_advance(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "tag", "-a", _TAG, "-m", "release")
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")
    (fixture.author / "source.txt").write_text("second\n", encoding="utf-8")
    _git(fixture.author, "commit", "-am", "advance master")
    advanced_master = _git(fixture.author, "rev-parse", "HEAD")
    _git(fixture.author, "push", "origin", "master")

    result = verify_remote_source(
        fixture.consumer,
        tag=_TAG,
        source_sha=fixture.source_sha,
    )

    assert result.source_sha == fixture.source_sha
    assert result.master_sha == advanced_master
    assert result.master_sha != result.source_sha


def test_remote_source_rejects_moved_tag_object(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "tag", "-a", _TAG, "-m", "first annotation")
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")
    initial = verify_remote_source(
        fixture.consumer,
        tag=_TAG,
        source_sha=fixture.source_sha,
    )
    _git(fixture.author, "tag", "-f", "-a", _TAG, "-m", "replacement", fixture.source_sha)
    _git(fixture.author, "push", "--force", "origin", f"refs/tags/{_TAG}")

    with pytest.raises(ReleaseOperationError, match="changed after the build gate"):
        verify_remote_source(
            fixture.consumer,
            tag=_TAG,
            source_sha=fixture.source_sha,
            expected_tag_object_sha=initial.tag_object_sha,
        )


def test_remote_source_rejects_deleted_tag(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "tag", "-a", _TAG, "-m", "release")
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")
    _git(fixture.author, "push", "origin", f":refs/tags/{_TAG}")

    with pytest.raises(ReleaseOperationError, match="git fetch"):
        verify_remote_source(
            fixture.consumer,
            tag=_TAG,
            source_sha=fixture.source_sha,
        )


def test_remote_source_rejects_tagged_commit_off_master(tmp_path: Path) -> None:
    fixture = _git_fixture(tmp_path)
    _git(fixture.author, "switch", "-c", "side")
    (fixture.author / "source.txt").write_text("side\n", encoding="utf-8")
    _git(fixture.author, "commit", "-am", "side commit")
    side_sha = _git(fixture.author, "rev-parse", "HEAD")
    _git(fixture.author, "tag", "-a", _TAG, "-m", "release")
    _git(fixture.author, "push", "origin", f"refs/tags/{_TAG}")
    _git(fixture.consumer, "fetch", "origin", f"refs/tags/{_TAG}")
    _git(fixture.consumer, "checkout", "--detach", side_sha)

    with pytest.raises(ReleaseOperationError, match="not reachable"):
        verify_remote_source(
            fixture.consumer,
            tag=_TAG,
            source_sha=side_sha,
        )
