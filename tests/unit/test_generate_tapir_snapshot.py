"""Focused tests for the deterministic Tapir snapshot maintainer generator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts import generate_tapir_snapshot
from scripts.generate_tapir_snapshot import (
    InputRefused,
    build_document,
    main,
    metadata,
    serialize_document,
)

import archicad_mcp.schemas.registry as registry_module
from archicad_mcp.schemas.registry import load_provider_snapshot

_COMMAND_CATEGORIES = [
    {
        "name": "Element Commands",
        "commands": [
            {
                "name": "GetAddOnVersion",
                "description": "Reports the add-on version.",
                "version": "0.1.0",
                "inputScheme": None,
                "outputScheme": {
                    "type": "object",
                    "properties": {"version": {"type": "string"}},
                    "required": ["version"],
                },
            },
            {
                "name": "PingWalls",
                "description": "Creates walls.",
                "version": "1.4.0",
                "inputScheme": {"type": "object", "properties": {"height": 2.0}},
                "outputScheme": None,
            },
            {
                "name": "LateCommand",
                "description": "Added late.",
                "version": "1.5.8",
                "inputScheme": None,
                "outputScheme": None,
            },
        ],
    }
]
_COMMON_SCHEMAS = {
    "ElementType": {"enum": ["Wall", "Slab"]},
    "Guid": {"type": "string"},
}


def _commands_js(categories: Any) -> str:
    return f"var gCommands = {json.dumps(categories)};\n"


def _common_js(schemas: Any) -> str:
    return f"var gSchemaDefinitions = {json.dumps(schemas)};\n"


def _input_files(tmp_path: Path) -> tuple[Path, Path]:
    commands = tmp_path / "command_definitions.js"
    common = tmp_path / "common_schema_definitions.js"
    commands.write_text(_commands_js(_COMMAND_CATEGORIES), encoding="utf-8")
    common.write_text(_common_js(_COMMON_SCHEMAS), encoding="utf-8")
    return commands, common


def _document_bytes(commands: bytes, common: bytes) -> bytes:
    return serialize_document(build_document(commands, common)).encode("utf-8")


def _match_input_pins(
    monkeypatch: pytest.MonkeyPatch, commands_text: str, common_text: str
) -> None:
    """Point the generator pins at synthetic inputs so the shared transform
    proceeds past its declared-hash gate."""

    monkeypatch.setattr(
        generate_tapir_snapshot,
        "EXPECTED_INPUTS",
        {
            "command_definitions.js": hashlib.sha256(commands_text.encode("utf-8")).hexdigest(),
            "common_schema_definitions.js": hashlib.sha256(common_text.encode("utf-8")).hexdigest(),
        },
    )


def _full(name: str, version: str) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "description": f"{name}.",
        "inputScheme": None,
        "outputScheme": None,
    }


def test_document_is_deterministic_self_describing_and_registry_loadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands_text = _commands_js(_COMMAND_CATEGORIES)
    common_text = _common_js(_COMMON_SCHEMAS)
    _match_input_pins(monkeypatch, commands_text, common_text)
    first = build_document(commands_text.encode("utf-8"), common_text.encode("utf-8"))
    second = build_document(commands_text.encode("utf-8"), common_text.encode("utf-8"))
    assert first == second
    assert first["_metadata"] == metadata()
    ping = first["commands"]["PingWalls"]
    assert ping == {
        "category": "Element Commands",
        "description": "Creates walls.",
        "version": "1.4.0",
        "parameters": {"type": "object", "properties": {"height": 2}},
        "returns": None,
        "api": "tapir",
        "name": "PingWalls",
    }
    assert first["element_types"] == ["Wall", "Slab"]

    payload = _document_bytes(
        _commands_js(_COMMAND_CATEGORIES).encode("utf-8"),
        _common_js(_COMMON_SCHEMAS).encode("utf-8"),
    )
    again = _document_bytes(
        _commands_js(_COMMAND_CATEGORIES).encode("utf-8"),
        _common_js(_COMMON_SCHEMAS).encode("utf-8"),
    )
    assert payload == again
    root = json.loads(payload.decode("utf-8"))
    assert root["_metadata"] == metadata()
    assert metadata()["provider_version"] == "1.5.8"
    snapshot = load_provider_snapshot(
        payload,
        provider="tapir",
        provider_version=root["_metadata"]["provider_version"],
        provenance=(
            f"package:{root['_metadata']['package_path']}",
            f"upstream:{root['_metadata']['upstream_repository']}",
            f"tag:{root['_metadata']['upstream_tag']}",
            f"commit:{root['_metadata']['upstream_commit']}",
            "license:MIT",
        ),
        distribution="packaged tapir.json",
    )
    assert snapshot.command_count == 3
    assert snapshot.definition_count == 2


def test_packaged_snapshot_metadata_matches_generator_constants() -> None:
    schema_dir = Path(registry_module.__file__).parent
    packaged = json.loads((schema_dir / "tapir.json").read_text(encoding="utf-8"))
    assert packaged["_metadata"] == metadata()
    assert metadata()["inputs"] == generate_tapir_snapshot.EXPECTED_INPUTS


@pytest.mark.parametrize(
    ("commands_text", "common_text", "expected_code"),
    [
        pytest.param(
            _commands_js(
                [
                    {
                        "name": "C",
                        "commands": [
                            _full("Dup", "1.0.0"),
                            _full("Dup", "1.1.0"),
                        ],
                    }
                ]
            ),
            _common_js(_COMMON_SCHEMAS),
            "duplicate-command:Dup",
            id="duplicate-command",
        ),
        pytest.param(
            'var gCommands = [{"a":1,"a":2}];',
            _common_js(_COMMON_SCHEMAS),
            "duplicate-json-key:command_definitions.js:a",
            id="duplicate-json-key",
        ),
        pytest.param(
            _commands_js(_COMMAND_CATEGORIES).replace("2.0", "NaN", 1),
            _common_js(_COMMON_SCHEMAS),
            "forbidden-constant:NaN",
            id="nan-constant",
        ),
        pytest.param(
            _commands_js([]),
            _common_js(_COMMON_SCHEMAS),
            "commands-envelope",
            id="empty-commands",
        ),
        pytest.param(
            _commands_js(_COMMAND_CATEGORIES),
            _common_js({"Guid": {"type": "string"}}),
            "missing-ElementType-enum",
            id="missing-element-types",
        ),
    ],
)
def test_build_document_refuses_adversarial_inputs(
    monkeypatch: pytest.MonkeyPatch,
    commands_text: str,
    common_text: str,
    expected_code: str,
) -> None:
    _match_input_pins(monkeypatch, commands_text, common_text)
    with pytest.raises(InputRefused) as caught:
        build_document(commands_text.encode("utf-8"), common_text.encode("utf-8"))
    assert caught.value.code == expected_code


def test_nonintegral_float_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    commands_text = _commands_js(
        [
            {
                "name": "C",
                "commands": [
                    {
                        "name": "Scaled",
                        "version": "1.0.0",
                        "description": "Scaled.",
                        "inputScheme": {"type": "number", "multipleOf": 0.5},
                        "outputScheme": None,
                    }
                ],
            }
        ]
    )
    common_text = _common_js(_COMMON_SCHEMAS)
    _match_input_pins(monkeypatch, commands_text, common_text)
    with pytest.raises(InputRefused) as caught:
        build_document(commands_text.encode("utf-8"), common_text.encode("utf-8"))
    assert caught.value.code.startswith("nonintegral-float:")


def test_input_digest_mismatch_refuses_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, common = _input_files(tmp_path)
    output = tmp_path / "tapir.json"

    exit_code = main([str(commands), str(common), "--output", str(output)])

    assert exit_code == 2
    assert not output.exists()
    assert (
        "generation refused: input-sha256-mismatch:command_definitions.js"
        in capsys.readouterr().err
    )


def test_main_writes_deterministic_snapshot_for_matching_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands, common = _input_files(tmp_path)
    monkeypatch.setattr(
        generate_tapir_snapshot,
        "EXPECTED_INPUTS",
        {
            "command_definitions.js": hashlib.sha256(commands.read_bytes()).hexdigest(),
            "common_schema_definitions.js": hashlib.sha256(common.read_bytes()).hexdigest(),
        },
    )
    first = tmp_path / "first" / "tapir.json"
    second = tmp_path / "second" / "tapir.json"

    assert main([str(commands), str(common), "--output", str(first)]) == 0
    assert main([str(commands), str(common), "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert report["commands"] == 3
    assert report["common_schemas"] == 2
