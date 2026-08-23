"""Focused SemVer tests for the direct Tapir updater foundation."""

from __future__ import annotations

from itertools import pairwise

import pytest

from archicad_mcp.schemas.semver import (
    SAFE_INTEGER_MAXIMUM,
    SemverValidationError,
    compare_semver,
    is_stable_release_version,
    validate_semver,
)

ORDERED = [
    "0.0.1",
    "0.1.0",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.1",
    "1.1.0",
    "1.9.0",
    "1.10.0",
    "2.0.0",
]


def test_strict_precedence_order_is_total_and_antisymmetric() -> None:
    for earlier, later in pairwise(ORDERED):
        assert compare_semver(earlier, later) < 0
        assert compare_semver(later, earlier) > 0
        assert compare_semver(earlier, earlier) == 0
    assert compare_semver(ORDERED[0], ORDERED[-1]) < 0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("1.2.3", "1.2.3"),
        ("1.0.0-rc.1", "1.0.0-rc.1"),
    ],
)
def test_equal_versions_compare_zero(left: str, right: str) -> None:
    assert compare_semver(left, right) == 0


def test_numeric_identifiers_order_by_value_not_lexicographically() -> None:
    assert compare_semver("1.0.0-2", "1.0.0-10") < 0
    assert compare_semver("1.0.0-beta.11", "1.0.0-beta.2") > 0
    assert compare_semver("1.10.0", "1.9.0") > 0


def test_numeric_identifiers_precede_alphanumeric() -> None:
    assert compare_semver("1.0.0-1", "1.0.0-alpha") < 0
    assert compare_semver("1.0.0-alpha", "1.0.0-1") > 0


def test_huge_components_stay_exact_beyond_safe_integer_range() -> None:
    huge_low = f"1.2.{SAFE_INTEGER_MAXIMUM}"
    huge_high = f"1.2.{SAFE_INTEGER_MAXIMUM + 1}"
    assert compare_semver(huge_low, huge_high) < 0
    validate_semver(huge_high)
    twenty_digits = "1.2." + "9" * 20
    twentyone_digits = "1.2." + "1" + "0" * 20
    assert compare_semver(twenty_digits, twentyone_digits) < 0


def test_selection_key_differs_from_naive_string_or_float_ordering() -> None:
    # Sensitivity: a naive lexicographic selection would pick the wrong max.
    assert compare_semver("1.9.0", "1.10.0") < 0
    assert "1.10.0" < "1.9.0"
    # A naive float conversion would lose precision at this magnitude.
    assert compare_semver("1.2.9007199254740993", "1.2.9007199254740992") > 0


@pytest.mark.parametrize(
    "value",
    [
        "1.2",
        "1.2.3.4",
        "v1.2.3",
        " 1.2.3",
        "1.2.3 ",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-",
        "1.2.3-01",
        "1.2.3-alpha..1",
        "1.2.3+build",
        "",
        "-1.2.3",
        "1.-2.3",
        5,
        None,
        ["1.2.3"],
    ],
)
def test_malformed_versions_are_refused_with_stable_code(value: object) -> None:
    with pytest.raises(SemverValidationError) as caught:
        validate_semver(value)
    assert caught.value.code == "semver"


def test_prerelease_alphabet_is_restricted() -> None:
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0-alpha_1")
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0-ä")


def test_prerelease_alphanumeric_identifiers_accept_mixed_digits() -> None:
    # Regression: alphanumeric identifiers may contain digits per SemVer.
    validate_semver("1.0.0-alpha1")
    validate_semver("1.0.0-a1-b2")
    validate_semver("1.0.0-rc1.2")
    assert compare_semver("1.0.0-alpha", "1.0.0-alpha1") < 0
    assert compare_semver("1.0.0-alpha1", "1.0.0-alpha2") < 0
    assert compare_semver("1.0.0-a1-b2", "1.0.0-a1.b2") > 0


def test_leading_zero_numeric_identifiers_and_build_metadata_stay_refused() -> None:
    # Negative control: mixed-digit acceptance must not loosen numeric rules.
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0-01")
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0-alpha.01")
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0-alpha+build")
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.0+b1")
    with pytest.raises(SemverValidationError):
        validate_semver("1.0.01-a1")
    # An implementation rejecting mixed digits outright would misorder here.
    assert compare_semver("1.0.0-alpha1", "1.0.0-alphaz") < 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.5.8", True),
        ("0.0.1", True),
        ("1.5.8-beta", False),
        ("ac-addon-1.5.8", False),
        ("1.5", False),
        (None, False),
        ("1.2.3+meta", False),
    ],
)
def test_stable_release_detection(value: object, expected: bool) -> None:
    assert is_stable_release_version(value) is expected


def test_error_carries_code_and_message() -> None:
    error = SemverValidationError("semver")
    assert error.code == "semver"
    assert str(error) == "semver"
