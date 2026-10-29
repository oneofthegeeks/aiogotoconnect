"""Phone normalisation to E.164 (port of Node phone.ts).

A phone number is a caller's identity for keyword handling and dedupe, so loosely formatted
input such as "(801) 555-0123" or "+1 801-555-0123" must resolve to one canonical form.
Returns None for anything that isn't clearly a phone number rather than guessing.
"""

import re

_NANP_COUNTRY_CODE = "1"
# Only a leading '+' plus ASCII digits, whitespace and the usual separators. `fullmatch` (not `$`)
# so a trailing newline is rejected; [0-9] (not \d) so non-ASCII digits are rejected, as in JS.
_ALLOWED = re.compile(r"\+?[0-9\s().-]+")


def _is_valid_nanp(ten_digits: str) -> bool:
    """NANP: area code and exchange both start 2-9; N11 area codes (211, 911...) are service codes."""
    if not re.fullmatch(r"[0-9]{10}", ten_digits):
        return False
    area, exchange = ten_digits[:3], ten_digits[3:6]
    if area[0] in "01" or exchange[0] in "01":
        return False
    return not (area[1] == "1" and area[2] == "1")


def normalize_phone(raw: str) -> str | None:
    text = raw.strip()
    if not _ALLOWED.fullmatch(text):
        return None

    has_plus = text.startswith("+")
    digits = re.sub(r"[^0-9]", "", text)

    # A '+' followed by a non-NANP country code: any length from 8 to 15 digits.
    if has_plus and not digits.startswith(_NANP_COUNTRY_CODE):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None

    # NANP. "1" + ten digits is the country-code form; a bare ten digits is the national form.
    # A '+' must use the country-code form, so "+1" with a wrong length is rejected.
    has_country_code = len(digits) == 11 and digits.startswith(_NANP_COUNTRY_CODE)
    if has_plus and not has_country_code:
        return None
    national = digits[1:] if has_country_code else digits
    if not _is_valid_nanp(national):
        return None
    return f"+{_NANP_COUNTRY_CODE}{national}"
