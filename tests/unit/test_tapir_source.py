"""Focused adversarial tests for the shared non-executing Tapir transform."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from archicad_mcp.schemas.registry import load_provider_snapshot
from archicad_mcp.schemas.semver import SAFE_INTEGER_MAXIMUM
from archicad_mcp.schemas.tapir_source import (
    CACHE_DISTRIBUTION,
    GENERATOR_SCRIPT_IDENTITY,
    PINNED_LICENSE_SHA256,
    SNAPSHOT_MAX_BYTES,
    TAPIR_PACKAGED_METADATA_KEYS,
    TapirSnapshotIdentity,
    TapirSnapshotMetadataError,
    TapirSourceError,
    assemble_tapir_document,
    load_packaged_identity,
    load_tapir_identity,
    parse_command_definitions,
    parse_common_schema_definitions,
    require_packaged_provenance,
    serialize_tapir_snapshot,
    sha256_hex,
    snapshot_metadata,
    strict_json_bytes,
    transform_inputs,
    validate_observed_assets,
    validate_snapshot_document,
    validate_source_size,
    validate_tapir_metadata,
    verify_license_identity,
)

UPSTREAM_COMMIT = "ce033d6bdcc90b538b3c5f7ab62f676099b96823"
UPSTREAM_REPOSITORY = "https://github.com/ENZYME-APD/tapir-archicad-automation"

VERSION_COMMAND = {
    "name": "GetAddOnVersion",
    "version": "0.1.0",
    "description": "Version probe.",
    "inputScheme": None,
    "outputScheme": {
        "type": "object",
        "properties": {"version": {"type": "string"}},
        "required": ["version"],
    },
}


def full_command(name: str, version: str, **overrides: object) -> dict[str, object]:
    """One exact five-key upstream command record."""

    command: dict[str, object] = {
        "name": name,
        "version": version,
        "description": f"{name} description.",
        "inputScheme": None,
        "outputScheme": None,
    }
    command.update(overrides)
    return command


def commands_with_version(categories: list[dict[str, object]]) -> list[dict[str, object]]:
    """Prepend the mandatory GetAddOnVersion record to one category set."""

    extended = [dict(category) for category in categories]
    first = extended[0]
    existing_commands = first["commands"]
    assert isinstance(existing_commands, list)
    merged = dict(first)
    merged["commands"] = [dict(VERSION_COMMAND), *existing_commands]
    extended[0] = merged
    return extended


def make_commands_js(commands: list[dict[str, object]], *, category: str = "C") -> bytes:
    return (
        b"var gCommands = "
        + json.dumps([{"name": category, "commands": commands}]).encode("utf-8")
        + b";\n"
    )


def make_common_js(schemas: dict[str, object]) -> bytes:
    return b"var gSchemaDefinitions = " + json.dumps(schemas).encode("utf-8") + b";\n"


def base_common() -> dict[str, object]:
    return {
        "ElementType": {"type": "string", "enum": ["Wall", "Slab"]},
        "Guid": {"type": "string"},
    }


def cache_metadata(
    *,
    provider_version: str = "1.6.0",
    tag: str = "1.6.0",
    commit: str = UPSTREAM_COMMIT,
) -> dict[str, object]:
    return snapshot_metadata(
        provider_version=provider_version,
        distribution=CACHE_DISTRIBUTION,
        package_path="schema-cache/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag=tag,
        upstream_commit=commit,
        license_name="MIT",
        input_hashes={"command_definitions.js": "0" * 64, "common_schema_definitions.js": "0" * 64},
        observed_assets={"majors": [27], "platforms": ["windows"]},
    )


@pytest.mark.parametrize(
    ("data", "expected_code"),
    [
        pytest.param(
            b"const gCommands = [];",
            "input-envelope-prefix:command_definitions.js",
            id="wrong-declaration-prefix",
        ),
        pytest.param(
            b"var gSchemas = {};",
            "input-envelope-prefix:command_definitions.js",
            id="wrong-variable-name",
        ),
        pytest.param(
            b"var gCommands = [1,2] x;",
            "malformed-json:command_definitions.js",
            id="garbage-before-terminator",
        ),
        pytest.param(
            b"var gCommands = [1,2];extra",
            "input-envelope-terminator:command_definitions.js",
            id="data-after-terminator",
        ),
        pytest.param(
            b"var gCommands = [1,2];\n\n",
            "input-envelope-trailing:command_definitions.js",
            id="double-newline-after-terminator",
        ),
        pytest.param(
            b"var gCommands = [1,2]; ",
            "input-envelope-trailing:command_definitions.js",
            id="space-after-terminator",
        ),
        pytest.param(
            b"\xef\xbb\xbfvar gCommands = [1,2];",
            "input-envelope-prefix:command_definitions.js",
            id="bom-breaks-exact-prefix",
        ),
        pytest.param(
            b'var gCommands = ["\xff"];',
            "invalid-utf8:command_definitions.js",
            id="invalid-utf8",
        ),
        pytest.param(
            b"var gCommands = [1,2",
            "input-envelope-terminator:command_definitions.js",
            id="truncation",
        ),
        pytest.param(
            b'var gCommands = {"a": 1};',
            "input-envelope-container:command_definitions.js",
            id="wrong-container-object-for-array",
        ),
        pytest.param(
            b"var gCommands = ;",
            "input-envelope-container:command_definitions.js",
            id="missing-value",
        ),
    ],
)
def test_command_envelope_faults_are_refused(data: bytes, expected_code: str) -> None:
    with pytest.raises(TapirSourceError) as caught:
        parse_command_definitions(data)
    assert caught.value.code.startswith(expected_code)


def test_command_envelope_accepts_documented_whitespace() -> None:
    assert parse_command_definitions(b"var gCommands =  [ 1 , 2 ]  ;") == [1, 2]
    assert parse_command_definitions(b"var gCommands = [1];\r\n") == [1]
    assert parse_common_schema_definitions(b'var gSchemaDefinitions = {"a":1};') == {"a": 1}
    with pytest.raises(TapirSourceError) as caught:
        parse_common_schema_definitions(b"var gSchemaDefinitions = [1];")
    assert caught.value.code == "input-envelope-container:common_schema_definitions.js"


@pytest.mark.parametrize(
    ("text", "expected_code", "extract_refuses"),
    [
        pytest.param('{"a": 1, "a": 2}', "duplicate-json-key:", True, id="duplicate-top-level"),
        pytest.param(
            '{"outer": {"b": 1, "b": 0}}',
            "duplicate-json-key:",
            True,
            id="duplicate-nested",
        ),
        pytest.param("[NaN]", "forbidden-constant:NaN", True, id="nan"),
        pytest.param("[Infinity]", "forbidden-constant:Infinity", True, id="infinity"),
        pytest.param("[-Infinity]", "forbidden-constant:-Infinity", True, id="negative-infinity"),
        pytest.param("{", "malformed-json", True, id="truncated-object"),
        pytest.param(
            '{"n": 9007199254740992}',
            "integer-out-of-range",
            True,
            id="integer-beyond-safe-range",
        ),
        pytest.param(
            '{"n": ' + "1" * 129 + "}",
            "numeric-literal-too-long",
            True,
            id="integer-token-bound",
        ),
        pytest.param(
            '{"n": 0.' + "0" * 129 + "}",
            "numeric-literal-too-long",
            True,
            id="float-token-bound",
        ),
        # Nonintegral floats survive envelope parsing as sentinels and are
        # refused later with command-name paths inside document assembly.
        pytest.param('{"n": 0.5}', "nonintegral-float:", False, id="nonintegral-float"),
    ],
)
def test_strict_json_faults_are_refused(
    text: str, expected_code: str, extract_refuses: bool
) -> None:
    del extract_refuses
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(text.encode("utf-8"))
    assert caught.value.code.startswith(expected_code)


def test_strict_json_normalizes_integral_floats_and_rejects_bom() -> None:
    assert strict_json_bytes(b'{"a": 2.0, "b": [3.00]}') == {"a": 2, "b": [3]}
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b"\xef\xbb\xbf{}")
    assert caught.value.code == "utf8-bom"


def test_float_parsing_uses_exact_decimal_semantics() -> None:
    # Regression: binary-float rounding must never mint integers.
    assert strict_json_bytes(b'{"a": 1e3, "b": 1.5e2}') == {"a": 1000, "b": 150}
    assert strict_json_bytes(b'{"a": 9007199254740991.0}') == {"a": SAFE_INTEGER_MAXIMUM}
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": 9007199254740992.5}')
    assert caught.value.code.startswith("nonintegral-float:")
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": 9007199254740993.0}')
    assert caught.value.code == "integer-out-of-range"
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": -9007199254740992.0}')
    assert caught.value.code == "integer-out-of-range"


def test_float_exponent_boundaries_stay_exact_or_refused() -> None:
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": 1e400}')
    assert caught.value.code == "integer-out-of-range"
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": -1E+400}')
    assert caught.value.code == "integer-out-of-range"
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": 1e309}')
    assert caught.value.code == "integer-out-of-range"
    with pytest.raises(TapirSourceError) as caught:
        strict_json_bytes(b'{"a": 0.30000000000000004}')
    assert caught.value.code.startswith("nonintegral-float:")
    with pytest.raises(TapirSourceError):
        strict_json_bytes(b'{"a": [2.50]}')


def test_envelope_parsing_reports_labelled_structural_codes() -> None:
    with pytest.raises(TapirSourceError) as caught:
        parse_command_definitions(b'var gCommands = [{"a":1,"a":2}];')
    assert caught.value.code == "duplicate-json-key:command_definitions.js:a"
    with pytest.raises(TapirSourceError) as caught:
        parse_common_schema_definitions(b'var gSchemaDefinitions = {"a":};')
    assert caught.value.code.startswith("malformed-json:common_schema_definitions.js")


def test_document_assembly_paths_for_float_faults_match_command_names() -> None:
    # Envelope-level normalization reports positional paths.
    commands_js = make_commands_js(
        [
            {
                "name": "Scaled",
                "version": "1.0.0",
                "description": "Scaled.",
                "inputScheme": {"multipleOf": 0.5},
                "outputScheme": None,
            }
        ]
    )
    with pytest.raises(TapirSourceError) as caught:
        parse_command_definitions(commands_js)
    assert caught.value.code == ("nonintegral-float:[0].commands[0].inputScheme.multipleOf:0.5")
    raw_common = b'var gSchemaDefinitions = {"ElementType": {"enum": ["Wall"]}, "N": {"k": 1.25}};'
    with pytest.raises(TapirSourceError) as caught:
        parse_common_schema_definitions(raw_common)
    assert caught.value.code.startswith("nonintegral-float:N.k")


def test_document_assembly_requires_unique_categories_commands_and_versions() -> None:
    good = commands_with_version([{"name": "C", "commands": [full_command("A", "1.0.0")]}])
    document = assemble_tapir_document(good, base_common(), cache_metadata())
    record = document["commands"]["A"]
    assert record == {
        "category": "C",
        "description": "A description.",
        "version": "1.0.0",
        "parameters": None,
        "returns": None,
        "api": "tapir",
        "name": "A",
    }
    assert document["commands"]["GetAddOnVersion"]["version"] == "0.1.0"
    duplicate_category = [
        {"name": "C", "commands": []},
        {"name": "C", "commands": []},
    ]
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(duplicate_category, base_common(), cache_metadata())
    assert caught.value.code == "category-shape"
    duplicate_command = [
        {
            "name": "C",
            "commands": [
                full_command("A", "1.0.0"),
                full_command("A", "1.1.0"),
            ],
        }
    ]
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(duplicate_command, base_common(), cache_metadata())
    assert caught.value.code == "duplicate-command:A"


@pytest.mark.parametrize(
    ("commands", "expected_code"),
    [
        (
            [full_command("A", "not-a-version")],
            "invalid-command-version:A",
        ),
        ([full_command("", "1.0.0")], "command-name"),
        ([full_command("Bad Name", "1.0.0")], "command-name"),
        (
            [{"version": "1.0.0", "description": "d", "inputScheme": None, "outputScheme": None}],
            "command-shape",
        ),
    ],
)
def test_command_record_faults_are_refused(
    commands: list[dict[str, object]], expected_code: str
) -> None:
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(
            commands_with_version([{"name": "C", "commands": commands}]),
            base_common(),
            cache_metadata(),
        )
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    ("common", "origin", "expected_code"),
    [
        ({"Guid": {"type": "string"}}, "assemble", "missing-ElementType-enum"),
        (
            {"ElementType": {"type": "int"}},
            "assemble",
            "missing-ElementType-enum",
        ),
        (
            {"ElementType": {"enum": ["Wall"]}, "X": {"$ref": "#/Missing"}},
            "closure-command",
            None,
        ),
    ],
)
def test_schema_shape_and_reference_faults(
    common: dict[str, object], origin: str, expected_code: str | None
) -> None:
    if origin == "closure-command":
        commands = [
            full_command("A", "1.0.0", inputScheme={"$ref": "#/Missing"}),
        ]
        with pytest.raises(TapirSourceError) as caught:
            assemble_tapir_document([{"name": "C", "commands": commands}], common, cache_metadata())
        assert caught.value.code.startswith("dangling-reference:")
        return
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document([], common, cache_metadata())
    assert caught.value.code == expected_code


def test_external_references_are_refused_without_execution() -> None:
    commands: list[dict[str, object]] = [
        full_command(
            "A",
            "1.0.0",
            inputScheme={"$ref": "https://evil.example/schema.json"},
        )
    ]
    categories = parse_command_definitions(make_commands_js(commands))
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(categories, base_common(), cache_metadata())
    assert caught.value.code.startswith("external-reference:")


def test_runtime_transform_requires_get_add_on_version_output() -> None:
    commands = make_commands_js([full_command("Other", "1.0.0")])
    metadata = dict(cache_metadata())
    metadata["inputs"] = {
        "command_definitions.js": sha256_hex(commands),
        "common_schema_definitions.js": sha256_hex(make_common_js(base_common())),
    }
    with pytest.raises(TapirSourceError) as caught:
        transform_inputs(commands, make_common_js(base_common()), metadata=metadata)
    assert caught.value.code == "missing-command:GetAddOnVersion"
    good = make_commands_js([dict(VERSION_COMMAND)])
    metadata["inputs"] = {
        "command_definitions.js": sha256_hex(good),
        "common_schema_definitions.js": sha256_hex(make_common_js(base_common())),
    }
    document = transform_inputs(good, make_common_js(base_common()), metadata=metadata)
    assert document["commands"]["GetAddOnVersion"]["returns"]["required"] == ["version"]


@pytest.mark.parametrize(
    ("categories", "expected_code"),
    [
        pytest.param(
            [{"name": "C", "commands": [], "since": "2024"}],
            "category-shape",
            id="unknown-category-key",
        ),
        pytest.param(
            [{"name": "C"}],
            "category-shape",
            id="missing-commands-key",
        ),
        pytest.param(
            commands_with_version(
                [{"name": "C", "commands": [full_command("A", "1.0.0", extra=1)]}]
            ),
            "command-shape",
            id="unknown-command-key",
        ),
        pytest.param(
            commands_with_version(
                [
                    {
                        "name": "C",
                        "commands": [full_command("A", "1.0.0", description=5)],
                    }
                ]
            ),
            "command-shape",
            id="non-string-description",
        ),
        pytest.param(
            commands_with_version(
                [{"name": "C", "commands": [full_command("A", "1.0.0", inputScheme="x")]}]
            ),
            "command-shape",
            id="scheme-not-object-or-null",
        ),
        pytest.param(
            commands_with_version([{"name": "C", "commands": [full_command("A", "9.9.9")]}]),
            "command-version-above-provider:A",
            id="command-above-provider-version",
        ),
    ],
)
def test_upstream_layout_drift_fails_closed(
    categories: list[dict[str, object]], expected_code: str
) -> None:
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(categories, base_common(), cache_metadata())
    assert caught.value.code == expected_code


def test_upstream_layout_clean_equivalents_still_assemble() -> None:
    # Sensitivity: the drifted records minus their drift must assemble cleanly.
    assemble_tapir_document(
        commands_with_version([{"name": "C", "commands": []}]),
        base_common(),
        cache_metadata(),
    )
    assemble_tapir_document(
        commands_with_version([{"name": "C", "commands": [full_command("A", "1.0.0")]}]),
        base_common(),
        cache_metadata(),
    )
    assemble_tapir_document(
        commands_with_version(
            [
                {
                    "name": "C",
                    "commands": [full_command("A", "1.0.0", description="text")],
                }
            ]
        ),
        base_common(),
        cache_metadata(),
    )


def test_get_add_on_version_exact_input_and_output_contract() -> None:
    def transform_with(command: dict[str, Any]) -> dict[str, Any]:
        commands_js = make_commands_js([command])
        common_js = make_common_js(base_common())
        metadata = cache_metadata()
        metadata["inputs"] = {
            "command_definitions.js": sha256_hex(commands_js),
            "common_schema_definitions.js": sha256_hex(common_js),
        }
        return transform_inputs(commands_js, common_js, metadata=metadata)

    non_null_input: dict[str, Any] = dict(VERSION_COMMAND)
    non_null_input["inputScheme"] = {"type": "object"}
    with pytest.raises(TapirSourceError) as caught:
        transform_with(non_null_input)
    assert caught.value.code == "invalid-version-input"

    string_output: dict[str, Any] = dict(VERSION_COMMAND)
    string_output["outputScheme"] = {
        "type": "object",
        "properties": {"version": {"type": "integer"}},
        "required": ["version"],
    }
    with pytest.raises(TapirSourceError) as caught:
        transform_with(string_output)
    assert caught.value.code == "missing-version-output"

    missing_object_type: dict[str, Any] = dict(VERSION_COMMAND)
    missing_object_type["outputScheme"] = {
        "properties": {"version": {"type": "string"}},
        "required": ["version"],
    }
    with pytest.raises(TapirSourceError) as caught:
        transform_with(missing_object_type)
    assert caught.value.code == "missing-version-output"

    document = transform_with(dict(VERSION_COMMAND))
    assert document["commands"]["GetAddOnVersion"].get("parameters") is None


def test_element_type_enum_requires_unique_clean_strings() -> None:
    duplicate = {"ElementType": {"enum": ["Wall", "Wall"]}}
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document([], duplicate, cache_metadata())
    assert caught.value.code == "missing-ElementType-enum"
    blank = {"ElementType": {"enum": ["Wall", " "]}}
    with pytest.raises(TapirSourceError):
        assemble_tapir_document([], blank, cache_metadata())


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        (
            {"upstream_repository": "https://github.com/evil/tapir-archicad-automation"},
            "invalid_tapir_upstream_repository",
        ),
        ({"package_path": "elsewhere/tapir.json"}, "invalid_tapir_package_path"),
        ({"license": "Apache-2.0"}, "invalid_tapir_license"),
        ({"generator": "custom-tool/1"}, "invalid_tapir_generator"),
        ({"upstream_tag": "1.6.1"}, "invalid_tapir_upstream_tag"),
    ],
)
def test_cached_metadata_fixed_provenance_is_enforced(
    overrides: dict[str, object], expected_code: str
) -> None:
    drifted = {**cache_metadata(), **overrides}
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_tapir_metadata(drifted)
    assert caught.value.code == expected_code


def test_json_diagnostics_are_stable_without_parser_messages() -> None:
    hostile_samples = [b"{", b'{"a": 1} trailing', b"[1,", b"var gCommands = [1,2] x;"]
    for sample in hostile_samples:
        with pytest.raises(TapirSourceError) as caught:
            if sample.startswith(b"var"):
                parse_command_definitions(sample)
            else:
                strict_json_bytes(sample)
        code = caught.value.code
        for parser_fragment in ("Expecting", "Extra data", "char ", "line "):
            assert parser_fragment not in code, code


def test_snapshot_metadata_packaged_shape_matches_tracked_file_exactly() -> None:
    packaged = snapshot_metadata(
        provider_version="1.5.8",
        package_path="archicad_mcp/schemas/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.5.8",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": (
                "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8"
            ),
            "common_schema_definitions.js": (
                "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0"
            ),
        },
        generator=GENERATOR_SCRIPT_IDENTITY,
    )
    tracked = json.loads(Path(r"src/archicad_mcp/schemas/tapir.json").read_text(encoding="utf-8"))
    assert list(packaged) == [
        "format",
        "provider",
        "provider_version",
        "package_path",
        "upstream_repository",
        "upstream_tag",
        "upstream_commit",
        "license",
        "inputs",
        "generator",
    ]
    assert packaged == tracked["_metadata"]
    assert frozenset(packaged) == TAPIR_PACKAGED_METADATA_KEYS


def test_metadata_validation_accepts_both_shapes_and_refuses_drift() -> None:
    packaged = snapshot_metadata(
        provider_version="1.5.8",
        package_path="p.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.5.8",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": "0" * 64,
            "common_schema_definitions.js": "0" * 64,
        },
    )
    validate_tapir_metadata(packaged)
    cache = snapshot_metadata(
        provider_version="1.6.0",
        distribution=CACHE_DISTRIBUTION,
        package_path="schema-cache/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.6.0",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": "0" * 64,
            "common_schema_definitions.js": "0" * 64,
        },
    )
    validate_tapir_metadata(cache)
    drifted = dict(cache)
    drifted["distribution"] = "packaged"
    with pytest.raises(TapirSnapshotMetadataError):
        validate_tapir_metadata(drifted)
    extra = dict(cache, extra_key=1)
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_tapir_metadata(extra)
    assert caught.value.code == "unexpected_tapir_metadata_keys"


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"provider_version": "01.0.0"}, "invalid_tapir_provider_version"),
        ({"upstream_tag": "1.6.0-beta"}, "invalid_tapir_upstream_tag"),
        ({"upstream_commit": "ZZZ"}, "invalid_tapir_upstream_commit"),
        ({"package_path": "../escape.json"}, "invalid_tapir_package_path"),
        ({"upstream_repository": "http://insecure"}, "invalid_tapir_upstream_repository"),
    ],
)
def test_metadata_field_faults_are_refused(
    overrides: dict[str, object], expected_code: str
) -> None:
    base = snapshot_metadata(
        provider_version="1.5.8",
        package_path="p.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.5.8",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": "0" * 64,
            "common_schema_definitions.js": "0" * 64,
        },
    )
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_tapir_metadata({**base, **overrides})
    assert caught.value.code == expected_code


def test_tracked_packaged_snapshot_loads_with_exact_identity() -> None:
    payload = Path(r"src/archicad_mcp/schemas/tapir.json").read_bytes()
    version, provenance, identity = load_tapir_identity(payload)
    assert version == "1.5.8"
    assert identity == TapirSnapshotIdentity(
        version="1.5.8",
        distribution="packaged",
        package_path="archicad_mcp/schemas/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.5.8",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        source_sha256=sha256_hex(payload),
        input_hashes={
            "command_definitions.js": (
                "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8"
            ),
            "common_schema_definitions.js": (
                "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0"
            ),
        },
    )
    assert any(entry.startswith("distribution:packaged") for entry in provenance)
    assert load_packaged_identity(payload)[0] == "1.5.8"


def _runtime_document() -> tuple[bytes, bytes, dict[str, object]]:
    commands = make_commands_js([dict(VERSION_COMMAND), full_command("Zed", "1.2.3")])
    common = make_common_js(base_common())
    metadata = cache_metadata()
    metadata["inputs"] = {
        "command_definitions.js": sha256_hex(commands),
        "common_schema_definitions.js": sha256_hex(common),
    }
    return commands, common, metadata


def test_transform_and_serialize_are_deterministic_and_registry_loadable() -> None:
    commands, common, metadata = _runtime_document()
    first = serialize_tapir_snapshot(transform_inputs(commands, common, metadata=dict(metadata)))
    second = serialize_tapir_snapshot(transform_inputs(commands, common, metadata=dict(metadata)))
    assert first == second
    root = json.loads(first.decode("utf-8"))
    assert root["_metadata"]["distribution"] == CACHE_DISTRIBUTION
    snapshot = load_provider_snapshot(
        first,
        provider="tapir",
        provider_version="1.6.0",
        distribution="user-cache schema-cache/tapir.json",
        provenance=(
            "path:schema-cache/tapir.json",
            "tag:1.6.0",
            f"commit:{UPSTREAM_COMMIT}",
            "license:MIT",
            "generator:archicad_mcp.schemas.tapir_source",
        ),
    )
    assert snapshot.command_count == 2
    assert snapshot.definition_count == 2


def test_transform_refuses_input_hash_or_size_drift() -> None:
    commands, common, metadata = _runtime_document()
    tampered_metadata = dict(metadata)
    tampered_metadata["inputs"] = {
        "command_definitions.js": sha256_hex(b"other"),
        "common_schema_definitions.js": sha256_hex(common),
    }
    with pytest.raises(TapirSourceError) as caught:
        transform_inputs(commands, common, metadata=tampered_metadata)
    assert caught.value.code == "input-sha256-mismatch:command_definitions.js"
    with pytest.raises(TapirSourceError) as caught:
        validate_source_size("LICENSE", b"")
    assert caught.value.code == "input-size:LICENSE"
    with pytest.raises(TapirSourceError):
        validate_source_size("command_definitions.js", b"x" * (16 * 1024 * 1024 + 1))


def test_license_identity_fails_closed_on_drift() -> None:
    license_bytes = b"MIT License\n(upstream pinned bytes)\n"
    verify_license_identity(license_bytes, sha256_hex(license_bytes))
    with pytest.raises(TapirSourceError) as caught:
        verify_license_identity(license_bytes + b"\n", PINNED_LICENSE_SHA256)
    assert caught.value.code == "license-identity-drift"


def test_serialize_refuses_non_document_shapes() -> None:
    with pytest.raises(TapirSourceError) as caught:
        serialize_tapir_snapshot({"commands": {}})
    assert caught.value.code.startswith("validation-failed:")
    with pytest.raises(TapirSourceError):
        serialize_tapir_snapshot({"commands": [], "element_types": [], "_metadata": {}})


def test_json_pointer_refs_require_one_token_with_rfc6901_escaping() -> None:
    slash_named = {
        "ElementType": {"enum": ["Wall"]},
        "A/B": {"type": "string"},
    }
    commands = [
        full_command("Ref", "1.0.0", inputScheme={"$ref": "#/A~1B"}),
    ]
    document = assemble_tapir_document(
        commands_with_version([{"name": "C", "commands": commands}]),
        slash_named,
        cache_metadata(),
    )
    # Refs are preserved verbatim for registry-time resolution; assembly
    # proves closure by accepting the decoded one-token target.
    assert document["commands"]["Ref"]["parameters"] == {"$ref": "#/A~1B"}
    for bad_ref in ("#/", "#/A/B", "#/A~", "#/A~2B", "#A"):
        drifted = [
            full_command("Ref", "1.0.0", inputScheme={"$ref": bad_ref}),
        ]
        with pytest.raises(TapirSourceError) as caught:
            assemble_tapir_document(
                commands_with_version([{"name": "C", "commands": drifted}]),
                base_common(),
                cache_metadata(),
            )
        assert caught.value.code.startswith(("invalid-json-pointer:", "external-reference:"))
    dangling = [full_command("Ref", "1.0.0", inputScheme={"$ref": "#/Missing~1X"})]
    with pytest.raises(TapirSourceError) as caught:
        assemble_tapir_document(
            commands_with_version([{"name": "C", "commands": dangling}]),
            base_common(),
            cache_metadata(),
        )
    assert caught.value.code == "dangling-reference:command:Missing/X"


@pytest.mark.parametrize(
    ("assets", "valid"),
    [
        ({"majors": [], "platforms": []}, True),
        ({"majors": [27, 28], "platforms": ["macos", "windows"]}, True),
        ({"majors": [28, 27], "platforms": []}, False),
        ({"majors": [27, 27], "platforms": []}, False),
        ({"majors": [0], "platforms": []}, False),
        ({"majors": [10000], "platforms": []}, False),
        ({"majors": [True], "platforms": []}, False),
        ({"majors": [27], "platforms": ["linux"]}, False),
        ({"majors": [27], "platforms": ["windows", "windows"]}, False),
        ({"majors": 27, "platforms": []}, False),
        ({"majors": [], "platforms": "windows"}, False),
        ({"majors": [1.5], "platforms": []}, False),
    ],
)
def test_observed_assets_record_is_validated_exactly(
    assets: dict[str, object], valid: bool
) -> None:
    if valid:
        assert validate_observed_assets(assets) == assets
        return
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_observed_assets(assets)
    assert caught.value.code == "invalid_tapir_observed_assets"


def _cache_document_bytes(metadata: dict[str, object]) -> bytes:
    commands_js = make_commands_js([dict(VERSION_COMMAND)])
    common_js = make_common_js(base_common())
    document = transform_inputs(commands_js, common_js, metadata=dict(metadata))
    return serialize_tapir_snapshot(document)


def _valid_cache_metadata() -> dict[str, object]:
    metadata = cache_metadata()
    metadata["inputs"] = {
        "command_definitions.js": sha256_hex(make_commands_js([dict(VERSION_COMMAND)])),
        "common_schema_definitions.js": sha256_hex(make_common_js(base_common())),
    }
    return metadata


def test_strict_reader_requires_canonical_bytes_and_exact_shape() -> None:
    payload = _cache_document_bytes(_valid_cache_metadata())
    validate_snapshot_document(payload)
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_snapshot_document(b" " * (SNAPSHOT_MAX_BYTES + 1))
    assert caught.value.code == "snapshot-size"
    compact = json.loads(payload.decode("utf-8"))
    noncanonical = json.dumps(compact, separators=(",", ":")).encode("utf-8")
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_snapshot_document(noncanonical)
    assert caught.value.code == "noncanonical_snapshot_bytes"
    float_payload = json.loads(payload.decode("utf-8"))
    float_payload["common_schemas"]["Guid"]["minLength"] = 2.0
    refloat = json.dumps(float_payload, indent=2, ensure_ascii=False) + "\n"
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        # 2.0 normalizes to int 2, so canonical re-serialization differs.
        validate_snapshot_document(refloat.encode("utf-8"))
    assert caught.value.code == "noncanonical_snapshot_bytes"

    invalid_command = json.loads(payload.decode("utf-8"))
    invalid_command["commands"]["GetAddOnVersion"]["api"] = "native"
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_snapshot_document(
            (json.dumps(invalid_command, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
    assert caught.value.code == "invalid_snapshot_commands"
    extra_root = json.loads(payload.decode("utf-8"))
    extra_root["extra"] = 1
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_snapshot_document(
            (json.dumps(extra_root, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
    assert caught.value.code == "unexpected_snapshot_keys"
    projected = json.loads(payload.decode("utf-8"))
    projected["element_types"] = ["Wall", "Other"]
    with pytest.raises(TapirSnapshotMetadataError) as caught:
        validate_snapshot_document(
            (json.dumps(projected, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
    assert caught.value.code == "inconsistent_element_types"

    oversized = json.loads(payload.decode("utf-8"))
    oversized["common_schemas"]["Guid"]["description"] = "x" * SNAPSHOT_MAX_BYTES
    with pytest.raises(TapirSourceError) as source_error:
        serialize_tapir_snapshot(oversized)
    assert source_error.value.code == "snapshot-size"


def test_packaged_provenance_pins_fail_closed() -> None:
    base = snapshot_metadata(
        provider_version="1.5.8",
        package_path="archicad_mcp/schemas/tapir.json",
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag="1.5.8",
        upstream_commit=UPSTREAM_COMMIT,
        license_name="MIT",
        input_hashes={
            "command_definitions.js": (
                "b88152c8276a0a913d8a8e9a33671348de13953dc2654e0e0e855ebac6e096e8"
            ),
            "common_schema_definitions.js": (
                "2f003f9cbcb2fa5b177d74091f9578e16890498e4cbea28971d37801e876e2a0"
            ),
        },
        generator=GENERATOR_SCRIPT_IDENTITY,
    )
    require_packaged_provenance(base)
    for overrides, expected_code in [
        ({"upstream_commit": "f" * 40}, "unpinned_packaged_provenance"),
        ({"provider_version": "1.5.9"}, "unpinned_packaged_provenance"),
        ({"generator": GENERATOR_SCRIPT_IDENTITY + "/x"}, "unpinned_packaged_provenance"),
        ({"license": "Apache-2.0"}, "unpinned_packaged_provenance"),
    ]:
        drifted = {**base, **overrides}
        with pytest.raises(TapirSnapshotMetadataError) as caught:
            require_packaged_provenance(drifted)
        assert caught.value.code == expected_code
