"""Port of packages/node/test/phone.test.ts (C8)."""

import pytest

from aiogotoconnect.sms.phone import normalize_phone


@pytest.mark.parametrize(
    "raw",
    ["(801) 555-0123", "801.555.0123", "1 801 555 0123", "+1 801-555-0123", " 8015550123 "],
)
def test_c8_nanp_loose_formats(raw: str) -> None:
    assert normalize_phone(raw) == "+18015550123"


@pytest.mark.parametrize("raw", ["1015550123", "8011550123", "9115550123"])
def test_c8_nanp_rules_area_exchange_start_2_9_no_n11_area(raw: str) -> None:
    assert normalize_phone(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "555-0123",
        "call me",
        "+1",
        "",
        "+12",
        "801-555-0123 ext 4",
        "+",
        "(801) 555-0123#",
        "٨٠١٥٥٥٠١٢٣",  # non-ASCII digits are not digits here (JS \d is ASCII-only)
    ],
)
def test_c8_junk_is_none(raw: str) -> None:
    assert normalize_phone(raw) is None


def test_c8_international_plus_passes_through_8_to_15_digits() -> None:
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("+1234567") is None


def test_c8_plus_one_numbers_must_pass_nanp_rules() -> None:
    assert normalize_phone("+1 911 555 0123") is None
    assert normalize_phone("+1 801 155 0123") is None
