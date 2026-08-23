#!/usr/bin/env python3
"""Regenerate the packaged Tapir snapshot from authoritative upstream inputs.

This is a release-only maintainer tool, not an end-user sync CLI. It reads the
Tapir add-on's own generated documentation JavaScript for exactly one release
and rewrites ``src/archicad_mcp/schemas/tapir.json`` deterministically:

    uv run python scripts/generate_tapir_snapshot.py \
        command_definitions.js common_schema_definitions.js \
        --output src/archicad_mcp/schemas/tapir.json

The tool performs no Git, submodule, network, or package operations. Inputs
must be the exact files published for the release recorded below; any other
bytes are refused by SHA-256 before parsing. All parsing, adversarial
validation, normalization, and registry-backed serialization come from the
shared package transform in ``archicad_mcp.schemas.tapir_source``; JavaScript
is never executed. The emitted document embeds its own provenance under
``_metadata``, which the server validates at startup.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from archicad_mcp.schemas.tapir_source import (
    GENERATOR_SCRIPT_IDENTITY,
    PACKAGED_INPUT_SHA256,
    PACKAGED_PACKAGE_PATH,
    PACKAGED_PROVIDER_VERSION,
    PACKAGED_UPSTREAM_COMMIT,
    PINNED_LICENSE_NAME,
    TAPIR_UPSTREAM_REPOSITORY,
    TapirSourceError,
    serialize_tapir_snapshot,
    sha256_hex,
    snapshot_metadata,
    transform_inputs,
)

PROVIDER_VERSION = PACKAGED_PROVIDER_VERSION
PACKAGE_PATH = PACKAGED_PACKAGE_PATH
UPSTREAM_REPOSITORY = TAPIR_UPSTREAM_REPOSITORY
UPSTREAM_TAG = PACKAGED_PROVIDER_VERSION
UPSTREAM_COMMIT = PACKAGED_UPSTREAM_COMMIT
LICENSE = PINNED_LICENSE_NAME

EXPECTED_INPUTS = dict(PACKAGED_INPUT_SHA256)


class InputRefused(ValueError):
    """An input or derived document violated the deterministic contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256(data: bytes) -> str:
    return sha256_hex(data)


def _verify_input(path: Path, expected_name: str) -> bytes:
    data = path.read_bytes()
    digest = _sha256(data)
    if EXPECTED_INPUTS[expected_name] != digest:
        raise InputRefused(
            f"input-sha256-mismatch:{expected_name}:expected="
            f"{EXPECTED_INPUTS[expected_name]}:actual={digest}"
        )
    return data


def metadata() -> dict[str, Any]:
    """Return the authoritative provenance record embedded in the snapshot."""

    return snapshot_metadata(
        provider_version=PROVIDER_VERSION,
        package_path=PACKAGE_PATH,
        upstream_repository=UPSTREAM_REPOSITORY,
        upstream_tag=UPSTREAM_TAG,
        upstream_commit=UPSTREAM_COMMIT,
        license_name=LICENSE,
        input_hashes=dict(EXPECTED_INPUTS),
        generator=GENERATOR_SCRIPT_IDENTITY,
    )


def build_document(command_definitions_js: bytes, common_schemas_js: bytes) -> dict[str, Any]:
    """Build the deterministic snapshot document from exact upstream bytes.

    The maintainer tool uses exactly the same full-consuming strict transform
    as the runtime updater (``transform_inputs``): exact ``var gCommands =`` /
    ``var gSchemaDefinitions =`` envelopes, strict JSON, declared input-hash
    pins, shapes, versions, reference closure, and the GetAddOnVersion
    contract. No looser extraction path exists.
    """

    try:
        return transform_inputs(command_definitions_js, common_schemas_js, metadata=metadata())
    except TapirSourceError as exc:
        raise InputRefused(exc.code) from None


def serialize_document(document: dict[str, Any]) -> str:
    """Validate the exact emitted bytes through the product loaders before writing."""

    try:
        return serialize_tapir_snapshot(document).decode("utf-8")
    except (TapirSourceError, ValueError) as exc:
        code = getattr(exc, "code", f"validation-failed:{exc}")
        raise InputRefused(code) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command_definitions", type=Path)
    parser.add_argument("common_schemas", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = build_document(
            _verify_input(args.command_definitions, "command_definitions.js"),
            _verify_input(args.common_schemas, "common_schema_definitions.js"),
        )
        text = serialize_document(document)
    except (InputRefused, OSError) as exc:
        code = getattr(exc, "code", f"input-unreadable:{exc}")
        sys.stderr.write(f"generation refused: {code}\n")
        return 2
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    payload = output.read_bytes()
    sys.stdout.write(
        json.dumps(
            {
                "output": str(output),
                "bytes": len(payload),
                "sha256": _sha256(payload),
                "commands": len(document["commands"]),
                "common_schemas": len(document["common_schemas"]),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
