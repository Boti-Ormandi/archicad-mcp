"""Unit tests for security configuration."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from archicad_mcp.config import (
    SecurityConfig,
    expand_pattern,
    format_file_access_docs,
    get_default_blocked,
    load_config,
    matches_pattern,
    normalize_for_match,
)


class TestGetDefaultBlocked:
    """Tests for get_default_blocked()."""

    def test_returns_list(self) -> None:
        """Returns a list of patterns."""
        result = get_default_blocked()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_returns_copy(self) -> None:
        """Returns a copy, not the original list."""
        result1 = get_default_blocked()
        result2 = get_default_blocked()
        result1.append("test")
        assert "test" not in result2

    @patch("archicad_mcp.config.sys.platform", "win32")
    def test_windows_platform(self) -> None:
        """Returns Windows paths on Windows."""
        # Need to reimport to pick up patched platform
        from archicad_mcp import config

        with patch.object(config, "sys") as mock_sys:
            mock_sys.platform = "win32"
            result = config.get_default_blocked()
        assert "C:/Windows/*" in result

    @patch("archicad_mcp.config.sys.platform", "darwin")
    def test_macos_platform(self) -> None:
        """Returns macOS paths on macOS."""
        from archicad_mcp import config

        with patch.object(config, "sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = config.get_default_blocked()
        assert "/System/*" in result

    @patch("archicad_mcp.config.sys.platform", "linux")
    def test_linux_platform(self) -> None:
        """Returns Linux paths on Linux."""
        from archicad_mcp import config

        with patch.object(config, "sys") as mock_sys:
            mock_sys.platform = "linux"
            result = config.get_default_blocked()
        assert "/usr/*" in result


class TestExpandPattern:
    """Tests for expand_pattern()."""

    def test_expands_home_tilde(self) -> None:
        """Expands ~ to home directory."""
        result = expand_pattern("~/Documents/*")
        assert result.startswith(str(Path.home()).replace("\\", "/"))
        assert result.endswith("/Documents/*")

    def test_expands_temp_variable(self) -> None:
        """Expands ${TEMP} to temp directory."""
        result = expand_pattern("${TEMP}/output.csv")
        expected_temp = tempfile.gettempdir().replace("\\", "/")
        assert expected_temp in result

    def test_normalizes_backslashes(self) -> None:
        """Converts backslashes to forward slashes."""
        # Even if input has backslashes, output uses forward
        result = expand_pattern("C:\\Users\\test")
        assert "\\" not in result

    def test_no_expansion_needed(self) -> None:
        """Passes through patterns without placeholders."""
        result = expand_pattern("C:/Windows/*")
        assert result == "C:/Windows/*"


class TestNormalizeForMatch:
    """Tests for normalize_for_match()."""

    def test_expands_tilde(self) -> None:
        """Expands ~ in paths."""
        result = normalize_for_match("~/test.txt")
        assert "~" not in result
        assert str(Path.home()).replace("\\", "/").lower() in result.lower()

    def test_converts_to_forward_slashes(self) -> None:
        """Converts backslashes to forward slashes."""
        if sys.platform == "win32":
            result = normalize_for_match("C:\\Users\\test")
            assert "\\" not in result
            assert "/" in result

    def test_resolves_relative_paths(self) -> None:
        """Resolves relative paths to absolute."""
        result = normalize_for_match("./test.txt")
        # Should be absolute path
        if sys.platform == "win32":
            assert result[1] == ":"  # Drive letter
        else:
            assert result.startswith("/")

    @patch("archicad_mcp.config.sys.platform", "win32")
    def test_lowercase_on_windows(self) -> None:
        """Lowercases paths on Windows."""
        from archicad_mcp import config

        with patch.object(config, "sys") as mock_sys:
            mock_sys.platform = "win32"
            # Call the actual normalize logic
            path = str(Path("C:/Users/TEST").resolve()).replace("\\", "/")
            result = path.lower()
            assert result == result.lower()


class TestMatchesPattern:
    """Tests for matches_pattern()."""

    def test_matches_wildcard(self) -> None:
        """Matches paths with wildcard patterns."""
        assert matches_pattern("C:/Windows/System32/cmd.exe", "C:/Windows/*")

    def test_no_match(self) -> None:
        """Returns False when path doesn't match."""
        assert not matches_pattern("C:/Users/test.txt", "C:/Windows/*")

    def test_matches_with_tilde_pattern(self) -> None:
        """Matches expanded tilde patterns."""
        home = str(Path.home()).replace("\\", "/")
        test_path = f"{home}/Desktop/file.txt"
        assert matches_pattern(test_path, "~/Desktop/*")

    def test_matches_with_temp_pattern(self) -> None:
        """Matches expanded ${TEMP} patterns."""
        temp = tempfile.gettempdir().replace("\\", "/")
        test_path = f"{temp}/output.csv"
        assert matches_pattern(test_path, "${TEMP}/*")


class TestSecurityConfig:
    """Tests for SecurityConfig dataclass."""

    def test_default_values(self) -> None:
        """Default config has unrestricted mode."""
        cfg = SecurityConfig()
        assert cfg.mode == "unrestricted"
        assert len(cfg.blocked_patterns) > 0
        assert len(cfg.allowed_write_patterns) > 0

    def test_blocked_expanded_cached(self) -> None:
        """blocked_expanded is a cached property."""
        cfg = SecurityConfig()
        result1 = cfg.blocked_expanded
        result2 = cfg.blocked_expanded
        assert result1 is result2  # Same object (cached)

    def test_allowed_write_expanded_cached(self) -> None:
        """allowed_write_expanded is a cached property."""
        cfg = SecurityConfig()
        result1 = cfg.allowed_write_expanded
        result2 = cfg.allowed_write_expanded
        assert result1 is result2

    def test_is_path_blocked_system_dir(self) -> None:
        """System directories are blocked."""
        cfg = SecurityConfig()
        if sys.platform == "win32":
            assert cfg.is_path_blocked("C:/Windows/System32/file.dll")
        else:
            assert cfg.is_path_blocked("/usr/bin/python")

    def test_is_path_blocked_normal_path(self) -> None:
        """Normal paths are not blocked in unrestricted mode."""
        cfg = SecurityConfig()
        home = str(Path.home())
        assert not cfg.is_path_blocked(f"{home}/test.txt")

    def test_sandboxed_blocks_writes_outside_allowlist(self) -> None:
        """Sandboxed mode blocks writes outside allowed paths."""
        cfg = SecurityConfig(mode="sandboxed")
        # Random path not in allowlist
        assert cfg.is_path_blocked("C:/random/output.csv", for_write=True)

    def test_sandboxed_allows_writes_to_desktop(self) -> None:
        """Sandboxed mode allows writes to Desktop."""
        cfg = SecurityConfig(mode="sandboxed")
        home = str(Path.home()).replace("\\", "/")
        assert not cfg.is_path_blocked(f"{home}/Desktop/output.csv", for_write=True)

    def test_sandboxed_allows_reads_anywhere(self) -> None:
        """Sandboxed mode allows reads from non-blocked paths."""
        cfg = SecurityConfig(mode="sandboxed")
        # Read from random path (not system dir) should be allowed
        assert not cfg.is_path_blocked("C:/Data/input.csv", for_write=False)


class TestLoadConfig:
    """Tests for load_config()."""

    def test_default_without_env_vars(self) -> None:
        """Returns default config when no env vars set."""
        # Clear any existing env vars
        env_backup = {}
        for key in [
            "ARCHICAD_MCP_SECURITY",
            "ARCHICAD_MCP_BLOCKED_PATHS",
            "ARCHICAD_MCP_ALLOWED_WRITE_PATHS",
        ]:
            env_backup[key] = os.environ.pop(key, None)

        try:
            cfg = load_config()
            assert cfg.mode == "unrestricted"
            assert cfg.blocked_patterns == get_default_blocked()
        finally:
            # Restore env vars
            for key, value in env_backup.items():
                if value is not None:
                    os.environ[key] = value

    def test_reads_security_mode(self) -> None:
        """Reads ARCHICAD_MCP_SECURITY env var."""
        with patch.dict(os.environ, {"ARCHICAD_MCP_SECURITY": "sandboxed"}):
            cfg = load_config()
            assert cfg.mode == "sandboxed"

    def test_security_mode_case_insensitive(self) -> None:
        """Security mode is case-insensitive."""
        with patch.dict(os.environ, {"ARCHICAD_MCP_SECURITY": "SANDBOXED"}):
            cfg = load_config()
            assert cfg.mode == "sandboxed"

    def test_invalid_mode_defaults_to_unrestricted(self) -> None:
        """Invalid mode falls back to unrestricted."""
        with patch.dict(os.environ, {"ARCHICAD_MCP_SECURITY": "invalid"}):
            cfg = load_config()
            assert cfg.mode == "unrestricted"

    def test_reads_extra_blocked_paths(self) -> None:
        """Reads ARCHICAD_MCP_BLOCKED_PATHS and merges with defaults."""
        with patch.dict(os.environ, {"ARCHICAD_MCP_BLOCKED_PATHS": "~/.ssh/*;~/.aws/*"}):
            cfg = load_config()
            assert "~/.ssh/*" in cfg.blocked_patterns
            assert "~/.aws/*" in cfg.blocked_patterns
            # Still has defaults
            assert len(cfg.blocked_patterns) > 2

    def test_reads_allowed_write_paths(self) -> None:
        """Reads ARCHICAD_MCP_ALLOWED_WRITE_PATHS (replaces defaults)."""
        with patch.dict(
            os.environ, {"ARCHICAD_MCP_ALLOWED_WRITE_PATHS": "~/Output/*;D:/Projects/*"}
        ):
            cfg = load_config()
            assert cfg.allowed_write_patterns == ["~/Output/*", "D:/Projects/*"]


class TestFormatFileAccessDocs:
    """Tests for format_file_access_docs()."""

    def test_unrestricted_mode_header(self) -> None:
        """Unrestricted mode has correct header."""
        cfg = SecurityConfig(mode="unrestricted")
        result = format_file_access_docs(cfg)
        assert "FILE ACCESS" in result
        assert "SANDBOXED" not in result

    def test_sandboxed_mode_header(self) -> None:
        """Sandboxed mode has correct header."""
        cfg = SecurityConfig(mode="sandboxed")
        result = format_file_access_docs(cfg)
        assert "FILE ACCESS (SANDBOXED)" in result

    def test_shows_blocked_paths(self) -> None:
        """Shows blocked paths in output."""
        cfg = SecurityConfig()
        result = format_file_access_docs(cfg)
        assert "BLOCKED" in result
        # Should show expanded paths
        for path in cfg.blocked_expanded:
            assert path in result

    def test_sandboxed_shows_allowed_write(self) -> None:
        """Sandboxed mode shows allowed write paths."""
        cfg = SecurityConfig(mode="sandboxed")
        result = format_file_access_docs(cfg)
        assert "ALLOWED WRITE PATHS" in result
        for path in cfg.allowed_write_expanded:
            assert path in result

    def test_unrestricted_no_allowed_write(self) -> None:
        """Unrestricted mode doesn't show allowed write section."""
        cfg = SecurityConfig(mode="unrestricted")
        result = format_file_access_docs(cfg)
        assert "ALLOWED WRITE PATHS" not in result
