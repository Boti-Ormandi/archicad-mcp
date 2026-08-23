"""Schema cache for Archicad command documentation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from archicad_mcp.schemas.search import SearchIndex

logger = logging.getLogger(__name__)


class SchemaCache:
    """Loads and searches command schemas for documentation."""

    def __init__(self) -> None:
        self.commands: dict[str, dict[str, Any]] = {}
        self.categories: list[str] = []
        self.element_types: list[str] = []
        self.common_schemas: dict[str, Any] = {}  # Tapir $ref resolution (#/Name)
        self.builtin_defs: dict[str, Any] = {}  # Built-in $ref resolution (#/$defs/Name)
        self._search_index: SearchIndex | None = None
        self._loaded = False

    def load_embedded(self) -> None:
        """Load embedded schema files from package."""
        if self._loaded:
            return

        schema_dir = Path(__file__).parent

        # Load Tapir schema
        tapir_path = schema_dir / "tapir.json"
        if tapir_path.exists():
            with open(tapir_path, encoding="utf-8") as f:
                data = json.load(f)
                for name, cmd in data.get("commands", {}).items():
                    cmd["api"] = "tapir"
                    cmd["name"] = name
                    self.commands[name] = cmd
                # Get element types from Tapir schema
                self.element_types = data.get("element_types", [])
                # Load common schemas for $ref resolution
                self.common_schemas = data.get("common_schemas", {})
        else:
            logger.warning("Embedded Tapir schema is missing")

        # Load Built-in API schema
        builtin_path = schema_dir / "builtin.json"
        if builtin_path.exists():
            with open(builtin_path, encoding="utf-8") as f:
                data = json.load(f)
                for name, cmd in data.get("commands", {}).items():
                    cmd["api"] = "builtin"
                    cmd["name"] = name
                    self.commands[name] = cmd
                self.builtin_defs = data.get("$defs", {})
        else:
            logger.warning("Embedded builtin schema is missing")

        # Build category list
        self.categories = sorted(
            {cmd.get("category", "Uncategorized") for cmd in self.commands.values()}
        )

        # Build search index — pass ref schemas so enum values behind $refs get indexed
        self._search_index = SearchIndex()
        ref_schemas = {**self.common_schemas, **self.builtin_defs}
        self._search_index.build(self.commands, self.element_types, ref_schemas)

        self._loaded = True

    def get_command(self, name: str) -> dict[str, Any] | None:
        """Get detailed docs for a specific command.

        Args:
            name: Command name (e.g., "CreateColumns" or "API.GetAllElements")

        Returns:
            Full command schema with parameters, returns, examples, or None if not found.
            $ref references are resolved to show actual enum values.
        """
        self._ensure_loaded()
        cmd = self.commands.get(name)
        if cmd is None:
            return None
        # Resolve $refs so AI can see actual enum values
        resolved: dict[str, Any] = self._resolve_refs(cmd)
        return resolved

    def get_commands(self, names: list[str]) -> dict[str, Any]:
        """Get detailed docs for multiple commands.

        Args:
            names: List of command names

        Returns:
            Dict with 'commands' list and 'not_found' list.
        """
        self._ensure_loaded()
        found = []
        not_found = []

        for name in names:
            cmd = self.commands.get(name)
            if cmd:
                found.append(cmd)
            else:
                not_found.append(name)

        result: dict[str, Any] = {"commands": found}
        if not_found:
            result["not_found"] = not_found
        return result

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Search commands using full-text search.

        Searches across command names, descriptions, parameters, examples, and notes.
        Supports exact matching, prefix matching, and fuzzy matching for typos.
        Detects element types and provides usage hints.

        Args:
            query: Search term (e.g., "wall", "create slab", "property")
            limit: Maximum number of results (default 20)

        Returns:
            Dict with query, total, element_type_hint (if detected), and results.
        """
        self._ensure_loaded()
        if self._search_index is None:
            return {"error": "Search index not initialized"}
        return self._search_index.search(query, limit)

    def get_category(self, category: str) -> dict[str, Any]:
        """Get all commands in a category.

        Args:
            category: Category name (e.g., "Element Commands")

        Returns:
            Dict with category name, total count, and commands list.
            If category not found, includes suggestion with similar names.
        """
        self._ensure_loaded()
        matches: list[dict[str, Any]] = []

        for name, cmd in self.commands.items():
            if cmd.get("category") == category:
                matches.append(
                    {
                        "name": name,
                        "api": cmd.get("api"),
                        "description": cmd.get("description"),
                        "has_details": "parameters" in cmd,
                    }
                )

        matches.sort(key=lambda x: str(x["name"]))

        result: dict[str, Any] = {
            "query": {"category": category},
            "category": category,
            "total": len(matches),
            "commands": matches,
        }

        if not matches:
            similar = self._find_similar_categories(category)
            result["suggestion"] = (
                f"Did you mean: {', '.join(similar)}?"
                if similar
                else "Use get_docs() to see all categories."
            )

        return result

    def _find_similar_categories(self, query: str) -> list[str]:
        """Find categories similar to query for typo recovery.

        Uses substring match, prefix match, then fuzzy fallback.
        """
        query_lower = query.lower()
        suggestions: list[str] = []

        for cat in self.categories:
            cat_lower = cat.lower()
            if query_lower in cat_lower or cat_lower.startswith(query_lower[:3]):
                suggestions.append(cat)

        if not suggestions:
            try:
                from rapidfuzz import fuzz

                for cat in self.categories:
                    if fuzz.ratio(query_lower, cat.lower()) >= 70:
                        suggestions.append(cat)
            except ImportError:
                pass

        return sorted(suggestions)[:3]

    def find_similar_commands(self, query: str, limit: int = 3) -> list[str]:
        """Find command names similar to query using fuzzy matching.

        Unlike search(), this compares directly against command names
        without tokenization — better for CamelCase command name typos.
        """
        try:
            from rapidfuzz import fuzz
        except ImportError:
            return []

        query_lower = query.lower()
        scored: list[tuple[str, float]] = []

        for name in self.commands:
            ratio = fuzz.ratio(query_lower, name.lower())
            if ratio >= 40:
                scored.append((name, ratio))

        scored.sort(key=lambda x: -x[1])
        return [name for name, _ in scored[:limit]]

    def get_summary(self) -> dict[str, Any]:
        """Get overview of all available commands.

        Returns:
            Dict with total count, categories with counts, and element types.
        """
        self._ensure_loaded()

        # Count commands per category
        category_counts: dict[str, int] = {}
        for cmd in self.commands.values():
            cat = cmd.get("category", "Uncategorized")
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # Count by API
        tapir_count = sum(1 for c in self.commands.values() if c.get("api") == "tapir")
        builtin_count = sum(1 for c in self.commands.values() if c.get("api") == "builtin")

        return {
            "total_commands": len(self.commands),
            "tapir_commands": tapir_count,
            "builtin_commands": builtin_count,
            "categories": category_counts,
            "element_types": self.element_types,
            "tip": "Use get_docs(category='...') to browse commands in a category",
        }

    def _ensure_loaded(self) -> None:
        """Ensure schemas are loaded."""
        if not self._loaded:
            self.load_embedded()

    def _resolve_refs(self, obj: Any, depth: int = 0) -> Any:
        """Recursively resolve $ref references in schema objects.

        Args:
            obj: Schema object (dict, list, or primitive)
            depth: Current recursion depth (limited to prevent infinite loops)

        Returns:
            Object with $refs resolved to their definitions.
        """
        if depth > 10:  # Prevent infinite recursion
            return obj

        if isinstance(obj, dict):
            # Check if this contains a $ref
            if "$ref" in obj:
                ref_path = obj["$ref"]
                resolved_schema = None

                if ref_path.startswith("#/$defs/"):
                    # Built-in API format: #/$defs/Name
                    ref_name = ref_path[8:]  # Strip "#/$defs/"
                    if ref_name in self.builtin_defs:
                        resolved_schema = self.builtin_defs[ref_name].copy()
                elif ref_path.startswith("#/"):
                    # Tapir format: #/Name
                    ref_name = ref_path[2:]  # Strip "#/"
                    if ref_name in self.common_schemas:
                        resolved_schema = self.common_schemas[ref_name].copy()

                if resolved_schema is not None:
                    # Merge sibling fields (e.g. description) with resolved schema
                    siblings = {k: v for k, v in obj.items() if k != "$ref"}
                    resolved = self._resolve_refs(resolved_schema, depth + 1)
                    if siblings:
                        resolved = {**resolved, **siblings}
                    return resolved
                # Return original if can't resolve
                return obj

            # Recurse into dict values
            return {k: self._resolve_refs(v, depth + 1) for k, v in obj.items()}

        if isinstance(obj, list):
            return [self._resolve_refs(item, depth + 1) for item in obj]

        return obj
