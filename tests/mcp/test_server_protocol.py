"""Focused protocol tests for the native MCP SDK v2 server."""

from __future__ import annotations

import asyncio
from importlib.metadata import entry_points

import pytest
from mcp.client import Client

from archicad_mcp import cli as cli_module
from archicad_mcp import server as server_module
from archicad_mcp.core.connection import ArchicadConnection
from archicad_mcp.core.manager import ConnectionManager
from archicad_mcp.schemas.cache import SchemaCache


async def _no_archicad_scan(_: ConnectionManager) -> None:
    """Keep protocol tests independent of a live Archicad instance."""


async def _one_local_instance(manager: ConnectionManager) -> None:
    manager.connections[19723] = ArchicadConnection(
        19723,
        manager.session,
        {"version": "test", "projectName": "Test", "tapirAvailable": False},
    )


async def _one_tapir_instance(manager: ConnectionManager) -> None:
    manager.connections[19723] = ArchicadConnection(
        19723,
        manager.session,
        {"version": "29.0.0", "projectName": "Test", "tapirAvailable": True},
    )


@pytest.fixture(autouse=True)
def _offline_lifespans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protocol tests never exercise live update networking."""
    monkeypatch.setenv("ARCHICAD_MCP_OFFLINE", "1")


@pytest.mark.asyncio
async def test_server_exposes_exactly_four_tools_with_compact_honest_execute_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)
    server = server_module.create_server()

    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {"list_instances", "get_docs", "get_properties", "execute_script"}
    assert all(tool.output_schema is not None for tool in tools.values())

    description = tools["execute_script"].description or ""
    assert len(description) < 1000
    assert "local_user" in description
    assert "not hostile-code isolation" in description
    assert "destructive" in description
    assert "timeout" in description.lower()
    assert "cancellation" in description.lower()
    assert "get_docs" in description
    assert "BLOCKED" not in description
    assert "ALLOWED WRITE" not in description


@pytest.mark.asyncio
async def test_native_initialization_discovery_and_tool_calls_without_archicad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)

    async with Client(server_module.create_server()) as client:
        assert client.protocol_version == "2026-07-28"

        tools = await client.list_tools()
        tools_by_name = {tool.name: tool for tool in tools.tools}
        assert set(tools_by_name) == {
            "list_instances",
            "get_docs",
            "get_properties",
            "execute_script",
        }
        assert all(tool.output_schema is not None for tool in tools_by_name.values())

        instances = await client.call_tool("list_instances")
        assert not instances.is_error
        assert instances.structured_content == {"result": []}

        docs = await client.call_tool("get_docs")
        assert not docs.is_error
        assert docs.structured_content is not None
        assert docs.structured_content["total"] == 309
        assert docs.structured_content["provider_counts"] == {"native": 73, "tapir": 236}
        assert docs.structured_content["status"] == "compatibility_unknown"

        missing_properties = await client.call_tool("get_properties", {"port": 19723})
        missing_script = await client.call_tool(
            "execute_script",
            {"port": 19723, "script": "result = 1"},
        )

    assert missing_properties.is_error
    assert missing_properties.structured_content is None
    assert missing_script.is_error
    assert missing_script.structured_content is None


@pytest.mark.asyncio
async def test_get_docs_registry_modes_are_complete_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)

    async with Client(server_module.create_server()) as client:
        overview = await client.call_tool("get_docs")
        category = await client.call_tool("get_docs", {"category": "Element Listing Commands"})
        search = await client.call_tool("get_docs", {"search": "GetAllElements"})
        bare = await client.call_tool("get_docs", {"command": "API.GetAllElements"})
        namespaced = await client.call_tool("get_docs", {"command": "native:API.GetAllElements"})
        batch = await client.call_tool(
            "get_docs",
            {
                "commands": [
                    "API.GetAllElements",
                    "native:API.GetAllElements",
                    "MissingCapability",
                ]
            },
        )
        missing = await client.call_tool("get_docs", {"command": "MissingCapability"})

    assert overview.structured_content is not None
    assert overview.structured_content["total"] == 309
    assert overview.structured_content["provider_counts"] == {"native": 73, "tapir": 236}
    assert overview.structured_content["status"] == "compatibility_unknown"
    assert sum(item["count"] for item in overview.structured_content["categories"]) == 309

    assert category.structured_content is not None
    category_content = category.structured_content
    assert category_content["total"] == len(category_content["capability_names"])
    assert category_content["total"] == len(category_content["capabilities"])
    assert category_content["capability_names"] == sorted(category_content["capability_names"])
    assert "API.GetAllElements" in category_content["capability_names"]

    assert search.structured_content is not None
    search_content = search.structured_content
    assert search_content["total"] == len(search_content["results"])
    assert search_content["results"][0]["id"] == "native:API.GetAllElements"
    assert search_content["exhaustive_browse"]["overview"] == "get_docs()"
    assert "category" in search_content["exhaustive_browse"]

    assert bare.structured_content is not None
    assert namespaced.structured_content is not None
    assert bare.structured_content["id"] == "native:API.GetAllElements"
    assert namespaced.structured_content == bare.structured_content
    assert "command" in bare.structured_content
    assert "$defs" in bare.structured_content

    assert batch.structured_content is not None
    assert [document["id"] for document in batch.structured_content["documents"]] == [
        "native:API.GetAllElements",
        "native:API.GetAllElements",
    ]
    assert batch.structured_content["missing"] == ["MissingCapability"]

    assert missing.structured_content is not None
    assert missing.structured_content["found"] is False
    assert missing.structured_content["error"] == "capability_not_found"
    assert missing.structured_content["missing"] == ["MissingCapability"]
    assert "suggestion" in missing.structured_content


def test_load_native_snapshot_serves_the_packaged_builtin_floor() -> None:
    snapshot = server_module._load_native_snapshot()
    assert snapshot.provider == "native"
    assert snapshot.provider_version == "2.0.0"
    assert snapshot.distribution == "packaged builtin.json"
    assert snapshot.provenance == ("package:archicad_mcp.schemas/builtin.json",)
    assert snapshot.command_count > 0


@pytest.mark.asyncio
async def test_tapir_documents_carry_baseline_version_and_late_release_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)

    async with Client(server_module.create_server()) as client:
        create_walls = await client.call_tool("get_docs", {"command": "tapir:CreateWalls"})
        bare_walls = await client.call_tool("get_docs", {"command": "CreateWalls"})
        section_elements = await client.call_tool(
            "get_docs", {"command": "tapir:GetSectionElements"}
        )
        native_doc = await client.call_tool("get_docs", {"command": "native:API.GetAllElements"})

    assert create_walls.structured_content is not None
    assert bare_walls.structured_content == create_walls.structured_content
    document = create_walls.structured_content
    assert document["id"] == "tapir:CreateWalls"
    assert document["provider"] == "tapir"
    assert document["provider_version"] == "1.5.8"
    assert document["provider_distribution"] == "packaged archicad_mcp/schemas/tapir.json"
    command = document["command"]
    assert command["category"] == "Element Commands"
    assert command["version"] == "1.4.0"
    assert command["parameters"]["required"] == ["wallsData"]
    assert command["returns"]["required"] == ["elements"]
    assert sorted(document["$defs"]) == [
        "AttributeId",
        "Coordinate2D",
        "ElementId",
        "ElementIdArrayItem",
        "ElementIdOrError",
        "ElementIdsOrErrors",
        "Error",
        "ErrorItem",
        "Guid",
    ]
    provenance = document["provenance"]
    assert provenance[0] == "path:archicad_mcp/schemas/tapir.json"
    assert "distribution:packaged" in provenance
    assert "upstream:https://github.com/ENZYME-APD/tapir-archicad-automation" in provenance
    assert "tag:1.5.8" in provenance
    assert "commit:ce033d6bdcc90b538b3c5f7ab62f676099b96823" in provenance
    assert "license:MIT" in provenance
    assert "generator:scripts/generate_tapir_snapshot.py" in provenance
    assert any(
        ref.startswith("input-sha256:common_schema_definitions.js=2f003f9c") for ref in provenance
    )

    # A command introduced after 1.5.6 proves the snapshot is not a stale partial
    # catalog that merely matches the packaged count.
    assert section_elements.structured_content is not None
    section_command = section_elements.structured_content["command"]
    assert section_elements.structured_content["provider_version"] == "1.5.8"
    assert section_command["version"] == "1.5.8"
    assert section_command["category"] == "Element Commands"

    assert native_doc.structured_content is not None
    assert native_doc.structured_content["provider_version"] == "2.0.0"


async def test_observed_native_only_instance_excludes_tapir_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _one_local_instance)

    async with Client(server_module.create_server()) as client:
        overview = await client.call_tool("get_docs")
        tapir_command = await client.call_tool("get_docs", {"command": "CreateSlabs"})

    assert overview.structured_content is not None
    assert overview.structured_content["status"] == "tapir_unavailable"
    assert overview.structured_content["target_identity"] == "test"
    assert overview.structured_content["provider_counts"] == {"native": 73, "tapir": 0}
    assert overview.structured_content["total"] == 73

    assert tapir_command.structured_content is not None
    assert tapir_command.structured_content["found"] is False
    assert tapir_command.structured_content["error"] == "capability_not_found"


@pytest.mark.asyncio
async def test_tapir_view_uses_packaged_snapshot_only_after_live_availability_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_live_schema_fetch(
        _connection: ArchicadConnection,
        _command: str,
        _parameters: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError("server must not ingest live schema bytes")

    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _one_tapir_instance)
    monkeypatch.setattr(ArchicadConnection, "execute", reject_live_schema_fetch)

    async with Client(server_module.create_server()) as client:
        overview = await client.call_tool("get_docs")
        bare = await client.call_tool("get_docs", {"command": "CreateSlabs"})
        namespaced = await client.call_tool("get_docs", {"command": "tapir:CreateSlabs"})

    assert overview.structured_content is not None
    assert overview.structured_content["status"] == "tapir_available"
    assert overview.structured_content["target_identity"] == "29.0.0"
    assert overview.structured_content["provider_counts"] == {"native": 73, "tapir": 236}
    assert overview.structured_content["total"] == 309

    assert bare.structured_content is not None
    assert namespaced.structured_content is not None
    assert bare.structured_content["id"] == "tapir:CreateSlabs"
    assert namespaced.structured_content == bare.structured_content
    assert "command" in bare.structured_content
    assert "$defs" in bare.structured_content


def test_schema_cache_exposes_no_runtime_package_write_or_merge_api() -> None:
    for method_name in (
        "load_from_tapir",
        "_save_tapir_cache",
        "_save_builtin_cache",
        "sync_from_repo",
        "_sync_tapir_from_repo",
        "_sync_builtin_from_repo",
    ):
        assert not hasattr(SchemaCache, method_name)


@pytest.mark.asyncio
async def test_lifespan_closes_shared_http_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)
    server = server_module.create_server()

    async with server_module.lifespan(server) as state:
        session = state["session"]
        assert not session.closed
        assert "manager" in state
        assert "executor" in state
        assert state["schemas"].summary()["provider_counts"] == {"native": 73, "tapir": 236}
        assert "property_cache" in state

    assert session.closed


@pytest.mark.asyncio
async def test_concurrent_client_lifecycles_have_isolated_managers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = 0
    entered_lock = asyncio.Lock()
    both_entered = asyncio.Event()
    managers: list[ConnectionManager] = []

    async def overlapping_scan(manager: ConnectionManager) -> None:
        nonlocal entered
        managers.append(manager)
        async with entered_lock:
            entered += 1
            if entered >= 2:
                both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=5)

    monkeypatch.setattr(ConnectionManager, "scan_and_connect", overlapping_scan)
    server = server_module.create_server()

    async def use_server() -> tuple[str, int]:
        async with Client(server) as client:
            tools = await client.list_tools()
            docs = await client.call_tool("get_docs")
            assert len(tools.tools) == 4
            assert docs.structured_content is not None
            total_commands = docs.structured_content["total"]
            assert isinstance(total_commands, int)
            return client.protocol_version, total_commands

    first, second = await asyncio.gather(use_server(), use_server())

    assert first[0] == "2026-07-28"
    assert second[0] == "2026-07-28"
    assert first[1] > 0
    assert second[1] > 0
    assert len({id(manager) for manager in managers}) == 2


@pytest.mark.asyncio
async def test_official_client_legacy_mode_negotiates_earlier_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _no_archicad_scan)

    async with Client(server_module.create_server(), mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "list_instances",
            "get_docs",
            "get_properties",
            "execute_script",
        }
        response = await client.call_tool("list_instances")

    assert not response.is_error
    assert response.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_execute_script_response_remains_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ConnectionManager, "scan_and_connect", _one_local_instance)

    async with Client(server_module.create_server()) as client:
        response = await client.call_tool(
            "execute_script",
            {"port": 19723, "script": "result = {'answer': 42}"},
        )
        invalid_type = await client.call_tool(
            "execute_script",
            {"port": 19723, "script": "result = 1", "timeout_seconds": True},
        )
        invalid_range = await client.call_tool(
            "execute_script",
            {"port": 19723, "script": "result = 1", "timeout_seconds": 0},
        )

    assert not response.is_error
    assert response.structured_content is not None
    assert response.structured_content["success"] is True
    assert response.structured_content["result"] == {"answer": 42}
    assert response.structured_content["stdout"] == ""
    assert response.structured_content["stderr"] == ""
    assert response.structured_content["error"] is None
    assert response.structured_content["error_code"] is None
    assert response.structured_content["execution_model"] == "local_user"
    assert isinstance(response.structured_content["execution_time_ms"], int)
    assert invalid_type.is_error
    assert invalid_range.is_error


def test_console_entry_point_targets_cli() -> None:
    entry_point = next(
        ep for ep in entry_points(group="console_scripts") if ep.name == "archicad-mcp"
    )
    assert entry_point.value == "archicad_mcp.cli:main"
    assert entry_point.load() is cli_module.main
