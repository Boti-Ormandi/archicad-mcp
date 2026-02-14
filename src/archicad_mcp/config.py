"""Security configuration for script execution."""

from __future__ import annotations

import fnmatch
import os
import sys
import tempfile
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Literal

# =============================================================================
# Platform-Specific Default Blocked Paths
# =============================================================================
# System directories that scripts should never access.
# Architects don't have developer credentials - these are just system protection.

DEFAULT_BLOCKED_WINDOWS = [
    "C:/Windows/*",
    "C:/Program Files/*",
    "C:/Program Files (x86)/*",
]

DEFAULT_BLOCKED_MACOS = [
    "/System/*",
    "/Library/*",
    "/Applications/*",
    "/usr/*",
    "/bin/*",
    "/sbin/*",
    "/etc/*",
    "/var/*",
]

DEFAULT_BLOCKED_LINUX = [
    "/usr/*",
    "/bin/*",
    "/sbin/*",
    "/etc/*",
    "/var/*",
]


def get_default_blocked() -> list[str]:
    """Return platform-specific default blocked paths."""
    if sys.platform == "win32":
        return DEFAULT_BLOCKED_WINDOWS.copy()
    elif sys.platform == "darwin":
        return DEFAULT_BLOCKED_MACOS.copy()
    else:
        return DEFAULT_BLOCKED_LINUX.copy()


# =============================================================================
# Default Allowed Write Paths (sandboxed mode only)
# =============================================================================
# Common safe locations for script output.
# Uses ~ and ${TEMP} which get expanded at runtime.

DEFAULT_ALLOWED_WRITE = [
    "~/Desktop/*",
    "~/Documents/*",
    "${TEMP}/*",
]


# =============================================================================
# Path Helper Functions
# =============================================================================


def expand_pattern(pattern: str) -> str:
    """Expand ~ and ${TEMP} in a path pattern.

    Args:
        pattern: Path pattern with possible ~ or ${TEMP} placeholders.

    Returns:
        Pattern with placeholders expanded to actual paths.
    """
    # Expand ~ to home directory
    if pattern.startswith("~"):
        pattern = str(Path.home()) + pattern[1:]

    # Expand ${TEMP} to temp directory
    if "${TEMP}" in pattern:
        pattern = pattern.replace("${TEMP}", tempfile.gettempdir())

    # Normalize to forward slashes for consistent matching
    return pattern.replace("\\", "/")


def normalize_for_match(path: str) -> str:
    """Normalize path for cross-platform fnmatch comparison.

    - Expands ~ to home directory
    - Resolves to absolute path
    - Converts to forward slashes
    - Lowercases on Windows (case-insensitive filesystem)

    Args:
        path: Path to normalize.

    Returns:
        Normalized path string for matching.
    """
    # Expand ~ and resolve to absolute, canonical path
    resolved = str(Path(path).expanduser().resolve())

    # Convert to forward slashes
    normalized = resolved.replace("\\", "/")

    # Case-insensitive on Windows
    if sys.platform == "win32":
        normalized = normalized.lower()

    return normalized


def matches_pattern(path: str, pattern: str) -> bool:
    """Check if path matches a glob pattern.

    Handles platform differences:
    - Case-insensitive on Windows
    - Forward slash normalization

    Args:
        path: Path to check.
        pattern: Glob pattern (e.g., "C:/Windows/*").

    Returns:
        True if path matches pattern.
    """
    norm_path = normalize_for_match(path)
    norm_pattern = expand_pattern(pattern)

    # Also normalize the pattern for case on Windows
    if sys.platform == "win32":
        norm_pattern = norm_pattern.lower()

    return fnmatch.fnmatch(norm_path, norm_pattern)


# =============================================================================
# Security Configuration
# =============================================================================


@dataclass
class SecurityConfig:
    """Security configuration for script execution.

    Attributes:
        mode: "unrestricted" blocks system dirs only, "sandboxed" also restricts writes.
        blocked_patterns: Glob patterns for paths that cannot be accessed.
        allowed_write_patterns: Glob patterns for writable paths (sandboxed mode only).
    """

    mode: Literal["unrestricted", "sandboxed"] = "unrestricted"
    blocked_patterns: list[str] = field(default_factory=get_default_blocked)
    allowed_write_patterns: list[str] = field(default_factory=lambda: DEFAULT_ALLOWED_WRITE.copy())

    @cached_property
    def blocked_expanded(self) -> list[str]:
        """Blocked patterns with ~ and ${TEMP} expanded."""
        return [expand_pattern(p) for p in self.blocked_patterns]

    @cached_property
    def allowed_write_expanded(self) -> list[str]:
        """Allowed write patterns with ~ and ${TEMP} expanded."""
        return [expand_pattern(p) for p in self.allowed_write_patterns]

    def is_path_blocked(self, path: str, for_write: bool = False) -> bool:
        """Check if a path is blocked by security policy.

        Args:
            path: Path to check.
            for_write: True if this is a write operation.

        Returns:
            True if access should be denied.
        """
        # Check against blocked patterns (applies to both modes)
        for pattern in self.blocked_patterns:
            if matches_pattern(path, pattern):
                return True

        # In sandboxed mode, writes must be to allowed paths
        if for_write and self.mode == "sandboxed":
            for pattern in self.allowed_write_patterns:
                if matches_pattern(path, pattern):
                    return False
            # Write not in allowlist = blocked
            return True

        return False


# =============================================================================
# Configuration Loading
# =============================================================================


def load_config() -> SecurityConfig:
    """Load security configuration from environment variables.

    Environment variables:
        ARCHICAD_MCP_SECURITY: "unrestricted" (default) or "sandboxed"
        ARCHICAD_MCP_BLOCKED_PATHS: Semicolon-separated additional blocked patterns
        ARCHICAD_MCP_ALLOWED_WRITE_PATHS: Semicolon-separated allowed write patterns
            (replaces defaults if set)

    Returns:
        SecurityConfig instance with merged settings.
    """
    # Get mode
    mode_str = os.environ.get("ARCHICAD_MCP_SECURITY", "unrestricted").lower()
    mode: Literal["unrestricted", "sandboxed"] = (
        "sandboxed" if mode_str == "sandboxed" else "unrestricted"
    )

    # Get blocked patterns (merge with defaults)
    blocked = get_default_blocked()
    extra_blocked = os.environ.get("ARCHICAD_MCP_BLOCKED_PATHS", "")
    if extra_blocked:
        blocked.extend(p.strip() for p in extra_blocked.split(";") if p.strip())

    # Get allowed write patterns (replace defaults if set)
    allowed_write_env = os.environ.get("ARCHICAD_MCP_ALLOWED_WRITE_PATHS", "")
    if allowed_write_env:
        allowed_write = [p.strip() for p in allowed_write_env.split(";") if p.strip()]
    else:
        allowed_write = DEFAULT_ALLOWED_WRITE.copy()

    return SecurityConfig(
        mode=mode,
        blocked_patterns=blocked,
        allowed_write_patterns=allowed_write,
    )


def format_file_access_docs(config: SecurityConfig) -> str:
    """Format file access documentation for tool docstring.

    Shows expanded paths so AI sees actual system paths, not placeholders.

    Args:
        config: Security configuration to document.

    Returns:
        Formatted text block for embedding in tool description.
    """
    lines: list[str] = []

    if config.mode == "sandboxed":
        lines.append("FILE ACCESS (SANDBOXED)")
        lines.append("=======================")
        lines.append("Read access to most paths, write access restricted.")
        lines.append("")
        lines.append("ALLOWED WRITE PATHS:")
        for path in config.allowed_write_expanded:
            lines.append(f"  - {path}")
    else:
        lines.append("FILE ACCESS")
        lines.append("===========")
        lines.append("Read/write access to most paths.")

    lines.append("")
    lines.append("BLOCKED (system directories):")
    for path in config.blocked_expanded:
        lines.append(f"  - {path}")

    lines.append("")
    lines.append("Attempting to access blocked paths raises PermissionError.")

    return "\n".join(lines)
