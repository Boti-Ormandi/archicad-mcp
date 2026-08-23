"""Mock tests for ConnectionManager."""

from collections.abc import AsyncIterator, Callable

import aiohttp
import pytest
from aioresponses import aioresponses

from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.errors import ArchicadConnectionError
from archicad_mcp.core.manager import PORT_RANGE, ConnectionManager
from archicad_mcp.schemas.registry import ViewStatus
from archicad_mcp.schemas.updater import load_packaged_tapir
from archicad_mcp.server import (
    _build_capability_view,
    _load_native_snapshot,
    select_active_tapir_snapshot,
)

_PRODUCT_RESULT = {"version": 29, "buildNumber": 4006, "languageCode": "INT"}


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Create aiohttp session for tests."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def manager(session: aiohttp.ClientSession) -> ConnectionManager:
    """Create manager instance for tests."""
    return ConnectionManager(session)


def _product_payload(result: object = _PRODUCT_RESULT) -> dict[str, object]:
    return {"succeeded": True, "result": result}


def _addon_payload(response: dict[str, object]) -> dict[str, object]:
    return {"succeeded": True, "result": {"addOnCommandResponse": response}}


def _tapir_version_payload() -> dict[str, object]:
    return _addon_payload({"version": "1.5.8"})


def _project_payload(name: str = "Test Project") -> dict[str, object]:
    return _addon_payload(
        {
            "isUntitled": False,
            "isTeamwork": False,
            "projectName": name,
            "projectPath": "C:/test.pln",
        }
    )


def _mock_other_ports(mocked: aioresponses, *active: int) -> None:
    for port in PORT_RANGE:
        if port not in active:
            mocked.post(
                f"http://127.0.0.1:{port}",
                exception=aiohttp.ClientError("Connection refused"),
            )


class TestConnectionManagerInit:
    """Tests for manager initialization."""

    def test_starts_with_no_connections(self, manager: ConnectionManager) -> None:
        assert manager.connections == {}


class TestPortScanning:
    """Tests for strict product discovery and independent Tapir probing."""

    async def test_finds_exact_integer_product_response_and_project(
        self, manager: ConnectionManager
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post("http://127.0.0.1:19723", payload=_tapir_version_payload())
            mocked.post("http://127.0.0.1:19723", payload=_project_payload())
            _mock_other_ports(mocked, 19723)

            await manager.scan_and_connect()

        conn = manager.connections[19723]
        assert conn.project_name == "Test Project"
        assert conn.version == "29"
        assert conn._tapir_available is True
        assert conn.tapir_version == "1.5.8"
        instance = manager.get_instances()[0]
        assert instance.archicad_version == "29"
        assert instance.is_tapir_available is True
        assert instance.tapir_version == "1.5.8"

    async def test_handles_no_instances(self, manager: ConnectionManager) -> None:
        with aioresponses() as mocked:
            _mock_other_ports(mocked)
            await manager.scan_and_connect()

        assert manager.connections == {}

    async def test_recognized_missing_addon_is_definitively_unavailable(
        self, manager: ConnectionManager
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": False,
                    "error": {"code": 8000, "message": "AddOn is not registered"},
                },
            )
            _mock_other_ports(mocked, 19723)
            await manager.scan_and_connect()

        conn = manager.connections[19723]
        assert conn.project_name == "Unknown"
        assert conn._tapir_available is False
        assert conn.tapir_version is None

    @pytest.mark.parametrize(
        ("code", "message"),
        [(4010, "AddOn is not registered"), (4999, "Command failed")],
    )
    async def test_structured_addon_command_error_proves_tapir_present(
        self,
        manager: ConnectionManager,
        code: int,
        message: str,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload={
                    "succeeded": False,
                    "error": {"code": code, "message": message},
                },
            )
            mocked.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": False, "error": {"code": 77, "message": "No project"}},
            )
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn._tapir_available is True
        assert conn.tapir_version is None
        assert conn.project_name == "Unknown"
        assert conn.is_teamwork is False

    @pytest.mark.parametrize(
        "register_probe",
        [
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    body="{",
                    content_type="application/json",
                ),
                id="malformed-json",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={"succeeded": True, "result": {}},
                ),
                id="malformed-response-shape",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": True,
                        "result": {"addOnCommandResponse": {"version": "1.5.8"}},
                        "unexpected": True,
                    },
                ),
                id="outer-success-extra-key",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": True,
                        "result": {
                            "addOnCommandResponse": {"version": "1.5.8"},
                            "unexpected": True,
                        },
                    },
                ),
                id="result-extra-key",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": False,
                        "error": {"code": 8000, "message": "AddOn is not registered"},
                        "unexpected": True,
                    },
                ),
                id="outer-failure-extra-key",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={"succeeded": False, "error": {"code": 4010}},
                ),
                id="outer-error-missing-message",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": False,
                        "error": {"code": "4010", "message": "Command failed"},
                    },
                ),
                id="outer-error-wrong-code-type",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": False,
                        "error": {"code": 4010, "message": 7},
                    },
                ),
                id="outer-error-wrong-message-type",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": False,
                        "error": {"code": True, "message": "Command failed"},
                    },
                ),
                id="outer-error-bool-code",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={
                        "succeeded": False,
                        "error": {"code": 4010, "message": "Command failed", "extra": 1},
                    },
                ),
                id="outer-error-extra-field",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload={"succeeded": False, "error": {"arbitrary": "value"}},
                ),
                id="outer-error-arbitrary-dict",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload({"error": {"code": 7}}),
                ),
                id="nested-error-missing-message",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload({"error": {"code": "7", "message": "Command failed"}}),
                ),
                id="nested-error-wrong-type",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload({"error": {"code": True, "message": "Command failed"}}),
                ),
                id="nested-error-bool-code",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload(
                        {"error": {"code": 7, "message": "Command failed", "extra": 1}}
                    ),
                ),
                id="nested-error-extra-field",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload({"arbitrary": {"code": 7, "message": "failed"}}),
                ),
                id="nested-arbitrary-dict",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    payload=_addon_payload(
                        {
                            "success": False,
                            "error": {"code": 7, "message": "failed"},
                            "extra": 1,
                        }
                    ),
                ),
                id="nested-failed-result-extra-field",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    status=503,
                    payload={"error": "unavailable"},
                ),
                id="http-failure",
            ),
            pytest.param(
                lambda mocked: mocked.post(
                    "http://127.0.0.1:19723",
                    exception=aiohttp.ClientError("reset"),
                ),
                id="transport-failure",
            ),
        ],
    )
    async def test_ambiguous_tapir_probe_retains_native_connection_and_unknown_status(
        self,
        manager: ConnectionManager,
        register_probe: Callable[[aioresponses], object],
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            register_probe(mocked)
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn.version == "29"
        assert conn.project_name == "Unknown"
        assert conn._tapir_available is None
        assert conn.tapir_version is None
        assert manager.get(19723) is conn
        assert len(manager.get_instances()) == 1
        packaged = load_packaged_tapir()
        _native = _load_native_snapshot()
        tapir, _error = select_active_tapir_snapshot(packaged)
        assert (
            _build_capability_view(manager, _native, tapir).status
            is ViewStatus.COMPATIBILITY_UNKNOWN
        )

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                {"error": {"code": 7, "message": "Command failed"}},
                id="error-item",
            ),
            pytest.param(
                {"success": False, "error": {"code": 7, "message": "Command failed"}},
                id="failed-execution-result",
            ),
        ],
    )
    async def test_exact_nested_command_error_proves_tapir_present(
        self,
        manager: ConnectionManager,
        response: dict[str, object],
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post("http://127.0.0.1:19723", payload=_addon_payload(response))
            mocked.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": False, "error": {"code": 77, "message": "No project"}},
            )
            await manager._probe_port(19723)

        assert manager.connections[19723]._tapir_available is True

    async def test_project_info_failure_preserves_known_tapir_presence(
        self, manager: ConnectionManager
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post("http://127.0.0.1:19723", payload=_tapir_version_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": True, "result": {"addOnCommandResponse": []}},
            )
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn._tapir_available is True
        assert conn.project_name == "Unknown"
        assert conn.project_path is None
        assert conn.is_teamwork is False

    async def test_failed_product_probe_removes_previous_connection(
        self, manager: ConnectionManager
    ) -> None:
        manager.connections[19723] = ArchicadConnection(
            19723,
            manager.session,
            {"version": "27", "tapirAvailable": True},
        )
        with aioresponses() as mocked:
            mocked.post(
                "http://127.0.0.1:19723",
                payload={"succeeded": False, "error": {"message": "failed"}},
            )
            await manager._probe_port(19723)

        assert 19723 not in manager.connections

    @pytest.mark.parametrize(
        "result",
        [
            {"version": "29", "buildNumber": 4006, "languageCode": "INT"},
            {"version": 29, "languageCode": "INT"},
            {"version": 29, "buildNumber": 4006, "languageCode": "INT", "extra": True},
            [],
        ],
    )
    async def test_malformed_product_probe_removes_previous_connection(
        self,
        manager: ConnectionManager,
        result: object,
    ) -> None:
        manager.connections[19723] = ArchicadConnection(
            19723,
            manager.session,
            {"version": "27", "tapirAvailable": True},
        )
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload(result))
            await manager._probe_port(19723)

        assert 19723 not in manager.connections

    async def test_multiple_instances(self, manager: ConnectionManager) -> None:
        with aioresponses() as mocked:
            for port in (19723, 19724):
                mocked.post(f"http://127.0.0.1:{port}", payload=_product_payload())
                mocked.post(f"http://127.0.0.1:{port}", payload=_tapir_version_payload())
                mocked.post(
                    f"http://127.0.0.1:{port}",
                    payload=_project_payload(f"Project {port}"),
                )
            _mock_other_ports(mocked, 19723, 19724)
            await manager.scan_and_connect()

        assert set(manager.connections) == {19723, 19724}

    async def test_empty_reported_version_stays_unknown(self, manager: ConnectionManager) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload=_addon_payload({"version": ""}),
            )
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn._tapir_available is None
        assert conn.tapir_version is None

    async def test_non_string_reported_version_stays_unknown(
        self, manager: ConnectionManager
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload=_addon_payload({"version": 158}),
            )
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn._tapir_available is None
        assert conn.tapir_version is None

    async def test_extra_fields_in_version_response_stay_unknown_and_versionless(
        self,
        manager: ConnectionManager,
    ) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post(
                "http://127.0.0.1:19723",
                payload=_addon_payload({"version": "1.5.8", "build": 42}),
            )
            await manager._probe_port(19723)

        conn = manager.connections[19723]
        assert conn._tapir_available is None
        assert conn.tapir_version is None

    async def test_instances_on_different_addon_releases_report_differing_versions(
        self, manager: ConnectionManager
    ) -> None:
        with aioresponses() as mocked:
            addon_versions: tuple[tuple[int, dict[str, object]], ...] = (
                (19723, {"version": "1.5.8"}),
                (19724, {"version": "1.4.0"}),
            )
            for port, version in addon_versions:
                mocked.post(f"http://127.0.0.1:{port}", payload=_product_payload())
                mocked.post(f"http://127.0.0.1:{port}", payload=_addon_payload(version))
            _mock_other_ports(mocked, 19723, 19724)
            await manager.scan_and_connect()

        instances = {instance.port: instance for instance in manager.get_instances()}
        assert instances[19723].tapir_version == "1.5.8"
        assert instances[19724].tapir_version == "1.4.0"


class TestGetConnection:
    """Tests for getting connections."""

    async def test_get_existing_connection(self, manager: ConnectionManager) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post("http://127.0.0.1:19723", payload=_tapir_version_payload())
            mocked.post("http://127.0.0.1:19723", payload=_project_payload())
            _mock_other_ports(mocked, 19723)
            await manager.scan_and_connect()

        assert manager.get(19723).port == 19723

    def test_get_nonexistent_raises(self, manager: ConnectionManager) -> None:
        with pytest.raises(ArchicadConnectionError) as exc_info:
            manager.get(19999)

        assert "19999" in str(exc_info.value)
        assert exc_info.value.details["port"] == 19999
        assert "list_instances" in exc_info.value.suggestion


class TestGetInstances:
    """Tests for public instance projection."""

    async def test_returns_archicad_instances(self, manager: ConnectionManager) -> None:
        with aioresponses() as mocked:
            mocked.post("http://127.0.0.1:19723", payload=_product_payload())
            mocked.post("http://127.0.0.1:19723", payload=_tapir_version_payload())
            mocked.post("http://127.0.0.1:19723", payload=_project_payload())
            _mock_other_ports(mocked, 19723)
            await manager.scan_and_connect()

        instances = manager.get_instances()
        assert len(instances) == 1
        instance = instances[0]
        assert instance.port == 19723
        assert instance.project_name == "Test Project"
        assert instance.project_type == "solo"
        assert instance.archicad_version == "29"

    def test_empty_when_no_connections(self, manager: ConnectionManager) -> None:
        assert manager.get_instances() == []


class TestRefresh:
    """Tests for refresh functionality."""

    async def test_refresh_replaces_prior_scan_without_stale_connection(
        self, manager: ConnectionManager
    ) -> None:
        manager.connections[19723] = ArchicadConnection(
            19723,
            manager.session,
            {"version": "28", "tapirAvailable": True},
        )
        with aioresponses() as mocked:
            for port in PORT_RANGE:
                mocked.post(
                    f"http://127.0.0.1:{port}",
                    exception=aiohttp.ClientError("Connection refused"),
                )
            await manager.refresh()

        assert manager.connections == {}
