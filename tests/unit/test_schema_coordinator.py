"""Coordinator integration tests for direct startup/cache/update behavior."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest

import archicad_mcp.server as server_module
from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.manager import ConnectionManager
from archicad_mcp.schemas.cache_store import (
    atomically_replace,
    read_cached_snapshot,
    snapshot_path,
)
from archicad_mcp.schemas.registry import CapabilityView, ProviderSnapshot, ViewStatus
from archicad_mcp.schemas.updater import PackagedTapir, load_packaged_tapir
from tests.unit.test_direct_updater import (
    FakeGitHub,
    cache_document_bytes,
    routes_for,
)

ACCEPTED_VERSION = "1.9.0"


@pytest.fixture()
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "cache-root"
    root.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(root))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    monkeypatch.delenv("ARCHICAD_MCP_AUTO_UPDATE", raising=False)
    return root


@pytest.fixture(scope="module")
def packaged() -> PackagedTapir:
    return load_packaged_tapir()


def make_manager(session: aiohttp.ClientSession) -> ConnectionManager:
    return ConnectionManager(session)


def add_connection(
    manager: ConnectionManager,
    port: int,
    *,
    version: str,
    tapir_available: bool | None,
) -> None:
    manager.connections[port] = ArchicadConnection(
        port,
        manager.session,
        {
            "version": version,
            "projectName": f"Project {port}",
            "tapirAvailable": tapir_available,
        },
    )


def build_view(
    manager: ConnectionManager,
    native_snapshot: ProviderSnapshot,
    tapir_snapshot: ProviderSnapshot,
) -> CapabilityView:
    return server_module._build_capability_view(manager, native_snapshot, tapir_snapshot)


def make_coordinator(
    session: aiohttp.ClientSession,
    packaged: PackagedTapir,
    *,
    manager: ConnectionManager | None = None,
    publish: Callable[[CapabilityView], None] | None = None,
) -> server_module.SchemaCoordinator:
    active_manager = manager if manager is not None else ConnectionManager(session)
    native_snapshot = server_module._load_native_snapshot()
    tapir_snapshot, _error = server_module.select_active_tapir_snapshot(packaged)
    view = build_view(active_manager, native_snapshot, tapir_snapshot)
    return server_module.SchemaCoordinator(
        active_manager,
        session,
        packaged,
        native_snapshot,
        tapir_snapshot,
        view,
        publish if publish is not None else (lambda _view: None),
    )


async def noop_refresh() -> None:
    """Keep coordinator refreshes hermetic without live port scanning."""
    return None


def test_select_active_tapir_snapshot_prefers_newer_valid_cache(
    cache_env: Path, packaged: PackagedTapir
) -> None:
    payload, _inputs = cache_document_bytes(version=ACCEPTED_VERSION)
    atomically_replace(snapshot_path(), payload)

    selected, error = server_module.select_active_tapir_snapshot(packaged)

    assert error is None
    assert selected.provider_version == ACCEPTED_VERSION
    assert selected.source_sha256 != packaged.snapshot.source_sha256


def test_select_active_tapir_snapshot_package_wins_equal_and_corrupt_ties(
    cache_env: Path, packaged: PackagedTapir
) -> None:
    payload, _inputs = cache_document_bytes(version=packaged.identity.version)
    atomically_replace(snapshot_path(), payload)
    selected, error = server_module.select_active_tapir_snapshot(packaged)
    assert selected is packaged.snapshot
    assert error is None

    atomically_replace(snapshot_path(), b"{corrupt garbage")
    fallback, fallback_error = server_module.select_active_tapir_snapshot(packaged)
    assert fallback is packaged.snapshot
    assert fallback_error == "malformed_tapir_json"


def test_select_active_tapir_snapshot_without_cache_serves_packaged(
    cache_env: Path, packaged: PackagedTapir
) -> None:
    selected, error = server_module.select_active_tapir_snapshot(packaged)
    assert selected is packaged.snapshot
    assert error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offline", "auto", "scheduled"),
    [
        pytest.param(None, None, True, id="default-on"),
        pytest.param(None, "1", True, id="auto-explicit-on"),
        pytest.param(None, "0", False, id="auto-off"),
        pytest.param("1", None, False, id="offline-overrides-auto"),
        pytest.param("1", "0", False, id="offline-with-auto-off"),
        pytest.param("0", None, True, id="offline-disabled-is-automatic"),
        pytest.param("yes", None, False, id="invalid-offline"),
        pytest.param(None, "true", False, id="invalid-auto"),
    ],
)
async def test_startup_scheduling_precedence_is_strict(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
    offline: str | None,
    auto: str | None,
    scheduled: bool,
) -> None:
    if offline is None:
        monkeypatch.delenv("ARCHICAD_MCP_OFFLINE", raising=False)
    else:
        monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", offline)
    if auto is None:
        monkeypatch.delenv("ARCHICAD_MCP_AUTO_UPDATE", raising=False)
    else:
        monkeypatch.setenv("ARCHICAD_MCP_AUTO_UPDATE", auto)

    calls: list[str] = []

    async def recorded_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del kwargs
        calls.append("check")

    monkeypatch.setattr(server_module, "run_update_check", recorded_update)
    async with aiohttp.ClientSession() as session:
        coordinator = make_coordinator(session, packaged)
        coordinator.schedule_startup_update()
        task = coordinator.update_task
        assert (task is not None) is scheduled
        if task is not None:
            await asyncio.wait_for(task, timeout=5.0)
        await coordinator.close()
    assert len(calls) == (1 if scheduled else 0)


@pytest.mark.asyncio
async def test_startup_update_runs_once_and_refresh_never_triggers_another(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archicad_mcp.schemas import updater as upd_module

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []
    fake_fetch = FakeGitHub(routes_for(ACCEPTED_VERSION))

    async def gated_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del session
        calls.append(1)
        started.set()
        await asyncio.wait_for(release.wait(), timeout=5.0)
        return await upd_module.run_update_check(
            packaged_tapir,
            fetch=fake_fetch,
            on_accepted=kwargs.get("on_accepted"),
        )

    monkeypatch.setattr(server_module, "run_update_check", gated_update)
    async with aiohttp.ClientSession() as session:
        manager = make_manager(session)
        add_connection(manager, 19723, version="29.0.0", tapir_available=True)
        manager.refresh = noop_refresh  # type: ignore[method-assign]
        published: list[CapabilityView] = []
        coordinator = make_coordinator(session, packaged, manager=manager, publish=published.append)
        initial_revision = coordinator.view.revision
        coordinator.schedule_startup_update()
        assert coordinator.update_task is not None
        await asyncio.wait_for(started.wait(), timeout=5.0)

        await coordinator.refresh()
        await coordinator.refresh()
        coordinator.schedule_startup_update()
        assert len(calls) == 1

        release.set()
        await asyncio.wait_for(coordinator.update_task, timeout=10.0)
        assert len(calls) == 1
        assert coordinator.view.revision != initial_revision
        # Identical-state refreshes do not republish; only the accepted update does.
        assert len(published) == 1
        final = published[0]
        assert final.get("tapir:GetAddOnVersion") is not None
        assert final.get("tapir:CreateSlabs") is None
        assert final.get("native:API.GetAllElements") is not None
        assert coordinator.view.status is ViewStatus.TAPIR_AVAILABLE
        await coordinator.close()


@pytest.mark.asyncio
async def test_close_drains_update_task_and_blocks_late_view_or_cache_mutation(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archicad_mcp.schemas import updater as upd_module

    fetch_entered = asyncio.Event()
    release_fetch = asyncio.Event()
    calls: list[int] = []

    async def hanging_fetch(url: str, maximum: int) -> Any:
        del url, maximum
        fetch_entered.set()
        await asyncio.wait_for(release_fetch.wait(), timeout=10.0)
        raise AssertionError("fetch must be cancelled, not completed")

    async def gated_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del session
        calls.append(1)
        return await upd_module.run_update_check(
            packaged_tapir,
            fetch=hanging_fetch,
            on_accepted=kwargs.get("on_accepted"),
        )

    monkeypatch.setattr(server_module, "run_update_check", gated_update)
    async with aiohttp.ClientSession() as session:
        published: list[CapabilityView] = []
        coordinator = make_coordinator(session, packaged, publish=published.append)
        initial_revision = coordinator.view.revision
        coordinator.schedule_startup_update()
        assert coordinator.update_task is not None
        await asyncio.wait_for(fetch_entered.wait(), timeout=5.0)

        await coordinator.close()
        assert coordinator.update_task is None
        assert coordinator._closing is True
        assert coordinator._closed is True
        assert len(calls) == 1
        assert published == []
        assert coordinator.view.revision == initial_revision
        assert not snapshot_path().exists()

        # A late fetch completion after close must not mutate anything either.
        release_fetch.set()
        await asyncio.sleep(0.05)
        assert published == []
        assert coordinator.view.revision == initial_revision
        assert not snapshot_path().exists()

        coordinator.schedule_startup_update()
        assert coordinator.update_task is None


@pytest.mark.asyncio
async def test_close_contains_completed_updater_failure_and_reaches_terminal_state(
    cache_env: Path,
    packaged: PackagedTapir,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken_owned_task() -> None:
        raise RuntimeError("probe-updater-failure")

    async with aiohttp.ClientSession() as session:
        coordinator = make_coordinator(session, packaged)
        owned = asyncio.create_task(broken_owned_task())
        coordinator._update_task = owned
        for _ in range(20):
            if owned.done():
                break
            await asyncio.sleep(0)
        assert owned.done()

        await coordinator.close()
        assert coordinator.update_task is None
        assert coordinator._closed is True
        assert "Schema update task stopped during close (RuntimeError)" in caplog.text

        await coordinator.close()
        assert coordinator._closed is True


@pytest.mark.asyncio
async def test_concurrent_close_callers_share_one_drain_and_return_after_owned_terminal(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every close caller parks on one shared drain until the updater is terminal."""

    started = asyncio.Event()
    cleanup_released = asyncio.Event()
    order: list[str] = []

    async def gated_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del packaged_tapir, session, kwargs
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Product-owned shutdown work deliberately outlives close entry.
            await asyncio.wait_for(cleanup_released.wait(), timeout=10.0)
            order.append("cleanup:done")
            raise

    monkeypatch.setattr(server_module, "run_update_check", gated_update)
    async with aiohttp.ClientSession() as session:
        published: list[CapabilityView] = []
        coordinator = make_coordinator(session, packaged, publish=published.append)
        coordinator.schedule_startup_update()
        owned = coordinator.update_task
        assert owned is not None
        await asyncio.wait_for(started.wait(), timeout=5.0)

        async def watch_owned() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await owned
            order.append("owned:terminal")

        watcher = asyncio.create_task(watch_owned())

        async def tracked_close(tag: str) -> None:
            await coordinator.close()
            order.append(f"{tag}:returned")

        first = asyncio.create_task(tracked_close("first"))
        second = asyncio.create_task(tracked_close("second"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not first.done(), "close returned while product-owned work was still live"
        assert not second.done(), "concurrent close returned while product-owned work was live"
        assert not owned.done()
        assert order == []

        cleanup_released.set()
        await asyncio.wait_for(first, timeout=10.0)
        await asyncio.wait_for(second, timeout=10.0)
        await asyncio.wait_for(watcher, timeout=10.0)

        assert order[:2] == ["cleanup:done", "owned:terminal"]
        assert sorted(order[2:]) == ["first:returned", "second:returned"]
        assert owned.cancelled() is True
        assert published == []
        assert coordinator.update_task is None
        assert coordinator._closed is True


@pytest.mark.asyncio
async def test_cancelled_close_caller_propagates_only_after_shared_drain_finishes(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation is absorbed until the owned updater reaches terminal state."""

    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_released = asyncio.Event()
    cleanup_done = asyncio.Event()
    order: list[str] = []

    async def update_with_gated_cleanup(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del packaged_tapir, session, kwargs
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cleanup_started.set()
            await asyncio.wait_for(cleanup_released.wait(), timeout=10.0)
            cleanup_done.set()
            order.append("cleanup:done")
            raise

    monkeypatch.setattr(server_module, "run_update_check", update_with_gated_cleanup)
    async with aiohttp.ClientSession() as session:
        coordinator = make_coordinator(session, packaged)
        coordinator.schedule_startup_update()
        owned = coordinator.update_task
        assert owned is not None
        await asyncio.wait_for(started.wait(), timeout=5.0)

        async def watch_owned() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await owned
            order.append("owned:terminal")

        watcher = asyncio.create_task(watch_owned())

        async def tracked_close() -> None:
            await coordinator.close()
            order.append("close:returned")

        closer = asyncio.create_task(tracked_close())
        await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
        assert not cleanup_done.is_set()

        closer.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not closer.done()
        closer.cancel()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not closer.done(), "cancellation escaped before the shared drain finished"

        cleanup_released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, timeout=10.0)
        await asyncio.wait_for(watcher, timeout=10.0)
        assert cleanup_done.is_set()

        assert order == ["cleanup:done", "owned:terminal"]
        assert owned.cancelled() is True
        assert coordinator.update_task is None
        assert coordinator._closed is True


@pytest.mark.asyncio
async def test_refresh_during_acquisition_then_update_projects_onto_latest_manager_state(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archicad_mcp.schemas import updater as upd_module

    accepted_ready = asyncio.Event()
    proceed = asyncio.Event()
    fake_fetch = FakeGitHub(routes_for(ACCEPTED_VERSION))

    async def gated_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del session
        outcome = await upd_module.run_update_check(
            packaged_tapir,
            fetch=fake_fetch,
            on_accepted=kwargs.get("on_accepted"),
        )
        accepted_ready.set()
        await asyncio.wait_for(proceed.wait(), timeout=5.0)
        return outcome

    monkeypatch.setattr(server_module, "run_update_check", gated_update)
    async with aiohttp.ClientSession() as session:
        manager = make_manager(session)
        add_connection(manager, 19723, version="29.0.0", tapir_available=True)
        published: list[CapabilityView] = []
        coordinator = make_coordinator(session, packaged, manager=manager, publish=published.append)
        initial_revision = coordinator.view.revision
        assert coordinator.view.status is ViewStatus.TAPIR_AVAILABLE
        assert coordinator.view.target_identity == "29.0.0"
        coordinator.schedule_startup_update()
        await asyncio.wait_for(accepted_ready.wait(), timeout=10.0)

        async def discovery_finds_newer_instance() -> None:
            manager.connections.clear()
            add_connection(manager, 19723, version="30.0.0", tapir_available=True)

        manager.refresh = discovery_finds_newer_instance  # type: ignore[method-assign]

        # A real bounded coordinator refresh lands between acceptance and reprojection.
        instances = await asyncio.wait_for(coordinator.refresh(), timeout=5.0)
        assert [instance.port for instance in instances] == [19723]
        assert coordinator.view.revision != initial_revision
        cached_now = read_cached_snapshot()
        assert cached_now.cached is not None
        assert cached_now.cached.version == ACCEPTED_VERSION, (
            "the accepted snapshot must stay durably cached across the interleaved refresh"
        )
        assert cached_now.error_code is None

        proceed.set()
        assert coordinator.update_task is not None
        await asyncio.wait_for(coordinator.update_task, timeout=10.0)

        final = coordinator.view
        assert final.status is ViewStatus.TAPIR_AVAILABLE
        assert final.target_identity == "30.0.0"
        assert final.get("tapir:GetAddOnVersion") is not None
        assert final.get("tapir:CreateSlabs") is None
        assert coordinator.tapir_snapshot.provider_version == ACCEPTED_VERSION
        assert len(published) == 2
        await coordinator.close()


@pytest.mark.asyncio
async def test_refresh_after_update_retains_and_restores_the_accepted_snapshot(
    cache_env: Path,
    packaged: PackagedTapir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archicad_mcp.schemas import updater as upd_module

    fake_fetch = FakeGitHub(routes_for(ACCEPTED_VERSION))

    async def fetched_update(
        packaged_tapir: PackagedTapir,
        session: aiohttp.ClientSession,
        **kwargs: Any,
    ) -> Any:
        del session
        return await upd_module.run_update_check(
            packaged_tapir,
            fetch=fake_fetch,
            on_accepted=kwargs.get("on_accepted"),
        )

    monkeypatch.setattr(server_module, "run_update_check", fetched_update)
    async with aiohttp.ClientSession() as session:
        manager = make_manager(session)
        published: list[CapabilityView] = []
        coordinator = make_coordinator(session, packaged, manager=manager, publish=published.append)
        stage = "unavailable"

        async def staged_refresh() -> None:
            manager.connections.clear()
            if stage != "gone":
                add_connection(
                    manager,
                    19723,
                    version="29.0.0",
                    tapir_available=(stage == "available"),
                )

        manager.refresh = staged_refresh  # type: ignore[method-assign]
        add_connection(manager, 19723, version="29.0.0", tapir_available=True)
        coordinator.schedule_startup_update()
        assert coordinator.update_task is not None
        await asyncio.wait_for(coordinator.update_task, timeout=10.0)
        assert coordinator.view.get("tapir:GetAddOnVersion") is not None

        stage = "unavailable"
        await coordinator.refresh()
        assert coordinator.view.status is ViewStatus.TAPIR_UNAVAILABLE
        assert coordinator.view.tapir is None
        assert coordinator.tapir_snapshot.provider_version == ACCEPTED_VERSION
        unavailable_revision = coordinator.view.revision

        stage = "available"
        await coordinator.refresh()
        restored = coordinator.view
        assert restored.status is ViewStatus.TAPIR_AVAILABLE
        assert restored.revision != unavailable_revision
        document = restored.get("tapir:GetAddOnVersion")
        assert document is not None
        assert document["provider_version"] == ACCEPTED_VERSION

        stage = "gone"
        await coordinator.refresh()
        final_status: ViewStatus = coordinator.view.status
        assert final_status is ViewStatus.COMPATIBILITY_UNKNOWN
        assert coordinator.tapir_snapshot.provider_version == ACCEPTED_VERSION
        await coordinator.close()


def test_multi_instance_projection_uses_sorted_tapir_representative() -> None:
    packaged = load_packaged_tapir()
    manager = make_manager(cast(Any, object()))
    add_connection(manager, 19723, version="29.0.0", tapir_available=True)
    manager.connections[19724] = ArchicadConnection(
        19724,
        manager.session,
        {
            "version": "31.0.0",
            "projectName": "Project 19724",
            "tapirAvailable": False,
        },
    )

    view = build_view(manager, server_module._load_native_snapshot(), packaged.snapshot)
    assert view.status is ViewStatus.TAPIR_AVAILABLE
    assert view.target_identity == "29.0.0"
