"""Unit tests for safe_open file access control."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from archicad_mcp.config import SecurityConfig
from archicad_mcp.scripting.executor import _create_safe_open, _is_write_mode


class TestIsWriteMode:
    """Tests for _is_write_mode helper."""

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("r", False),
            ("rb", False),
            ("rt", False),
            ("w", True),
            ("wb", True),
            ("wt", True),
            ("a", True),
            ("ab", True),
            ("x", True),
            ("xb", True),
            ("r+", True),
            ("rb+", True),
            ("w+", True),
            ("a+", True),
        ],
    )
    def test_mode_detection(self, mode: str, expected: bool) -> None:
        """Correctly identifies write vs read modes."""
        assert _is_write_mode(mode) == expected


class TestSafeOpenUnrestricted:
    """Tests for safe_open in unrestricted mode."""

    def test_blocks_system_directory(self) -> None:
        """Blocks access to system directories."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        with pytest.raises(PermissionError) as exc_info:
            safe_open("C:/Windows/System32/test.txt", "r")

        assert "blocked directory" in str(exc_info.value)

    def test_allows_normal_read(self, tmp_path: Path) -> None:
        """Allows reading from normal directories."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        # Should be able to read it
        with safe_open(str(test_file), "r") as f:
            content = f.read()

        assert content == "hello"

    def test_allows_normal_write(self, tmp_path: Path) -> None:
        """Allows writing to normal directories."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        test_file = tmp_path / "output.txt"

        with safe_open(str(test_file), "w") as f:
            f.write("written")

        assert test_file.read_text() == "written"

    def test_blocks_write_to_system_dir(self) -> None:
        """Blocks writing to system directories."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        with pytest.raises(PermissionError):
            safe_open("C:/Windows/test.txt", "w")


class TestSafeOpenSandboxed:
    """Tests for safe_open in sandboxed mode."""

    def test_allows_read_anywhere(self, tmp_path: Path) -> None:
        """Allows reading from non-blocked paths."""
        config = SecurityConfig(mode="sandboxed")
        safe_open = _create_safe_open(config)

        # Create test file in tmp
        test_file = tmp_path / "input.txt"
        test_file.write_text("data")

        with safe_open(str(test_file), "r") as f:
            assert f.read() == "data"

    def test_blocks_write_outside_allowlist(self, tmp_path: Path) -> None:
        """Blocks writes to paths not in allowlist."""
        # Use a custom allowlist that doesn't include tmp_path
        config = SecurityConfig(
            mode="sandboxed",
            allowed_write_patterns=["~/Desktop/*"],  # Not tmp_path
        )
        safe_open = _create_safe_open(config)

        test_file = tmp_path / "blocked.txt"

        with pytest.raises(PermissionError) as exc_info:
            safe_open(str(test_file), "w")

        assert "not in allowed write paths" in str(exc_info.value)

    def test_allows_write_to_allowed_path(self) -> None:
        """Allows writes to paths in allowlist."""
        # Default allowlist includes ${TEMP}/*
        config = SecurityConfig(mode="sandboxed")
        safe_open = _create_safe_open(config)

        # Write to temp directory (in default allowlist)
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            temp_path = f.name

        try:
            with safe_open(temp_path, "w") as f:
                f.write("allowed")

            with open(temp_path) as f:
                assert f.read() == "allowed"
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_blocks_system_dir_even_for_read(self) -> None:
        """System directories are blocked even for reads."""
        config = SecurityConfig(mode="sandboxed")
        safe_open = _create_safe_open(config)

        with pytest.raises(PermissionError):
            safe_open("C:/Windows/System32/drivers/etc/hosts", "r")

    def test_allows_write_to_desktop(self) -> None:
        """Allows writes to Desktop (in default allowlist)."""
        config = SecurityConfig(mode="sandboxed")
        safe_open = _create_safe_open(config)

        # Desktop is in default allowlist, but we can't actually write there
        # in tests. Just verify the check passes for the path pattern.
        desktop_path = Path.home() / "Desktop" / "test_output.txt"

        # The path check should pass (not raise), even if the actual
        # file operation might fail due to permissions
        try:
            # This should not raise PermissionError from our check
            # It might raise other errors (FileNotFoundError if Desktop doesn't exist)
            safe_open(str(desktop_path), "w")
        except PermissionError as e:
            # Our check shouldn't raise - only OS-level might
            if "not in allowed write paths" in str(e):
                pytest.fail("Desktop should be in allowed write paths")
        except (FileNotFoundError, OSError):
            # These are OK - means our check passed
            pass


class TestSafeOpenPathHandling:
    """Tests for path handling edge cases."""

    def test_handles_path_object(self, tmp_path: Path) -> None:
        """Accepts Path objects, not just strings."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        test_file = tmp_path / "path_obj.txt"
        test_file.write_text("content")

        # Pass Path object directly
        with safe_open(test_file, "r") as f:
            assert f.read() == "content"

    def test_error_message_shows_blocked_paths(self) -> None:
        """Error message includes blocked paths for debugging."""
        config = SecurityConfig(mode="unrestricted")
        safe_open = _create_safe_open(config)

        with pytest.raises(PermissionError) as exc_info:
            safe_open("C:/Windows/test.txt", "r")

        error_msg = str(exc_info.value)
        assert "C:/Windows" in error_msg

    def test_error_message_shows_allowed_paths_sandboxed(self) -> None:
        """Sandboxed write error shows allowed paths."""
        config = SecurityConfig(
            mode="sandboxed",
            allowed_write_patterns=["~/Desktop/*", "~/Documents/*"],
        )
        safe_open = _create_safe_open(config)

        with pytest.raises(PermissionError) as exc_info:
            safe_open("C:/random/file.txt", "w")

        error_msg = str(exc_info.value)
        assert "Desktop" in error_msg or "Documents" in error_msg
