"""Schema registry and documentation for Archicad command capabilities."""

from archicad_mcp.schemas.cache import SchemaCache
from archicad_mcp.schemas.docgen import generate_compact_schema
from archicad_mcp.schemas.registry import (
    CapabilityView,
    ProviderSnapshot,
    SchemaRegistryError,
    ViewStatus,
    load_provider_snapshot,
)

__all__ = [
    "CapabilityView",
    "ProviderSnapshot",
    "SchemaCache",
    "SchemaRegistryError",
    "ViewStatus",
    "generate_compact_schema",
    "load_provider_snapshot",
]
