"""Focused tests for the immutable in-memory schema capability registry."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

import archicad_mcp.schemas.registry as registry_module
from archicad_mcp.schemas.registry import (
    CapabilityView,
    ProviderSnapshot,
    SchemaRegistryError,
    ViewStatus,
    load_provider_snapshot,
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _presentation(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _native_payload(commands: dict[str, Any], definitions: dict[str, Any] | None = None) -> bytes:
    return _json_bytes({"commands": commands, "$defs": definitions or {}})


def _tapir_payload(commands: dict[str, Any], definitions: dict[str, Any] | None = None) -> bytes:
    return _json_bytes({"commands": commands, "common_schemas": definitions or {}})


def _load_native(
    payload: bytes,
    *,
    version: str = "1.0",
    distribution: str = "fixture-native",
    provenance: Sequence[str] = ("fixture:native",),
) -> ProviderSnapshot:
    return load_provider_snapshot(
        payload,
        provider="native",
        provider_version=version,
        distribution=distribution,
        provenance=provenance,
    )


def _load_tapir(
    payload: bytes,
    *,
    version: str = "1.0",
    distribution: str = "fixture-tapir",
    provenance: Sequence[str] = ("fixture:tapir",),
) -> ProviderSnapshot:
    return load_provider_snapshot(
        payload,
        provider="tapir",
        provider_version=version,
        distribution=distribution,
        provenance=provenance,
    )


def _view(
    native: ProviderSnapshot,
    tapir: ProviderSnapshot | None = None,
    *,
    target: str = "target-a",
    status: ViewStatus | None = None,
) -> CapabilityView:
    selected_status = status
    if selected_status is None:
        selected_status = (
            ViewStatus.TAPIR_AVAILABLE if tapir is not None else ViewStatus.TAPIR_UNAVAILABLE
        )
    return CapabilityView(
        native=native,
        tapir=tapir,
        target_identity=target,
        status=selected_status,
    )


def test_strict_json_ingress_rejects_malformed_duplicate_numbers_and_surrogates() -> None:
    cases = (
        (b'{"commands":{"A":{"x":"\xff"}},"$defs":{}}', "malformed_utf8"),
        (b'{"commands":', "malformed_json"),
        (b'{"commands":{"A":{},"A":{}},"$defs":{}}', "duplicate_json_name"),
        (b'{"commands":{"A":{"x":1.25}},"$defs":{}}', "float_not_allowed"),
        (b'{"commands":{"A":{"x":NaN}},"$defs":{}}', "nonfinite_not_allowed"),
        (b'{"commands":{"A":{"x":"\\ud800"}},"$defs":{}}', "surrogate_not_allowed"),
    )
    for payload, code in cases:
        with pytest.raises(SchemaRegistryError) as caught:
            _load_native(payload)
        assert caught.value.code == code


def test_strict_provider_shape_and_runtime_types() -> None:
    malformed = (
        b"[]",
        b'{"commands":[],"$defs":{}}',
        b'{"commands":{},"$defs":[]}',
        b'{"commands":{"A":[]},"$defs":{}}',
        b'{"commands":{},"$defs":{"D":[]}}',
    )
    for payload in malformed:
        with pytest.raises(SchemaRegistryError, match="malformed_provider_shape"):
            _load_native(payload)

    with pytest.raises(SchemaRegistryError, match="empty_command_name"):
        _load_native(_native_payload({"": {}}))
    with pytest.raises(SchemaRegistryError, match="invalid_source_bytes_type"):
        load_provider_snapshot(
            cast(Any, bytearray(_native_payload({"A": {}}))),
            provider="native",
            provider_version="1",
            distribution="fixture",
            provenance=("fixture:native",),
        )
    with pytest.raises(SchemaRegistryError, match="invalid_provider"):
        load_provider_snapshot(
            _native_payload({"A": {}}),
            provider=cast(Any, "other"),
            provider_version="1",
            distribution="fixture",
            provenance=("fixture:native",),
        )
    for bad_version, bad_distribution, bad_provenance in (
        ("", "fixture", ("fixture:native",)),
        ("1", " ", ("fixture:native",)),
        ("1", "fixture", ()),
        ("1", "fixture", cast(Any, "fixture:native")),
    ):
        with pytest.raises(SchemaRegistryError):
            load_provider_snapshot(
                _native_payload({"A": {}}),
                provider="native",
                provider_version=bad_version,
                distribution=bad_distribution,
                provenance=bad_provenance,
            )


def test_refs_reject_external_bad_types_bad_pointer_and_dangling() -> None:
    commands = {
        "A": {"returns": {"$ref": "https://example.invalid/schema.json"}},
    }
    with pytest.raises(SchemaRegistryError, match="unsupported_reference"):
        _load_native(_native_payload(commands))

    with pytest.raises(SchemaRegistryError, match="invalid_ref_type"):
        _load_native(_native_payload({"A": {"returns": {"$ref": 1}}}))

    with pytest.raises(SchemaRegistryError, match="unsupported_reference"):
        _load_native(_native_payload({"A": {"returns": {"$ref": "#/$defs/A~2B"}}}))

    with pytest.raises(SchemaRegistryError) as caught:
        _load_native(_native_payload({"A": {"returns": {"$ref": "#/$defs/Missing"}}}))
    assert caught.value.code == "dangling_local_reference"
    assert caught.value.missing_definitions == (("Missing", 1),)


def test_transitive_dangling_and_external_refs_fail_closed() -> None:
    direct = {"A": {"returns": {"$ref": "#/$defs/First"}}}
    with pytest.raises(SchemaRegistryError) as caught:
        _load_native(
            _native_payload(direct, {"First": {"properties": {"x": {"$ref": "#/$defs/Later"}}}})
        )
    assert caught.value.code == "dangling_local_reference"
    assert caught.value.missing_definitions == (("Later", 1),)

    with pytest.raises(SchemaRegistryError, match="unsupported_reference"):
        _load_native(
            _native_payload(
                direct,
                {"First": {"properties": {"x": {"$ref": "other.json#/$defs/X"}}}},
            )
        )


@pytest.mark.parametrize(
    ("keyword", "value"),
    (
        ("$id", "https://example.invalid/schema"),
        ("$anchor", "node"),
        ("$dynamicAnchor", "node"),
        ("$dynamicRef", "#node"),
        ("$recursiveAnchor", True),
        ("$recursiveRef", "#"),
    ),
)
def test_resolution_changing_schema_keywords_fail_closed_in_commands_and_reachable_definitions(
    keyword: str, value: Any
) -> None:
    with pytest.raises(SchemaRegistryError) as caught:
        _load_native(_native_payload({"A": {"parameters": {keyword: value}}}))
    assert caught.value.code == "unsupported_schema_resolution_keyword"

    with pytest.raises(SchemaRegistryError) as caught:
        _load_native(
            _native_payload(
                {"A": {"returns": {"$ref": "#/$defs/Reachable"}}},
                {"Reachable": {"allOf": [{keyword: value}]}},
            )
        )
    assert caught.value.code == "unsupported_schema_resolution_keyword"


def test_native_and_tapir_ref_normalization_pointer_escaping_and_ref_siblings() -> None:
    definition_name = "A/B~C"
    encoded = "A~1B~0C"
    native = _load_native(
        _native_payload(
            {
                "Use": {
                    "returns": {
                        "$ref": f"#/$defs/{encoded}",
                        "description": "sibling stays",
                    }
                }
            },
            {definition_name: {"type": "string"}},
        )
    )
    tapir = _load_tapir(
        _tapir_payload(
            {"Use": {"returns": {"$ref": f"#/{encoded}", "description": "sibling stays"}}},
            {definition_name: {"type": "string"}},
        )
    )

    native_doc = _view(native).get("native:Use")
    tapir_doc = _view(
        _load_native(_native_payload({"Native": {}})), tapir, status=ViewStatus.TAPIR_AVAILABLE
    ).get("tapir:Use")
    assert native_doc is not None and tapir_doc is not None
    for document in (native_doc, tapir_doc):
        returns = document["command"]["returns"]
        assert returns == {"$ref": f"#/$defs/{encoded}", "description": "sibling stays"}
        assert document["$defs"] == {definition_name: {"type": "string"}}


def test_transitive_and_cyclic_closure_is_complete_without_expansion_or_depth_cutoff() -> None:
    definitions: dict[str, Any] = {}
    chain_length = 32
    for index in range(chain_length):
        name = f"D{index}"
        next_name = f"D{index + 1}" if index + 1 < chain_length else "D0"
        definitions[name] = {"next": {"$ref": f"#/{next_name}"}}
    snapshot = _load_tapir(_tapir_payload({"Walk": {"returns": {"$ref": "#/D0"}}}, definitions))
    native = _load_native(_native_payload({"Native": {}}))
    document = _view(native, snapshot).get("tapir:Walk")
    assert document is not None
    assert len(document["$defs"]) == chain_length
    assert document["command"]["returns"] == {"$ref": "#/$defs/D0"}
    assert document["$defs"]["D31"]["next"] == {"$ref": "#/$defs/D0"}


def test_unknown_command_fields_examples_notes_and_source_definitions_are_preserved() -> None:
    command = {
        "category": "Custom",
        "description": "Preserve every field",
        "parameters": {"type": "object"},
        "example": {"alpha": 1},
        "examples": [{"beta": 2}],
        "notes": "important note",
        "future_field": {"nested": [1, 2, 3]},
    }
    source = {
        "commands": {"Preserve": command},
        "common_schemas": {"Unused": {"description": "still snapshot data"}},
        "element_types": ["Wall"],
        "unknown_top_level": {"keep": True},
    }
    snapshot = _load_tapir(_json_bytes(source))
    native = _load_native(_native_payload({"Native": {}}))
    document = _view(native, snapshot).get("tapir:Preserve")
    assert document is not None
    assert document["command"] == command
    assert document["$defs"] == {}
    assert snapshot.source_data() == source


def test_snapshot_view_and_retrieval_are_immutable_against_caller_mutation() -> None:
    provenance = ["fixture:native"]
    snapshot = _load_native(
        _native_payload(
            {"A": {"returns": {"$ref": "#/$defs/D"}}},
            {"D": {"properties": {"value": {"type": "string"}}}},
        ),
        provenance=provenance,
    )
    provenance.append("changed-after-load")
    assert snapshot.provenance == ("fixture:native",)

    source_copy = snapshot.source_data()
    source_copy["commands"]["A"]["new"] = True
    assert "new" not in snapshot.source_data()["commands"]["A"]

    view = _view(snapshot)
    first = view.get("native:A")
    assert first is not None
    first["command"]["returns"]["$ref"] = "changed"
    first["$defs"]["D"]["properties"]["value"]["type"] = "number"
    second = view.get("native:A")
    assert second is not None
    assert second["command"]["returns"]["$ref"] == "#/$defs/D"
    assert second["$defs"]["D"]["properties"]["value"]["type"] == "string"

    with pytest.raises(FrozenInstanceError):
        snapshot.revision = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        view.revision = "changed"  # type: ignore[misc]


def test_provider_snapshot_factory_and_view_boundary_reject_forged_state() -> None:
    with pytest.raises(TypeError, match="created by load_provider_snapshot"):
        ProviderSnapshot()

    unmarked = object.__new__(ProviderSnapshot)
    with pytest.raises(SchemaRegistryError) as caught:
        _view(unmarked)
    assert caught.value.code == "invalid_provider_snapshot"

    native = _load_native(_native_payload({"Native": {}}))
    tapir = _load_tapir(_tapir_payload({"Tapir": {}}))
    object.__setattr__(native, "_records", tapir._records)
    with pytest.raises(SchemaRegistryError) as caught:
        _view(native)
    assert caught.value.code == "invalid_provider_snapshot"


def test_stable_ids_snapshot_revisions_source_and_document_digests_are_separate() -> None:
    payload = _native_payload({"A": {"description": "alpha"}})
    same = _load_native(payload)
    repeated = _load_native(payload)
    whitespace_changed = _load_native(payload + b" ")
    version_changed = _load_native(payload, version="2.0")
    distribution_changed = _load_native(payload, distribution="fixture-native-v2")

    assert same.capability_ids == repeated.capability_ids == whitespace_changed.capability_ids
    assert same.revision == repeated.revision
    assert same.source_sha256 == repeated.source_sha256
    assert same.source_sha256 != whitespace_changed.source_sha256
    assert same.revision != whitespace_changed.revision
    assert same.revision != version_changed.revision
    assert same.revision != distribution_changed.revision

    same_view = _view(same)
    document = same_view.get("native:A")
    assert document is not None
    document_digest = hashlib.sha256(_presentation(document)).hexdigest()
    assert same_view.document_sha256("native:A") == document_digest
    assert _view(repeated).document_sha256("native:A") == document_digest
    assert _view(whitespace_changed).document_sha256("native:A") != document_digest
    assert _view(version_changed).document_sha256("native:A") != document_digest


def test_ordered_provenance_changes_snapshot_document_and_view_revisions_but_not_ids() -> None:
    payload = _native_payload(
        {
            "A": {"description": "alpha"},
            "B": {"description": "beta"},
        }
    )
    first = _load_native(payload, provenance=("source:first", "source:second"))
    reordered = _load_native(payload, provenance=("source:second", "source:first"))
    tapir = _load_tapir(_tapir_payload({"Tapir": {}}))

    assert first.source_sha256 == reordered.source_sha256
    assert first.capability_ids == reordered.capability_ids
    assert first.revision != reordered.revision

    first_view = _view(first, tapir)
    reordered_view = _view(reordered, tapir)
    assert first_view.revision != reordered_view.revision
    for capability_id in first.capability_ids:
        assert first_view.document_sha256(capability_id) != reordered_view.document_sha256(
            capability_id
        )


def test_cross_provider_same_name_and_definition_names_are_isolated() -> None:
    native = _load_native(
        _native_payload(
            {"Same": {"returns": {"$ref": "#/$defs/Shared"}}},
            {"Shared": {"const": "native"}},
        )
    )
    tapir = _load_tapir(
        _tapir_payload(
            {"Same": {"returns": {"$ref": "#/Shared"}}},
            {"Shared": {"const": "tapir"}},
        )
    )
    view = _view(native, tapir)
    assert view.summary()["total"] == 2
    assert [item["id"] for item in view.browse(10)["capabilities"]] == [
        "native:Same",
        "tapir:Same",
    ]
    native_doc = view.get("native:Same")
    tapir_doc = view.get("tapir:Same")
    assert native_doc is not None and tapir_doc is not None
    assert native_doc["$defs"]["Shared"]["const"] == "native"
    assert tapir_doc["$defs"]["Shared"]["const"] == "tapir"


def test_exact_and_batch_retrieval_parity_order_missing_and_duplicate_ids() -> None:
    native = _load_native(_native_payload({"A": {"description": "a"}, "B": {"description": "b"}}))
    view = _view(native)
    batch = view.get_many(["native:B", "missing:X", "native:A"])
    assert [document["id"] for document in batch["documents"]] == ["native:B", "native:A"]
    assert batch["missing"] == ["missing:X"]
    for document in batch["documents"]:
        exact = view.get(document["id"])
        assert exact is not None
        assert _presentation(document) == _presentation(exact)

    batch["documents"][0]["command"]["description"] = "mutated"
    assert view.get("native:B")["command"]["description"] == "b"  # type: ignore[index]
    assert view.get("missing:X") is None
    with pytest.raises(SchemaRegistryError, match="duplicate_capability_id"):
        view.get_many(["native:A", "native:A"])


def test_browse_cursor_is_complete_unbounded_and_view_bound() -> None:
    commands = {f"C{index}": {"description": f"command {index}"} for index in range(7)}
    view = _view(_load_native(_native_payload(commands)))
    cursor: str | None = None
    seen: list[str] = []
    while True:
        page = view.browse(2, cursor)
        seen.extend(item["id"] for item in page["capabilities"])
        cursor = cast(str | None, page["next_cursor"])
        if cursor is None:
            break
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 7
    assert view.browse(100_000)["capabilities"] == view.browse(7)["capabilities"]

    first_page = view.browse(2)
    first_cursor = cast(str, first_page["next_cursor"])
    other_view = _view(view.native, target="target-b")
    with pytest.raises(SchemaRegistryError, match="cursor_view_mismatch"):
        other_view.browse(2, first_cursor)
    for malformed in (
        f"{view.revision}:x",
        f"{view.revision}:0",
        f"{view.revision}:01",
        f"{view.revision}:999",
    ):
        with pytest.raises(SchemaRegistryError, match="malformed_cursor_offset"):
            view.browse(2, malformed)
    for invalid_size in (0, -1, cast(Any, True)):
        with pytest.raises(SchemaRegistryError, match="invalid_page_size"):
            view.browse(invalid_size)


def test_cursor_offset_rejects_huge_ascii_and_unicode_decimal_without_raw_value_error() -> None:
    view = _view(_load_native(_native_payload({"A": {}, "B": {}, "C": {}})))
    for raw_offset in ("9" * 5001, "\u0661"):
        with pytest.raises(SchemaRegistryError) as caught:
            view.browse(1, f"{view.revision}:{raw_offset}")
        assert caught.value.code == "malformed_cursor_offset"


def test_intent_search_multiword_typo_fields_definitions_and_stable_ties() -> None:
    definitions = {
        "MaterialKind": {
            "description": "structural material selection",
            "enum": ["Concrete", "Brick", "ReinforcedConcrete"],
        }
    }
    commands = {
        "CreateConcreteWall": {
            "category": "Element Creation",
            "description": "Create a concrete wall element",
            "parameters": {
                "properties": {
                    "profileName": {"description": "profile name", "type": "string"},
                    "material": {"$ref": "#/$defs/MaterialKind"},
                }
            },
            "returns": {"description": "created wall identifier"},
            "example": {"profileName": "Core"},
            "notes": "renovation workflow",
        },
        "SharedA": {"description": "shared intent"},
        "SharedB": {"description": "shared intent"},
    }
    view = _view(_load_native(_native_payload(commands, definitions)))
    browse_before = view.browse(100)

    multiword = view.search("create concrete wall")
    assert multiword["results"][0]["id"] == "native:CreateConcreteWall"
    assert view.search("conrete wall")["results"][0]["id"] == "native:CreateConcreteWall"
    assert view.search("profile name")["results"][0]["id"] == "native:CreateConcreteWall"
    assert view.search("created identifier")["results"][0]["id"] == "native:CreateConcreteWall"
    assert view.search("renovation")["results"][0]["id"] == "native:CreateConcreteWall"
    assert view.search("reinforced concrete")["results"][0]["id"] == "native:CreateConcreteWall"

    tied = view.search("shared intent")
    assert tied["total"] == 2
    assert [result["id"] for result in tied["results"]] == ["native:SharedA", "native:SharedB"]
    assert view.search("!!! ___")["results"] == []
    assert view.browse(100) == browse_before


def test_view_status_counts_categories_and_revision_binding() -> None:
    native = _load_native(
        _native_payload(
            {
                "A": {"category": "One"},
                "B": {"category": "Two"},
            }
        )
    )
    tapir = _load_tapir(_tapir_payload({"T": {"category": "One"}}))
    native_only = _view(native, status=ViewStatus.TAPIR_UNAVAILABLE)
    generic = _view(native, tapir, status=ViewStatus.COMPATIBILITY_UNKNOWN)
    selected = _view(native, tapir, status=ViewStatus.TAPIR_AVAILABLE)

    assert native_only.summary()["status"] == "tapir_unavailable"
    assert generic.summary()["status"] == "compatibility_unknown"
    selected_summary = selected.summary()
    assert selected_summary["status"] == "tapir_available"
    assert selected_summary["provider_counts"] == {"native": 2, "tapir": 1}
    assert selected_summary["categories"] == [
        {"name": "One", "count": 2},
        {"name": "Two", "count": 1},
    ]
    assert generic.revision != selected.revision
    assert selected.revision != _view(native, tapir, target="target-b").revision

    with pytest.raises(SchemaRegistryError, match="tapir_unavailable_with_snapshot"):
        _view(native, tapir, status=ViewStatus.TAPIR_UNAVAILABLE)
    with pytest.raises(SchemaRegistryError, match="tapir_available_without_snapshot"):
        _view(native, status=ViewStatus.TAPIR_AVAILABLE)


def test_embedded_tapir_payload_loads_strictly() -> None:
    schema_dir = Path(registry_module.__file__).parent
    tapir_bytes = (schema_dir / "tapir.json").read_bytes()
    root = cast(dict[str, Any], json.loads(tapir_bytes.decode("utf-8")))
    metadata = root["_metadata"]
    provenance = (
        "package:archicad_mcp.schemas/tapir.json",
        "upstream:https://github.com/ENZYME-APD/tapir-archicad-automation",
        "tag:1.5.8",
        "commit:ce033d6bdcc90b538b3c5f7ab62f676099b96823",
        "input-sha256:command_definitions.js="
        "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8",
        "input-sha256:common_schema_definitions.js="
        "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0",
        "license:MIT",
        "generator:scripts/generate_tapir_snapshot.py",
    )
    snapshot = load_provider_snapshot(
        tapir_bytes,
        provider="tapir",
        provider_version="1.5.8",
        distribution="packaged tapir.json",
        provenance=provenance,
    )
    assert snapshot.provider == "tapir"
    assert snapshot.provider_version == "1.5.8"
    assert snapshot.command_count == 236
    assert snapshot.definition_count == 335
    assert len(snapshot.capability_ids) == 236
    assert snapshot.provenance == provenance
    assert metadata == {
        "format": "archicad-mcp.tapir-snapshot/1",
        "provider": "tapir",
        "provider_version": "1.5.8",
        "package_path": "archicad_mcp/schemas/tapir.json",
        "upstream_repository": ("https://github.com/ENZYME-APD/tapir-archicad-automation"),
        "upstream_tag": "1.5.8",
        "upstream_commit": "ce033d6bdcc90b538b3c5f7ab62f676099b96823",
        "license": "MIT",
        "inputs": {
            "command_definitions.js": (
                "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8"
            ),
            "common_schema_definitions.js": (
                "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0"
            ),
        },
        "generator": "scripts/generate_tapir_snapshot.py",
    }


def test_view_exposes_packaged_tapir_identity_and_provenance() -> None:
    schema_dir = Path(registry_module.__file__).parent
    native = load_provider_snapshot(
        (schema_dir / "builtin.json").read_bytes(),
        provider="native",
        provider_version="2.0.0",
        distribution="packaged builtin.json",
        provenance=("package:archicad_mcp.schemas/builtin.json",),
    )
    tapir_provenance = (
        "package:archicad_mcp.schemas/tapir.json",
        "upstream:https://github.com/ENZYME-APD/tapir-archicad-automation",
        "tag:1.5.8",
        "commit:ce033d6bdcc90b538b3c5f7ab62f676099b96823",
        "input-sha256:command_definitions.js="
        "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8",
        "input-sha256:common_schema_definitions.js="
        "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0",
        "license:MIT",
        "generator:scripts/generate_tapir_snapshot.py",
    )
    tapir = load_provider_snapshot(
        (schema_dir / "tapir.json").read_bytes(),
        provider="tapir",
        provider_version="1.5.8",
        distribution="packaged tapir.json",
        provenance=tapir_provenance,
    )
    view = _view(native, tapir)
    document = view.get("tapir:CreateWalls")
    assert document is not None
    assert document["provider_version"] == "1.5.8"
    assert document["provider_distribution"] == "packaged tapir.json"
    assert document["provenance"] == list(tapir_provenance)
    command = document["command"]
    assert command["category"] == "Element Commands"
    assert command["version"] == "1.4.0"
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


def test_embedded_native_payload_loads_strictly_with_closed_definition_graph() -> None:
    schema_dir = Path(registry_module.__file__).parent
    snapshot = load_provider_snapshot(
        (schema_dir / "builtin.json").read_bytes(),
        provider="native",
        provider_version="2.0.0",
        distribution="packaged builtin.json",
        provenance=("package:archicad_mcp.schemas/builtin.json",),
    )
    assert snapshot.command_count == 73
    assert snapshot.definition_count == 257

    view = _view(snapshot)
    execute_add_on = view.get("native:API.ExecuteAddOnCommand")
    create_attribute_folders = view.get("native:API.CreateAttributeFolders")
    rename_attribute_folders = view.get("native:API.RenameAttributeFolders")
    create_view_map_folder = view.get("native:API.CreateViewMapFolder")
    create_layout = view.get("native:API.CreateLayout")
    assert execute_add_on is not None
    assert create_attribute_folders is not None
    assert rename_attribute_folders is not None
    assert create_view_map_folder is not None
    assert create_layout is not None
    assert "AddOnCommandParameters" in execute_add_on["$defs"]
    assert {
        "AttributeFolderCreationParameters",
        "ExecutionResult",
        "FailedExecutionResult",
        "SuccessfulExecutionResult",
    } <= create_attribute_folders["$defs"].keys()
    assert "AttributeFolderRenameParameters" in rename_attribute_folders["$defs"]
    assert "FolderParameters" in create_view_map_folder["$defs"]
    assert "LayoutParameters" in create_layout["$defs"]
