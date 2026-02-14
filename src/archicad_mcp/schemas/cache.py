"""Schema cache for Archicad command documentation."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archicad_mcp.schemas.search import SearchIndex

if TYPE_CHECKING:
    from archicad_mcp.core.connection import ArchicadConnection

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
            logger.warning("Embedded Tapir schemas not found: %s", tapir_path)

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
            logger.warning("Embedded builtin schemas not found: %s", builtin_path)

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

    async def load_from_tapir(self, conn: ArchicadConnection) -> bool:
        """Load schemas directly from running Tapir instance.

        Calls GenerateDocumentation command and parses the output.
        On success, updates the cached tapir.json for future fallback.

        Args:
            conn: Active connection to Archicad with Tapir installed.

        Returns:
            True if schemas were loaded successfully, False otherwise.
        """
        try:
            # Generate docs to temp directory
            with tempfile.TemporaryDirectory() as tmpdir:
                result = await conn.execute(
                    "GenerateDocumentation",
                    {"destinationFolder": tmpdir},
                )

                if not result.get("success", False):
                    logger.warning("GenerateDocumentation returned failure")
                    return False

                # Parse generated files
                tmppath = Path(tmpdir)
                commands_file = tmppath / "command_definitions.js"
                schemas_file = tmppath / "common_schema_definitions.js"

                if not commands_file.exists():
                    logger.warning("command_definitions.js not generated")
                    return False

                # Parse JS files (format: "var gCommands = [...];" or "var gSchemaDefinitions = {...};")
                commands_data = self._parse_js_var(commands_file.read_text(encoding="utf-8"))
                common_schemas = {}
                if schemas_file.exists():
                    common_schemas = self._parse_js_var(schemas_file.read_text(encoding="utf-8"))

                if not commands_data:
                    logger.warning("Failed to parse command_definitions.js")
                    return False

                # Convert to our format
                tapir_commands = self._convert_tapir_docs(commands_data, common_schemas)

                # Update in-memory cache
                for name, cmd in tapir_commands.items():
                    cmd["api"] = "tapir"
                    cmd["name"] = name
                    self.commands[name] = cmd

                # Save to disk cache for fallback
                self._save_tapir_cache(tapir_commands, common_schemas)

                # Rebuild search index
                self._rebuild_index()

                logger.info(f"Loaded {len(tapir_commands)} Tapir commands from live instance")
                return True

        except Exception as e:
            logger.warning(f"Failed to load schemas from Tapir: {e}")
            return False

    def _parse_js_var(self, content: str) -> Any:
        """Parse JavaScript variable assignment to extract JSON value."""
        # Match: var gCommands = [...]; or var gSchemaDefinitions = {...};
        match = re.search(r"var\s+\w+\s*=\s*(.+);?\s*$", content, re.DOTALL)
        if not match:
            return None

        json_str = match.group(1).rstrip(";").strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _convert_tapir_docs(
        self,
        commands_data: list[dict[str, Any]],
        common_schemas: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Convert Tapir's GenerateDocumentation output to our format."""
        result: dict[str, dict[str, Any]] = {}

        for group in commands_data:
            category = group.get("name", "Uncategorized")
            for cmd in group.get("commands", []):
                name = cmd.get("name")
                if not name:
                    continue

                converted: dict[str, Any] = {
                    "category": category,
                    "description": cmd.get("description", ""),
                    "version": cmd.get("version", ""),
                }

                # Add input schema if present
                if cmd.get("inputScheme"):
                    converted["parameters"] = cmd["inputScheme"]

                # Add output schema if present
                if cmd.get("outputScheme"):
                    converted["returns"] = cmd["outputScheme"]

                result[name] = converted

        return result

    def _save_tapir_cache(
        self,
        commands: dict[str, dict[str, Any]],
        common_schemas: dict[str, Any],
    ) -> None:
        """Save Tapir schemas to disk for fallback."""
        try:
            schema_dir = Path(__file__).parent
            cache_path = schema_dir / "tapir.json"

            # Derive element types from common schemas
            element_type_def = common_schemas.get("ElementType", {})
            element_types = element_type_def.get("enum", [])

            cache_data = {
                "commands": commands,
                "element_types": element_types,
                "common_schemas": common_schemas,
                "_generated": "auto-generated from Tapir GenerateDocumentation",
            }

            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)

            logger.info(f"Saved Tapir schema cache to {cache_path}")
        except Exception as e:
            logger.warning(f"Failed to save Tapir cache: {e}")

    def _rebuild_index(self) -> None:
        """Rebuild search index after loading new schemas."""
        # Rebuild category list
        self.categories = sorted(
            {cmd.get("category", "Uncategorized") for cmd in self.commands.values()}
        )

        # Rebuild search index — pass ref schemas so enum values behind $refs get indexed
        self._search_index = SearchIndex()
        ref_schemas = {**self.common_schemas, **self.builtin_defs}
        self._search_index.build(self.commands, self.element_types, ref_schemas)

        self._loaded = True

    def sync_from_repo(self, repo_path: Path) -> bool:
        """Sync schemas from a local repo clone (auto-detects repo type).

        Detects whether the path is a Tapir or multiconn repo based on
        directory contents and runs the appropriate sync.

        Args:
            repo_path: Path to the repo root.

        Returns:
            True if schemas were synced successfully.
        """
        tapir_marker = repo_path / "docs" / "archicad-addon" / "command_definitions.js"
        builtin_marker = (
            repo_path / "code_generation" / "official" / "schema" / "_command_details.json"
        )

        if tapir_marker.exists():
            return self._sync_tapir_from_repo(repo_path)
        if builtin_marker.exists():
            return self._sync_builtin_from_repo(repo_path)

        logger.error(
            f"Cannot detect repo type at {repo_path}. Expected one of:\n"
            f"  Tapir: docs/archicad-addon/command_definitions.js\n"
            f"  Built-in: code_generation/official/schema/_command_details.json"
        )
        return False

    def _sync_tapir_from_repo(self, repo_path: Path) -> bool:
        """Sync Tapir schemas from a local repo clone."""
        docs_dir = repo_path / "docs" / "archicad-addon"
        commands_file = docs_dir / "command_definitions.js"
        schemas_file = docs_dir / "common_schema_definitions.js"

        if not commands_file.exists():
            logger.error(f"Not found: {commands_file}")
            return False

        commands_data = self._parse_js_var(commands_file.read_text(encoding="utf-8"))
        if not commands_data:
            logger.error("Failed to parse command_definitions.js")
            return False

        common_schemas: dict[str, Any] = {}
        if schemas_file.exists():
            common_schemas = self._parse_js_var(schemas_file.read_text(encoding="utf-8")) or {}

        tapir_commands = self._convert_tapir_docs(commands_data, common_schemas)

        # Update in-memory
        for name, cmd in tapir_commands.items():
            cmd["api"] = "tapir"
            cmd["name"] = name
            self.commands[name] = cmd
        self.common_schemas = common_schemas

        # Save to disk
        self._save_tapir_cache(tapir_commands, common_schemas)
        self._rebuild_index()

        logger.info(f"Synced {len(tapir_commands)} Tapir commands from {repo_path}")
        return True

    def _sync_builtin_from_repo(self, repo_path: Path) -> bool:
        """Sync built-in API schemas from a local multiconn repo clone.

        Reads _command_details.json (names, descriptions, groups) and
        official_api_master_schema.json (full parameter/return schemas),
        merges them into builtin.json.
        """
        schema_dir = repo_path / "code_generation" / "official" / "schema"
        details_file = schema_dir / "_command_details.json"
        master_file = schema_dir / "official_api_master_schema.json"

        if not details_file.exists():
            logger.error(f"Not found: {details_file}")
            return False

        with open(details_file, encoding="utf-8") as f:
            details: list[dict[str, Any]] = json.load(f)

        # Load master schema for full parameter/return definitions
        master_defs: dict[str, Any] = {}
        if master_file.exists():
            with open(master_file, encoding="utf-8") as f:
                master = json.load(f)
                master_defs = master.get("$defs", {})
        else:
            logger.warning(
                f"Master schema not found: {master_file} (proceeding without full schemas)"
            )

        commands: dict[str, dict[str, Any]] = {}
        for entry in details:
            name = entry.get("name", "")
            if not name:
                continue

            cmd: dict[str, Any] = {
                "category": entry.get("group", "Uncategorized"),
                "description": entry.get("description", ""),
            }

            # Map API.Foo -> FooParameters / FooResult in $defs
            short_name = name.removeprefix("API.")
            params_key = f"{short_name}Parameters"
            result_key = f"{short_name}Result"

            if params_key in master_defs:
                cmd["parameters"] = master_defs[params_key]
            if result_key in master_defs:
                cmd["returns"] = master_defs[result_key]

            commands[name] = cmd

        # Save to disk
        self._save_builtin_cache(commands, master_defs)

        # Update in-memory
        for name, cmd in commands.items():
            cmd["api"] = "builtin"
            cmd["name"] = name
            self.commands[name] = cmd
        self._rebuild_index()

        logger.info(f"Synced {len(commands)} built-in API commands from {repo_path}")
        return True

    def _save_builtin_cache(
        self,
        commands: dict[str, dict[str, Any]],
        defs: dict[str, Any],
    ) -> None:
        """Save built-in API schemas to disk."""
        try:
            from datetime import datetime

            schema_dir = Path(__file__).parent
            cache_path = schema_dir / "builtin.json"

            # Collect shared $defs referenced by commands (not the Parameters/Result ones)
            param_result_suffixes = ("Parameters", "Result")
            shared_defs = {k: v for k, v in defs.items() if not k.endswith(param_result_suffixes)}

            cache_data: dict[str, Any] = {
                "version": "2.0.0",
                "updated": datetime.now(UTC).strftime("%Y-%m-%d"),
                "commands": commands,
            }
            if shared_defs:
                cache_data["$defs"] = shared_defs

            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)

            logger.info(f"Saved builtin schema cache to {cache_path}")
        except Exception as e:
            logger.warning(f"Failed to save builtin cache: {e}")


def sync_cli() -> None:
    """CLI entry point for syncing schemas from upstream repo clones.

    Auto-detects repo type based on directory contents:
      - Tapir: docs/archicad-addon/command_definitions.js
      - Built-in: code_generation/official/schema/_command_details.json

    Usage: archicad-mcp-sync <path-to-repo>
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Sync Archicad MCP schemas from upstream repo clones",
    )
    parser.add_argument(
        "repo_path",
        type=Path,
        help="Path to a Tapir or multiconn repo (auto-detected)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cache = SchemaCache()
    if not cache.sync_from_repo(args.repo_path):
        logger.error("Sync failed")
        raise SystemExit(1)
