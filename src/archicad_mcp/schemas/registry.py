"""Immutable in-memory capability registry for accepted schema bundle bytes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, NoReturn, TypeAlias, cast

from rapidfuzz import fuzz

ProviderName: TypeAlias = Literal["native", "tapir"]
JsonValue: TypeAlias = bool | int | str | list["JsonValue"] | dict[str, "JsonValue"] | None

_PROVIDERS: Final = frozenset({"native", "tapir"})
_CAMEL_ACRONYM_BOUNDARY: Final = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY: Final = re.compile(r"([a-z0-9])([A-Z])")
_WORD: Final = re.compile(r"[^\W_]+", re.UNICODE)
_SCHEMA_NOISE_KEYS: Final = frozenset(
    {
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)
_UNSUPPORTED_SCHEMA_RESOLUTION_KEYS: Final = frozenset(
    {
        "$anchor",
        "$dynamicAnchor",
        "$dynamicRef",
        "$id",
        "$recursiveAnchor",
        "$recursiveRef",
    }
)
_PROVIDER_SNAPSHOT_MARKER: Final = object()


class SchemaRegistryError(ValueError):
    """Structured validation error raised by the capability registry."""

    def __init__(
        self,
        code: str,
        *,
        missing_definitions: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.missing_definitions = missing_definitions


class ViewStatus(StrEnum):
    """Tapir availability/compatibility state for one immutable capability view."""

    TAPIR_UNAVAILABLE = "tapir_unavailable"
    COMPATIBILITY_UNKNOWN = "compatibility_unknown"
    TAPIR_AVAILABLE = "tapir_available"


@dataclass(frozen=True, slots=True)
class _CapabilityRecord:
    capability_id: str
    provider: ProviderName
    name: str
    provider_version: str
    category: str
    description: str
    snapshot_revision: str
    document_sha256: str
    presentation: bytes = field(repr=False)
    search_terms: tuple[tuple[str, str, int], ...] = field(repr=False)


@dataclass(frozen=True, slots=True, init=False)
class ProviderSnapshot:
    """One validated immutable provider snapshot.

    Construct snapshots with :func:`load_provider_snapshot` so source bytes and
    provider metadata are validated and revisions are derived consistently.
    """

    provider: ProviderName
    provider_version: str
    distribution: str
    provenance: tuple[str, ...]
    source_sha256: str
    revision: str
    command_count: int
    definition_count: int
    _source_presentation: bytes = field(repr=False, compare=False)
    _records: tuple[_CapabilityRecord, ...] = field(repr=False, compare=False)
    _factory_marker: object = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("ProviderSnapshot instances are created by load_provider_snapshot")

    @classmethod
    def _create(
        cls,
        *,
        provider: ProviderName,
        provider_version: str,
        distribution: str,
        provenance: tuple[str, ...],
        source_sha256: str,
        revision: str,
        command_count: int,
        definition_count: int,
        source_presentation: bytes,
        records: tuple[_CapabilityRecord, ...],
    ) -> ProviderSnapshot:
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "provider", provider)
        object.__setattr__(snapshot, "provider_version", provider_version)
        object.__setattr__(snapshot, "distribution", distribution)
        object.__setattr__(snapshot, "provenance", provenance)
        object.__setattr__(snapshot, "source_sha256", source_sha256)
        object.__setattr__(snapshot, "revision", revision)
        object.__setattr__(snapshot, "command_count", command_count)
        object.__setattr__(snapshot, "definition_count", definition_count)
        object.__setattr__(snapshot, "_source_presentation", source_presentation)
        object.__setattr__(snapshot, "_records", records)
        object.__setattr__(snapshot, "_factory_marker", _PROVIDER_SNAPSHOT_MARKER)
        return snapshot

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """Return stable capability IDs in deterministic order."""
        return tuple(record.capability_id for record in self._records)

    def source_data(self) -> dict[str, Any]:
        """Return a fresh JSON-safe copy of the complete accepted provider data."""
        return _decode_object(self._source_presentation)


@dataclass(frozen=True, slots=True)
class CapabilityView:
    """Immutable target-scoped view over native and optional Tapir snapshots."""

    native: ProviderSnapshot
    tapir: ProviderSnapshot | None
    target_identity: str
    status: ViewStatus
    revision: str = field(init=False)
    _records: tuple[_CapabilityRecord, ...] = field(init=False, repr=False, compare=False)
    _by_id: MappingProxyType[str, _CapabilityRecord] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _validate_snapshot_for_view(self.native, expected_provider="native")
        if self.tapir is not None:
            _validate_snapshot_for_view(self.tapir, expected_provider="tapir")
        if type(self.target_identity) is not str or not self.target_identity.strip():
            raise SchemaRegistryError("invalid_target_identity")
        if not isinstance(self.status, ViewStatus):
            raise SchemaRegistryError("invalid_view_status")
        if self.status is ViewStatus.TAPIR_UNAVAILABLE and self.tapir is not None:
            raise SchemaRegistryError("tapir_unavailable_with_snapshot")
        if self.status is ViewStatus.TAPIR_AVAILABLE and self.tapir is None:
            raise SchemaRegistryError("tapir_available_without_snapshot")

        records = list(self.native._records)
        if self.tapir is not None:
            records.extend(self.tapir._records)
        records.sort(key=lambda record: record.capability_id)
        by_id = {record.capability_id: record for record in records}
        if len(by_id) != len(records):
            raise SchemaRegistryError("duplicate_capability_id")

        revision_descriptor: dict[str, Any] = {
            "status": self.status.value,
            "target_identity": self.target_identity,
            "snapshots": [
                ["native", self.native.revision],
                *([["tapir", self.tapir.revision]] if self.tapir is not None else []),
            ],
        }
        object.__setattr__(self, "_records", tuple(records))
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        object.__setattr__(self, "revision", _sha256(_presentation_bytes(revision_descriptor)))

    def summary(self) -> dict[str, Any]:
        """Return target status plus stable provider/category counts."""
        provider_counts = {"native": 0, "tapir": 0}
        category_counts: Counter[str] = Counter()
        for record in self._records:
            provider_counts[record.provider] += 1
            category_counts[record.category] += 1
        categories = [
            {"name": name, "count": count} for name, count in sorted(category_counts.items())
        ]
        return {
            "target_identity": self.target_identity,
            "status": self.status.value,
            "view_revision": self.revision,
            "total": len(self._records),
            "provider_counts": provider_counts,
            "categories": categories,
        }

    def browse(self, page_size: int, cursor: str | None = None) -> dict[str, Any]:
        """Browse every capability exactly once using a view-bound offset cursor."""
        if type(page_size) is not int or page_size <= 0:
            raise SchemaRegistryError("invalid_page_size")
        offset = self._cursor_offset(cursor)
        end = min(offset + page_size, len(self._records))
        capabilities = [self._summary_for(record) for record in self._records[offset:end]]
        next_cursor = f"{self.revision}:{end}" if end < len(self._records) else None
        return {
            "target_identity": self.target_identity,
            "status": self.status.value,
            "view_revision": self.revision,
            "total": len(self._records),
            "capabilities": capabilities,
            "next_cursor": next_cursor,
        }

    def get(self, capability_id: str) -> dict[str, Any] | None:
        """Return a fresh complete closed capability document, if present."""
        if type(capability_id) is not str:
            raise SchemaRegistryError("invalid_capability_id")
        record = self._by_id.get(capability_id)
        if record is None:
            return None
        return _decode_object(record.presentation)

    def document_sha256(self, capability_id: str) -> str | None:
        """Return the deterministic presentation digest for one capability document."""
        if type(capability_id) is not str:
            raise SchemaRegistryError("invalid_capability_id")
        record = self._by_id.get(capability_id)
        return None if record is None else record.document_sha256

    def get_many(self, capability_ids: Sequence[str]) -> dict[str, Any]:
        """Batch retrieve complete documents in requested order and report misses."""
        if isinstance(capability_ids, (str, bytes)):
            raise SchemaRegistryError("invalid_capability_id_batch")
        seen: set[str] = set()
        documents: list[dict[str, Any]] = []
        missing: list[str] = []
        for capability_id in capability_ids:
            if type(capability_id) is not str:
                raise SchemaRegistryError("invalid_capability_id")
            if capability_id in seen:
                raise SchemaRegistryError("duplicate_capability_id")
            seen.add(capability_id)
            record = self._by_id.get(capability_id)
            if record is None:
                missing.append(capability_id)
            else:
                documents.append(_decode_object(record.presentation))
        return {
            "view_revision": self.revision,
            "documents": documents,
            "missing": missing,
        }

    def search(self, query: str) -> dict[str, Any]:
        """Return all deterministic intent-ranked matches without limiting browse."""
        if type(query) is not str:
            raise SchemaRegistryError("invalid_search_query")
        query_tokens = _unique_tokens(query)
        if not query_tokens:
            return {
                "query": query,
                "target_identity": self.target_identity,
                "status": self.status.value,
                "view_revision": self.revision,
                "total": 0,
                "results": [],
            }

        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for record in self._records:
            score, matched_terms, matched_fields = _score_record(record, query_tokens)
            if score <= 0:
                continue
            result = self._summary_for(record)
            result.update(
                {
                    "score": score,
                    "matched_terms": matched_terms,
                    "matched_in": matched_fields,
                    "view_revision": self.revision,
                }
            )
            ranked.append((score, record.capability_id, result))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return {
            "query": query,
            "target_identity": self.target_identity,
            "status": self.status.value,
            "view_revision": self.revision,
            "total": len(ranked),
            "results": [item[2] for item in ranked],
        }

    def _cursor_offset(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        if type(cursor) is not str or not cursor:
            raise SchemaRegistryError("malformed_cursor")
        revision, separator, raw_offset = cursor.partition(":")
        if not separator:
            raise SchemaRegistryError("malformed_cursor")
        if revision != self.revision:
            raise SchemaRegistryError("cursor_view_mismatch")
        if not raw_offset or any(character < "0" or character > "9" for character in raw_offset):
            raise SchemaRegistryError("malformed_cursor_offset")
        if raw_offset[0] == "0":
            raise SchemaRegistryError("malformed_cursor_offset")
        maximum_offset = len(self._records) - 1
        if maximum_offset <= 0:
            raise SchemaRegistryError("malformed_cursor_offset")
        maximum_raw_offset = str(maximum_offset)
        if len(raw_offset) > len(maximum_raw_offset) or (
            len(raw_offset) == len(maximum_raw_offset) and raw_offset > maximum_raw_offset
        ):
            raise SchemaRegistryError("malformed_cursor_offset")
        return int(raw_offset)

    @staticmethod
    def _summary_for(record: _CapabilityRecord) -> dict[str, Any]:
        return {
            "id": record.capability_id,
            "provider": record.provider,
            "name": record.name,
            "provider_version": record.provider_version,
            "snapshot_revision": record.snapshot_revision,
            "document_sha256": record.document_sha256,
            "category": record.category,
            "description": record.description,
        }


def _validate_snapshot_for_view(snapshot: object, *, expected_provider: ProviderName) -> None:
    if not isinstance(snapshot, ProviderSnapshot) or (
        getattr(snapshot, "_factory_marker", None) is not _PROVIDER_SNAPSHOT_MARKER
    ):
        raise SchemaRegistryError("invalid_provider_snapshot")
    if snapshot.provider != expected_provider:
        code = (
            "native_snapshot_required"
            if expected_provider == "native"
            else "tapir_snapshot_required"
        )
        raise SchemaRegistryError(code)
    if type(snapshot._records) is not tuple:
        raise SchemaRegistryError("invalid_provider_snapshot")
    for record in snapshot._records:
        if not isinstance(record, _CapabilityRecord):
            raise SchemaRegistryError("invalid_provider_snapshot")
        if record.provider != snapshot.provider:
            raise SchemaRegistryError("invalid_provider_snapshot")
        if record.capability_id != f"{snapshot.provider}:{record.name}":
            raise SchemaRegistryError("invalid_provider_snapshot")


def load_provider_snapshot(
    source_bytes: bytes,
    *,
    provider: ProviderName,
    provider_version: str,
    distribution: str,
    provenance: Sequence[str],
) -> ProviderSnapshot:
    """Strictly validate exact provider JSON bytes and build an immutable snapshot."""
    if type(source_bytes) is not bytes:
        raise SchemaRegistryError("invalid_source_bytes_type")
    provider_name = _validate_provider(provider)
    provider_version_value = _validate_nonempty_text(provider_version, "invalid_provider_version")
    distribution_value = _validate_nonempty_text(distribution, "invalid_distribution")
    provenance_values = _validate_provenance(provenance)

    root = _parse_json_bytes(source_bytes)
    if type(root) is not dict:
        raise SchemaRegistryError("malformed_provider_shape")
    source_root = cast(dict[str, Any], root)
    commands_value = source_root.get("commands")
    definitions_key = "$defs" if provider_name == "native" else "common_schemas"
    definitions_value = source_root.get(definitions_key)
    if type(commands_value) is not dict or type(definitions_value) is not dict:
        raise SchemaRegistryError("malformed_provider_shape")
    commands = cast(dict[str, Any], commands_value)
    definitions = cast(dict[str, Any], definitions_value)

    for name, command in commands.items():
        if name == "":
            raise SchemaRegistryError("empty_command_name")
        if type(command) is not dict:
            raise SchemaRegistryError("malformed_provider_shape")
    for definition in definitions.values():
        if type(definition) is not dict:
            raise SchemaRegistryError("malformed_provider_shape")

    _reject_unsupported_schema_resolution(commands)
    _reject_unsupported_schema_resolution(definitions)

    source_sha256 = _sha256(source_bytes)
    snapshot_descriptor: dict[str, Any] = {
        "distribution": distribution_value,
        "provider": provider_name,
        "provider_version": provider_version_value,
        "provenance": list(provenance_values),
        "source_sha256": source_sha256,
    }
    snapshot_revision = _sha256(_presentation_bytes(snapshot_descriptor))
    source_presentation = _presentation_bytes(source_root)

    missing: Counter[str] = Counter()
    command_targets: dict[str, frozenset[str]] = {}
    definition_targets: dict[str, frozenset[str]] = {}
    definition_names = frozenset(definitions)
    pending_definitions: set[str] = set()
    for name, command in commands.items():
        targets = _scan_reference_targets(command, provider_name, definition_names, missing)
        command_targets[name] = targets
        pending_definitions.update(target for target in targets if target in definition_names)
    if missing:
        raise SchemaRegistryError(
            "dangling_local_reference",
            missing_definitions=tuple(sorted(missing.items())),
        )

    while pending_definitions:
        name = pending_definitions.pop()
        if name in definition_targets:
            continue
        targets = _scan_reference_targets(
            definitions[name], provider_name, definition_names, missing
        )
        definition_targets[name] = targets
        pending_definitions.update(
            target
            for target in targets
            if target in definition_names and target not in definition_targets
        )
    if missing:
        raise SchemaRegistryError(
            "dangling_local_reference",
            missing_definitions=tuple(sorted(missing.items())),
        )

    for command in commands.values():
        _normalize_refs_in_place(cast(dict[str, Any], command), provider_name)
    for name in definition_targets:
        _normalize_refs_in_place(cast(dict[str, Any], definitions[name]), provider_name)

    records: list[_CapabilityRecord] = []
    for name in sorted(commands):
        command = cast(dict[str, Any], commands[name])
        closure_names = _definition_closure(command_targets[name], definition_targets)
        closure = {
            definition_name: definitions[definition_name] for definition_name in closure_names
        }
        capability_id = f"{provider_name}:{name}"
        document: dict[str, Any] = {
            "id": capability_id,
            "provider": provider_name,
            "name": name,
            "provider_version": provider_version_value,
            "provider_distribution": distribution_value,
            "snapshot_revision": snapshot_revision,
            "source_sha256": source_sha256,
            "provenance": list(provenance_values),
            "command": command,
            "$defs": closure,
        }
        presentation = _presentation_bytes(document)
        category_value = command.get("category")
        category = category_value if type(category_value) is str else "Uncategorized"
        description_value = command.get("description")
        description = description_value if type(description_value) is str else ""
        records.append(
            _CapabilityRecord(
                capability_id=capability_id,
                provider=provider_name,
                name=name,
                provider_version=provider_version_value,
                category=category,
                description=description,
                snapshot_revision=snapshot_revision,
                document_sha256=_sha256(presentation),
                presentation=presentation,
                search_terms=_build_search_terms(name, command, closure),
            )
        )

    records_tuple = tuple(records)
    return ProviderSnapshot._create(
        provider=provider_name,
        provider_version=provider_version_value,
        distribution=distribution_value,
        provenance=provenance_values,
        source_sha256=source_sha256,
        revision=snapshot_revision,
        command_count=len(commands),
        definition_count=len(definitions),
        source_presentation=source_presentation,
        records=records_tuple,
    )


def _validate_provider(provider: object) -> ProviderName:
    if type(provider) is not str or provider not in _PROVIDERS:
        raise SchemaRegistryError("invalid_provider")
    return cast(ProviderName, provider)


def _validate_nonempty_text(value: object, code: str) -> str:
    if type(value) is not str or not value.strip():
        raise SchemaRegistryError(code)
    text = value
    if _has_surrogate(text):
        raise SchemaRegistryError(code)
    return text


def _validate_provenance(provenance: Sequence[str]) -> tuple[str, ...]:
    if isinstance(provenance, (str, bytes)):
        raise SchemaRegistryError("invalid_provenance")
    values: list[str] = []
    for value in provenance:
        values.append(_validate_nonempty_text(value, "invalid_provenance"))
    if not values:
        raise SchemaRegistryError("invalid_provenance")
    return tuple(values)


def _parse_json_bytes(source_bytes: bytes) -> JsonValue:
    try:
        text = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SchemaRegistryError("malformed_utf8") from exc
    try:
        value = cast(
            JsonValue,
            json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_float=_reject_float,
                parse_int=_parse_int,
                parse_constant=_reject_nonfinite,
            ),
        )
    except _DuplicateJsonName as exc:
        raise SchemaRegistryError("duplicate_json_name") from exc
    except _ForbiddenJsonNumber as exc:
        raise SchemaRegistryError(exc.code) from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SchemaRegistryError("malformed_json") from exc
    _validate_json_tree(value)
    return value


class _DuplicateJsonName(ValueError):
    pass


class _ForbiddenJsonNumber(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonName(key)
        result[key] = value
    return result


def _parse_int(raw: str) -> int:
    return int(Decimal(raw))


def _reject_float(_raw: str) -> NoReturn:
    raise _ForbiddenJsonNumber("float_not_allowed")


def _reject_nonfinite(_raw: str) -> NoReturn:
    raise _ForbiddenJsonNumber("nonfinite_not_allowed")


def _validate_json_tree(root: JsonValue) -> None:
    stack: list[object] = [root]
    while stack:
        value = stack.pop()
        if value is None or type(value) in {bool, int}:
            continue
        if type(value) is str:
            if _has_surrogate(value):
                raise SchemaRegistryError("surrogate_not_allowed")
            continue
        if type(value) is list:
            stack.extend(value)
            continue
        if type(value) is dict:
            mapping = cast(dict[str, Any], value)
            for key, child in mapping.items():
                if type(key) is not str:
                    raise SchemaRegistryError("wrong_json_type")
                if _has_surrogate(key):
                    raise SchemaRegistryError("surrogate_not_allowed")
                stack.append(child)
            continue
        raise SchemaRegistryError("wrong_json_type")


def _has_surrogate(text: str) -> bool:
    return any("\ud800" <= character <= "\udfff" for character in text)


def _reject_unsupported_schema_resolution(root: Any) -> None:
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if type(value) is dict:
            mapping = cast(dict[str, Any], value)
            if not _UNSUPPORTED_SCHEMA_RESOLUTION_KEYS.isdisjoint(mapping):
                raise SchemaRegistryError("unsupported_schema_resolution_keyword")
            stack.extend(mapping.values())
        elif type(value) is list:
            stack.extend(value)


def _scan_reference_targets(
    root: Any,
    provider: ProviderName,
    definition_names: frozenset[str],
    missing: Counter[str],
) -> frozenset[str]:
    targets: set[str] = set()
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if type(value) is dict:
            mapping = cast(dict[str, Any], value)
            if "$ref" in mapping:
                target = _reference_target(provider, mapping["$ref"])
                targets.add(target)
                if target not in definition_names:
                    missing[target] += 1
            stack.extend(mapping.values())
        elif type(value) is list:
            stack.extend(value)
    return frozenset(targets)


def _reference_target(provider: ProviderName, reference: object) -> str:
    if type(reference) is not str:
        raise SchemaRegistryError("invalid_ref_type")
    ref = reference
    if not ref.startswith("#/"):
        raise SchemaRegistryError("unsupported_reference")
    raw_tokens = ref[2:].split("/")
    tokens = tuple(_decode_pointer_token(token) for token in raw_tokens)
    if provider == "native":
        if len(tokens) != 2 or tokens[0] != "$defs":
            raise SchemaRegistryError("unsupported_reference")
        return tokens[1]
    if len(tokens) != 1:
        raise SchemaRegistryError("unsupported_reference")
    return tokens[0]


def _decode_pointer_token(token: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(token):
        character = token[index]
        if character != "~":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(token):
            raise SchemaRegistryError("unsupported_reference")
        escape = token[index + 1]
        if escape == "0":
            result.append("~")
        elif escape == "1":
            result.append("/")
        else:
            raise SchemaRegistryError("unsupported_reference")
        index += 2
    return "".join(result)


def _encode_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _normalized_ref(target: str) -> str:
    return f"#/$defs/{_encode_pointer_token(target)}"


def _normalize_refs_in_place(root: Any, provider: ProviderName) -> None:
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if type(value) is dict:
            mapping = cast(dict[str, Any], value)
            if "$ref" in mapping:
                mapping["$ref"] = _normalized_ref(_reference_target(provider, mapping["$ref"]))
            stack.extend(mapping.values())
        elif type(value) is list:
            stack.extend(value)


def _definition_closure(
    roots: frozenset[str],
    definition_targets: dict[str, frozenset[str]],
) -> tuple[str, ...]:
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        pending.extend(definition_targets[name] - visited)
    return tuple(sorted(visited))


def _build_search_terms(
    name: str,
    command: dict[str, Any],
    closure: dict[str, Any],
) -> tuple[tuple[str, str, int], ...]:
    terms: dict[tuple[str, str], int] = {}
    _add_text_terms(terms, name, "name", 100)
    description = command.get("description")
    if type(description) is str:
        _add_text_terms(terms, description, "description", 60)
    category = command.get("category")
    if type(category) is str:
        _add_text_terms(terms, category, "category", 40)
    if "parameters" in command:
        _add_json_terms(terms, command["parameters"], "parameters", 35)
    if "results" in command:
        _add_json_terms(terms, command["results"], "results", 30)
    if "returns" in command:
        _add_json_terms(terms, command["returns"], "results", 30)
    for key in ("example", "examples"):
        if key in command:
            _add_json_terms(terms, command[key], "examples", 20)
    if "notes" in command:
        _add_json_terms(terms, command["notes"], "notes", 20)
    for definition_name, definition in closure.items():
        _add_text_terms(terms, definition_name, "definitions", 18)
        _add_json_terms(terms, definition, "definitions", 18)
    return tuple(
        sorted((token, field_name, weight) for (token, field_name), weight in terms.items())
    )


def _add_json_terms(
    terms: dict[tuple[str, str], int],
    root: Any,
    field_name: str,
    weight: int,
) -> None:
    stack: list[Any] = [root]
    while stack:
        value = stack.pop()
        if type(value) is str:
            _add_text_terms(terms, value, field_name, weight)
        elif type(value) is list:
            stack.extend(value)
        elif type(value) is dict:
            mapping = cast(dict[str, Any], value)
            for key, child in mapping.items():
                if key not in _SCHEMA_NOISE_KEYS:
                    _add_text_terms(terms, key, field_name, weight)
                if key == "$ref":
                    continue
                if key == "enum":
                    _add_json_terms(terms, child, "enum", 32)
                else:
                    stack.append(child)


def _add_text_terms(
    terms: dict[tuple[str, str], int],
    text: str,
    field_name: str,
    weight: int,
) -> None:
    for token in _unique_tokens(text):
        key = (token, field_name)
        terms[key] = max(weight, terms.get(key, 0))


def _unique_tokens(text: str) -> tuple[str, ...]:
    expanded = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1 \2", text)
    expanded = _CAMEL_WORD_BOUNDARY.sub(r"\1 \2", expanded)
    candidates = [*(_WORD.findall(expanded.casefold())), *(_WORD.findall(text.casefold()))]
    return tuple(dict.fromkeys(token for token in candidates if token))


def _score_record(
    record: _CapabilityRecord,
    query_tokens: tuple[str, ...],
) -> tuple[int, list[str], list[str]]:
    score = 0
    matched_terms: list[str] = []
    matched_fields: set[str] = set()
    for query_token in query_tokens:
        best_score = 0
        best_field = ""
        best_indexed_token = ""
        for indexed_token, field_name, weight in record.search_terms:
            strength = _match_strength(query_token, indexed_token)
            candidate = weight * strength // 100
            if candidate > best_score or (
                candidate == best_score
                and candidate > 0
                and (field_name, indexed_token) < (best_field, best_indexed_token)
            ):
                best_score = candidate
                best_field = field_name
                best_indexed_token = indexed_token
        if best_score > 0:
            score += best_score
            matched_terms.append(query_token)
            matched_fields.add(best_field)
    if not matched_terms:
        return 0, [], []
    score = score * len(matched_terms) // len(query_tokens)
    return score, matched_terms, sorted(matched_fields)


def _match_strength(query_token: str, indexed_token: str) -> int:
    if query_token == indexed_token:
        return 100
    if indexed_token.startswith(query_token) or query_token.startswith(indexed_token):
        return 75
    ratio = fuzz.ratio(query_token, indexed_token)
    return int(ratio) * 3 // 5 if ratio >= 80 else 0


def _presentation_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _decode_object(presentation: bytes) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(presentation.decode("utf-8")))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
