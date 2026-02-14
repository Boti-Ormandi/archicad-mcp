"""Full-text search index for Archicad command documentation."""

from __future__ import annotations

import json
import re
from typing import Any


def tokenize(text: str) -> list[str]:
    """Split text into searchable tokens.

    Args:
        text: Text to tokenize

    Returns:
        List of lowercase tokens (min 2 chars)
    """
    text = text.lower()
    # Split on non-alphanumeric, keep tokens >= 2 chars
    tokens = re.split(r"[^a-z0-9]+", text)
    return [t for t in tokens if len(t) >= 2]


class SearchIndex:
    """Full-text search index for command documentation.

    Builds an inverted index at load time for fast searching.
    Supports exact matching, prefix matching, and fuzzy matching.
    """

    # Field weights for scoring
    WEIGHT_NAME = 100
    WEIGHT_DESCRIPTION = 40
    WEIGHT_PARAM_NAME = 30
    WEIGHT_PARAM_DESC = 25
    WEIGHT_ENUM = 20
    WEIGHT_NOTES = 15
    WEIGHT_EXAMPLE = 15
    WEIGHT_RETURNS = 10

    # JSON Schema keywords to skip during indexing (not semantic content)
    SCHEMA_KEYWORDS = frozenset(
        {
            "type",
            "properties",
            "items",
            "required",
            "additionalProperties",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
            "enum",
            "oneOf",
            "anyOf",
            "allOf",
            "$ref",
            "default",
            "const",
            "pattern",
            "format",
            "description",  # description indexed separately
        }
    )

    def __init__(self) -> None:
        self.commands: dict[str, dict[str, Any]] = {}
        self.element_types: set[str] = set()
        self.element_types_lower: dict[str, str] = {}  # lowercase -> actual
        self.ref_schemas: dict[str, Any] = {}  # $ref target name -> schema

        # Inverted index: token -> [(command_name, field, weight)]
        self.token_index: dict[str, list[tuple[str, str, int]]] = {}

        # All unique tokens (for fuzzy matching)
        self.all_tokens: set[str] = set()

    def build(
        self,
        commands: dict[str, dict[str, Any]],
        element_types: list[str],
        ref_schemas: dict[str, Any] | None = None,
    ) -> None:
        """Build search index from loaded schemas.

        Args:
            commands: Dict of command_name -> command_schema
            element_types: List of valid element types
            ref_schemas: Dict of schema name -> schema definition for $ref lookup
        """
        self.commands = commands
        self.element_types = set(element_types)
        self.element_types_lower = {et.lower(): et for et in element_types}
        self.ref_schemas = ref_schemas or {}

        # Clear and rebuild index
        self.token_index = {}
        self.all_tokens = set()

        for name, cmd in commands.items():
            self._index_command(name, cmd)

    def _add_to_index(self, token: str, command: str, field: str, weight: int) -> None:
        """Add a token to the inverted index."""
        if token not in self.token_index:
            self.token_index[token] = []
        self.token_index[token].append((command, field, weight))
        self.all_tokens.add(token)

    def _index_command(self, name: str, cmd: dict[str, Any]) -> None:
        """Index all searchable text from a command."""
        # Index command name
        for token in tokenize(name):
            self._add_to_index(token, name, "name", self.WEIGHT_NAME)

        # Index description
        desc = cmd.get("description", "")
        for token in tokenize(desc):
            self._add_to_index(token, name, "description", self.WEIGHT_DESCRIPTION)

        # Index parameters
        params = cmd.get("parameters", {})
        self._index_parameters(name, params)

        # Index example
        example = cmd.get("example")
        if example:
            example_text = json.dumps(example) if isinstance(example, dict) else str(example)
            for token in tokenize(example_text):
                self._add_to_index(token, name, "example", self.WEIGHT_EXAMPLE)

        # Index notes
        notes = cmd.get("notes", "")
        for token in tokenize(notes):
            self._add_to_index(token, name, "notes", self.WEIGHT_NOTES)

        # Index returns
        returns = cmd.get("returns", {})
        if returns:
            returns_text = json.dumps(returns) if isinstance(returns, dict) else str(returns)
            for token in tokenize(returns_text):
                self._add_to_index(token, name, "returns", self.WEIGHT_RETURNS)

    def _index_parameters(self, name: str, params: Any, depth: int = 0) -> None:
        """Recursively index parameter names and descriptions."""
        if depth > 5:  # Prevent infinite recursion
            return

        if isinstance(params, dict):
            for param_name, param_value in params.items():
                # Skip JSON schema keywords - they're not semantic content
                if param_name in self.SCHEMA_KEYWORDS:
                    # Index enum values — they contain domain vocabulary
                    if param_name == "enum" and isinstance(param_value, list):
                        for item in param_value:
                            if isinstance(item, str):
                                for token in tokenize(item):
                                    self._add_to_index(token, name, "enum", self.WEIGHT_ENUM)
                    # Look up $ref targets and index their enum values
                    elif param_name == "$ref" and isinstance(param_value, str):
                        ref_name = param_value.rsplit("/", 1)[-1]
                        ref_schema = self.ref_schemas.get(ref_name)
                        if isinstance(ref_schema, dict):
                            enum_values = ref_schema.get("enum", [])
                            for item in enum_values:
                                if isinstance(item, str):
                                    for token in tokenize(item):
                                        self._add_to_index(token, name, "enum", self.WEIGHT_ENUM)
                    # Still recurse into nested structures
                    if isinstance(param_value, dict):
                        self._index_parameters(name, param_value, depth + 1)
                    elif isinstance(param_value, list):
                        for item in param_value:
                            if isinstance(item, dict):
                                self._index_parameters(name, item, depth + 1)
                    continue

                # Index parameter name
                for token in tokenize(param_name):
                    self._add_to_index(token, name, "parameters", self.WEIGHT_PARAM_NAME)

                # Index parameter description/type if string
                if isinstance(param_value, str):
                    for token in tokenize(param_value):
                        self._add_to_index(token, name, "parameters", self.WEIGHT_PARAM_DESC)
                elif isinstance(param_value, dict):
                    # Recurse into nested parameter definitions
                    self._index_parameters(name, param_value, depth + 1)

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Search for commands matching query.

        Args:
            query: Search query (e.g., "wall", "create slab", "property")
            limit: Maximum number of results to return

        Returns:
            Dict with query, total, element_type_hint (if detected), and results
        """
        query_lower = query.lower().strip()
        tokens = tokenize(query_lower)

        if not tokens:
            return {
                "query": query,
                "total": 0,
                "results": [],
                "tip": "Enter a search term",
            }

        # Check for element type
        element_type_hint = self._detect_element_type(tokens)

        # Score commands
        scores = self._score_exact_and_prefix(tokens)

        # If no good results, try fuzzy matching
        if not scores or max(s for s, _, _ in scores.values()) < 20:
            fuzzy_scores = self._score_fuzzy(tokens)
            # Merge fuzzy scores
            for cmd, (score, fields, qtokens) in fuzzy_scores.items():
                if cmd in scores:
                    es, ef, eq = scores[cmd]
                    scores[cmd] = (es + score, ef | fields, eq | qtokens)
                else:
                    scores[cmd] = (score, fields, qtokens)

        # Apply coverage multiplier for multi-token queries:
        # commands matching all query tokens keep full score,
        # partial matches are scaled down proportionally.
        if len(tokens) > 1:
            for cmd_name, (score, fields, qtokens) in scores.items():
                coverage = len(qtokens) / len(tokens)
                scores[cmd_name] = (int(score * coverage), fields, qtokens)

        # Build results
        results = self._build_results(scores, limit)

        total = len(scores)
        response: dict[str, Any] = {
            "query": query,
            "total": total,
            "showing": len(results),
            "results": results,
        }

        if element_type_hint:
            response["element_type_hint"] = element_type_hint

        if results:
            if total > len(results):
                response["tip"] = (
                    f"{total} matches truncated to {len(results)}. Refine your search."
                )
            else:
                response["tip"] = "Use get_docs(command='name') for full parameter details"
        else:
            response["suggestion"] = "No matches found. Try different keywords."

        return response

    def _detect_element_type(self, tokens: list[str]) -> dict[str, Any] | None:
        """Check if query contains an element type."""
        for token in tokens:
            if token in self.element_types_lower:
                actual_type = self.element_types_lower[token]
                return {
                    "type": actual_type,
                    "message": f"To query {actual_type} elements:",
                    "command": "GetElementsByType",
                    "parameters": {"elementType": actual_type},
                    "script": f'await archicad.tapir("GetElementsByType", {{"elementType": "{actual_type}"}})',
                }
        return None

    def _score_exact_and_prefix(
        self, tokens: list[str]
    ) -> dict[str, tuple[int, set[str], set[str]]]:
        """Score commands by exact and prefix token matches.

        Returns:
            Dict of command_name -> (score, matched fields, matched query tokens)
        """
        scores: dict[str, tuple[int, set[str], set[str]]] = {}

        for token in tokens:
            # Exact matches
            if token in self.token_index:
                for cmd_name, field, weight in self.token_index[token]:
                    if cmd_name not in scores:
                        scores[cmd_name] = (0, set(), set())
                    score, fields, qtokens = scores[cmd_name]
                    scores[cmd_name] = (
                        score + weight,
                        fields | {field},
                        qtokens | {token},
                    )

            # Prefix matches (tokens >= 3 chars)
            if len(token) >= 3:
                for indexed_token in self.all_tokens:
                    if indexed_token.startswith(token) and indexed_token != token:
                        for cmd_name, field, weight in self.token_index[indexed_token]:
                            if cmd_name not in scores:
                                scores[cmd_name] = (0, set(), set())
                            score, fields, qtokens = scores[cmd_name]
                            # Prefix match gets 50% weight
                            scores[cmd_name] = (
                                score + weight // 2,
                                fields | {field},
                                qtokens | {token},
                            )

        return scores

    def _score_fuzzy(self, tokens: list[str]) -> dict[str, tuple[int, set[str], set[str]]]:
        """Score commands using fuzzy matching for typo tolerance.

        Returns:
            Dict of command_name -> (score, matched fields, matched query tokens)
        """
        try:
            from rapidfuzz import fuzz
        except ImportError:
            return {}

        scores: dict[str, tuple[int, set[str], set[str]]] = {}

        for token in tokens:
            if len(token) < 4:
                continue

            # Find similar tokens
            for indexed_token in self.all_tokens:
                if len(indexed_token) < 4:
                    continue

                # Use partial ratio for better substring matching
                ratio = fuzz.ratio(token, indexed_token)
                if ratio >= 80:  # 80% similarity threshold
                    for cmd_name, field, weight in self.token_index[indexed_token]:
                        if cmd_name not in scores:
                            scores[cmd_name] = (0, set(), set())
                        score, fields, qtokens = scores[cmd_name]
                        # Fuzzy match gets 30% weight, scaled by ratio
                        fuzzy_weight = int(weight * 30 * ratio) // (100 * 100)
                        scores[cmd_name] = (
                            score + fuzzy_weight,
                            fields | {field},
                            qtokens | {token},
                        )

        return scores

    def _build_results(
        self, scores: dict[str, tuple[int, set[str], set[str]]], limit: int
    ) -> list[dict[str, Any]]:
        """Build sorted result list from scores."""
        results = []

        # Sort by score descending
        sorted_cmds = sorted(scores.items(), key=lambda x: -x[1][0])

        for cmd_name, (score, fields, _qtokens) in sorted_cmds[:limit]:
            cmd = self.commands.get(cmd_name, {})
            results.append(
                {
                    "name": cmd_name,
                    "description": cmd.get("description", ""),
                    "category": cmd.get("category", ""),
                    "score": score,
                    "matched_in": sorted(fields),
                    "has_details": "parameters" in cmd,
                }
            )

        return results
