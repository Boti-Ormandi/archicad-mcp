"""Strict SemVer validation and precedence for Tapir release identities.

Only exact ``X.Y.Z`` tags (optionally with a prerelease suffix) are accepted.
Comparison follows SemVer precedence without converting arbitrarily long
numeric components through binary integers, so versions far beyond machine
word sizes still order correctly.
"""

from __future__ import annotations

from typing import Never

SAFE_INTEGER_MAXIMUM = 9_007_199_254_740_991


class SemverValidationError(ValueError):
    """A strict-SemVer failure represented by a stable code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> Never:
    raise SemverValidationError(code)


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and all("0" <= character <= "9" for character in value)


_NumericComponent = tuple[int, str]
_PrereleaseIdentifier = tuple[int, int, str]
_SemverKey = tuple[
    _NumericComponent,
    _NumericComponent,
    _NumericComponent,
    int,
    tuple[_PrereleaseIdentifier, ...],
]


def validate_semver(value: object) -> None:
    """Validate the strict SemVer profile accepted by this project."""
    if type(value) is not str:
        _fail("semver")
    core, separator, prerelease = value.partition("-")
    if core.count(".") != 2 or "+" in core or "+" in prerelease:
        _fail("semver")
    for component in core.split(".", 2):
        if not _is_ascii_digits(component) or (len(component) > 1 and component[0] == "0"):
            _fail("semver")
    if not separator:
        return
    if not prerelease:
        _fail("semver")
    start = 0
    while True:
        end = prerelease.find(".", start)
        end = len(prerelease) if end == -1 else end
        identifier = prerelease[start:end]
        if not identifier:
            _fail("semver")
        if _is_ascii_digits(identifier):
            if len(identifier) > 1 and identifier[0] == "0":
                _fail("semver")
        else:
            for character in identifier:
                if not (
                    "A" <= character <= "Z"
                    or "a" <= character <= "z"
                    or "0" <= character <= "9"
                    or character == "-"
                ):
                    _fail("semver")
        if end == len(prerelease):
            return
        start = end + 1


def is_stable_release_version(value: object) -> bool:
    """Return whether the value is an exact bare stable ``X.Y.Z`` SemVer tag."""
    try:
        validate_semver(value)
    except SemverValidationError:
        return False
    assert isinstance(value, str)
    return "-" not in value


def _numeric_key(component: str) -> _NumericComponent:
    """Compare canonical nonnegative decimal strings without integer conversion."""

    return len(component), component


def _identifier_key(identifier: str) -> _PrereleaseIdentifier:
    """Order numeric identifiers numerically and before alphanumeric ones."""

    if _is_ascii_digits(identifier):
        return 0, len(identifier), identifier
    return 1, 0, identifier


def _semver_key(value: object) -> _SemverKey:
    """Return the strict SemVer precedence key used across the foundation."""

    validate_semver(value)
    assert isinstance(value, str)
    core, separator, prerelease = value.partition("-")
    major, minor, patch = core.split(".")
    core_key = (_numeric_key(major), _numeric_key(minor), _numeric_key(patch))
    if not separator:
        return *core_key, 1, ()
    identifiers = tuple(_identifier_key(item) for item in prerelease.split("."))
    return *core_key, 0, identifiers


def compare_semver(left: object, right: object) -> int:
    """Compare two values under strict SemVer precedence without integer conversion."""

    left_key = _semver_key(left)
    right_key = _semver_key(right)
    return (left_key > right_key) - (left_key < right_key)


__all__ = [
    "SAFE_INTEGER_MAXIMUM",
    "SemverValidationError",
    "compare_semver",
    "is_stable_release_version",
    "validate_semver",
]
