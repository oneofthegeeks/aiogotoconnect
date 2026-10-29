"""Account and sending-number discovery: feeds the setup page's dropdowns (port of directory.ts)."""

from dataclasses import dataclass
from typing import Any

from aiogotoconnect.sms.http import GoToHttp
from aiogotoconnect.sms.phone import normalize_phone

_SCIM_GETGO = "urn:scim:schemas:extension:getgo:1.0"
_PAGE_SIZE = "100"
_MAX_PAGES = 50


@dataclass(frozen=True)
class GoToAccount:
    key: str
    name: str


@dataclass(frozen=True)
class GoToNumber:
    number: str
    name: str
    account_key: str
    sms_enabled: bool | None = None


async def list_accounts(http: GoToHttp) -> list[GoToAccount]:
    """The GoTo accounts this login can act for. Accounts without a `value` are skipped."""
    me: Any = await http.json("GET", "/identity/v1/Users/me")
    extension = me.get(_SCIM_GETGO) if isinstance(me, dict) else None
    entries = extension.get("accounts") if isinstance(extension, dict) else None
    accounts: list[GoToAccount] = []
    for entry in entries or []:
        value = entry.get("value") if isinstance(entry, dict) else None
        if value:
            accounts.append(GoToAccount(key=value, name=entry.get("display") or value))
    return accounts


async def list_numbers(http: GoToHttp, account_key: str) -> list[GoToNumber]:
    """Phone numbers on one account, in first-seen order, de-duplicated by E.164 number."""
    found: dict[str, GoToNumber] = {}
    marker: str | None = None
    # The cap only guards against a marker that never ends. Hitting it returns what we have
    # rather than raising: a partial list still lets the admin pick a number.
    for _ in range(_MAX_PAGES):
        query = {"accountKey": account_key, "pageSize": _PAGE_SIZE}
        if marker:
            query["pageMarker"] = marker
        res: Any = await http.json("GET", "/voice-admin/v1/phone-numbers", query=query)
        for item in (res.get("items") if isinstance(res, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            # Normalization is the filter: anything that isn't a phone number is skipped silently.
            number = normalize_phone(item.get("number") or "")
            if not number or number in found:
                continue
            # GoTo often puts the number itself in `name`; that's not a useful label.
            raw_name = item.get("name")
            name = item.get("callerIdName") or (
                raw_name if raw_name and normalize_phone(raw_name) != number else ""
            )
            enabled = item.get("smsEnabled")
            found[number] = GoToNumber(
                number=number,
                name=name,
                account_key=account_key,
                sms_enabled=enabled if isinstance(enabled, bool) else None,
            )
        marker = res.get("nextPageMarker") or None if isinstance(res, dict) else None
        if not marker:
            break
    return list(found.values())
