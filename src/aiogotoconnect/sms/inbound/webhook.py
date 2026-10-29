"""Webhook setup (notification channel + messaging subscription) and tolerant parsing of GoTo's
inbound envelope (port of inbound/webhook.ts). Nothing here logs: bodies hold phone numbers/text.
"""

import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import quote

from aiogotoconnect.sms.errors import GoToApiError
from aiogotoconnect.sms.http import GoToHttp
from aiogotoconnect.sms.log import iso_ms
from aiogotoconnect.sms.phone import normalize_phone
from aiogotoconnect.sms.store.types import GoToSmsStore
from aiogotoconnect.sms.types import InboundMessage

CHANNELS = "/notification-channel/v1/channels"
SUBSCRIPTIONS = "/messaging/v2/subscriptions"
EVENT = "INBOUND_MESSAGE"
MAX_TEXT = 500

KEY_SECRET = "inbound:webhookSecret"
KEY_CHANNEL = "inbound:channelId"
KEY_SUBSCRIPTION = "inbound:subscriptionId"
KEY_ACCOUNT = "inbound:accountKey"

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _new_secret() -> str:
    return secrets.token_urlsafe(32)  # 43 chars of base64url, as Node's randomBytes(32)


def _base36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _BASE36[r] + out
        if n == 0:
            return out


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


async def enable_webhook(
    *,
    http: GoToHttp,
    store: GoToSmsStore,
    tenant: str,
    account_key: str,
    webhook_url: str,
    channel_prefix: str,
) -> dict[str, str]:
    """Make sure GoTo has a Webhook channel pointing at `webhook_url` and an INBOUND_MESSAGE
    subscription on `account_key` for it, reusing whatever already matches. Stores the ids (and
    the webhook secret, if absent). A 403 surfaces as GoToApiError with GoTo's body.

    Returns {"channel_id": ..., "subscription_id": ...}.
    """
    if not account_key:
        raise ValueError("account_key is required to subscribe to inbound texts")
    query = {"accountKey": account_key}

    listing = _as_dict(await http.json("GET", CHANNELS))
    channel: dict[str, Any] | None = None
    for c in _as_list(listing.get("items")):
        url = _as_dict(_as_dict(_as_dict(c).get("webhookChannelData")).get("webhook")).get("url")
        if url == webhook_url:
            channel = _as_dict(c)
            break
    if channel is None:
        channel = _as_dict(
            await http.json(
                "POST",
                f"{CHANNELS}/{channel_prefix}-{_base36(int(time.time() * 1000))}",
                {"channelType": "Webhook", "webhookChannelData": {"webhook": {"url": webhook_url}}},
            )
        )
    channel_id = channel.get("channelId")
    if not channel_id or not isinstance(channel_id, str):
        raise RuntimeError("GoTo made no notification channel (no channelId in the answer)")

    have = _as_dict(await http.json("GET", SUBSCRIPTIONS, query=query))
    sub: dict[str, Any] | None = None
    for s in _as_list(have.get("items")):
        s = _as_dict(s)
        if s.get("channelId") == channel_id and EVENT in _as_list(s.get("eventTypes")):
            sub = s
            break
    if sub is None:

        async def create(owner_entity_type: str) -> dict[str, Any]:
            return _as_dict(
                await http.json(
                    "POST",
                    SUBSCRIPTIONS,
                    {
                        "accountKey": account_key,
                        "ownerEntity": account_key,
                        "ownerEntityType": owner_entity_type,
                        "channelId": channel_id,
                        "eventTypes": [EVENT],
                    },
                    query=query,
                )
            )

        try:
            sub = await create("ACCOUNT")
        except GoToApiError as err:
            # GoTo's guide shows the organization as owner; try that shape on a 4xx first.
            if not 400 <= err.status < 500:
                raise
            sub = await create("ORGANIZATION")
    subscription_id = sub.get("id")
    if not subscription_id or not isinstance(subscription_id, str):
        raise RuntimeError("GoTo made no subscription (no id in the answer)")

    await store.set(tenant, KEY_CHANNEL, channel_id)
    await store.set(tenant, KEY_SUBSCRIPTION, subscription_id)
    await store.set(tenant, KEY_ACCOUNT, account_key)
    await ensure_webhook_secret(store, tenant)
    return {"channel_id": channel_id, "subscription_id": subscription_id}


async def get_webhook_secret(store: GoToSmsStore, tenant: str) -> str | None:
    """The stored webhook path secret, or None if none was made yet."""
    v = await store.get(tenant, KEY_SECRET)
    return v if isinstance(v, str) and v != "" else None


async def ensure_webhook_secret(store: GoToSmsStore, tenant: str) -> str:
    """The webhook secret, created on first use. Callers need it before enable_webhook, because
    the webhook URL they pass in contains it."""
    have = await get_webhook_secret(store, tenant)
    if have:
        return have
    secret = _new_secret()
    await store.set(tenant, KEY_SECRET, secret)
    return secret


async def disable_webhook(
    http: GoToHttp, store: GoToSmsStore, tenant: str, *, rotate_secret: bool = True
) -> None:
    """Delete the stored subscription (a 404 means it's already gone), clear the stored ids and
    rotate the webhook secret so the old webhook URL stops authenticating. A failed DELETE raises
    and leaves everything as it was, so disable can be retried. `rotate_secret=False` keeps the
    secret, for moving the subscription to another account on the same URL.
    """
    subscription_id = await store.get(tenant, KEY_SUBSCRIPTION)
    account_key = await store.get(tenant, KEY_ACCOUNT)
    if isinstance(subscription_id, str):
        try:
            await http.json(
                "DELETE",
                f"{SUBSCRIPTIONS}/{quote(subscription_id, safe='')}",
                query={"accountKey": account_key} if isinstance(account_key, str) else None,
            )
        except GoToApiError as err:
            if err.status != 404:
                raise
    await store.delete(tenant, KEY_CHANNEL)
    await store.delete(tenant, KEY_SUBSCRIPTION)
    await store.delete(tenant, KEY_ACCOUNT)
    if rotate_secret:
        await store.set(tenant, KEY_SECRET, _new_secret())


async def get_webhook_account(store: GoToSmsStore, tenant: str) -> str | None:
    """The account the stored subscription is on, or None when there is none."""
    v = await store.get(tenant, KEY_ACCOUNT)
    return v if isinstance(v, str) and v != "" else None


def check_webhook_secret(given: str, expected: str) -> bool:
    """Constant-time compare; False on length mismatch, and an empty secret never matches."""
    a, b = given.encode(), expected.encode()
    return len(b) > 0 and len(a) == len(b) and hmac.compare_digest(a, b)


def _pick(v: Any) -> str:
    """A number from a string or a touchpoint-like object ({phoneNumber|number|value|id})."""
    if isinstance(v, str):
        return v
    if not isinstance(v, dict):
        return ""
    for k in ("phoneNumber", "number", "value", "id"):
        x = v.get(k)
        if isinstance(x, str) and x:
            return x
    return ""


def _truthy(v: Any) -> bool:
    """JavaScript truthiness, for the `type`/`direction` checks (`[]` and `{}` are truthy)."""
    if isinstance(v, (list, dict)):
        return True
    return bool(v)


def parse_webhook_envelope(body: object) -> InboundMessage | None:
    """Tolerant parse of a GoTo webhook body. Returns None for anything that isn't an inbound
    text with a usable sender and body; never raises. The id is GoTo's, else a stable hash so a
    redelivery dedupes. The keyword is left unset and nothing is persisted (the caller does that).
    """
    try:
        return _parse(body)
    except Exception:  # noqa: BLE001 - the contract is "never raises", whatever the body holds
        return None


def _parse(body: object) -> InboundMessage | None:
    if not isinstance(body, dict):
        return None
    if _truthy(body.get("type")) and body.get("type") != EVENT:
        return None
    content = _as_dict(body.get("content"))
    p = next(
        (
            c
            for c in (content.get("payload"), body.get("payload"), body.get("data"), body)
            if isinstance(c, dict)
        ),
        None,
    )
    if p is None:
        return None
    if _truthy(p.get("direction")) and p.get("direction") != "INBOUND":
        return None

    contacts = _as_list(p.get("contactTouchpoints"))
    from_raw = (
        _pick(p.get("authorTouchpoint"))
        or _pick(p.get("from"))
        or _pick(p.get("contactPhoneNumber"))
        or _pick(contacts[0] if contacts else None)
    )
    sender = normalize_phone(from_raw)
    raw_text = next(
        (x for x in (p.get("body"), p.get("text"), p.get("message")) if x is not None), ""
    )
    text = (raw_text if isinstance(raw_text, str) else "").strip()[:MAX_TEXT]
    if not sender or not text:
        return None

    to_raw = (
        _pick(p.get("ownerPhoneNumber")) or _pick(p.get("to")) or _pick(p.get("ownerTouchpoint"))
    )
    to = normalize_phone(to_raw) or to_raw
    timestamp = next(
        (
            t
            for t in (p.get("timestamp"), p.get("createdAt"), body.get("timestamp"))
            if isinstance(t, str) and t
        ),
        "",
    )
    pid = p.get("id")
    msg_id = (
        pid
        if isinstance(pid, str) and pid
        else hashlib.sha256(f"{sender}|{to}|{text}|{timestamp}".encode()).hexdigest()
    )
    return InboundMessage(
        id=msg_id,
        from_=sender,
        to=to,
        text=text,
        received_at=timestamp or iso_ms(time.time() * 1000),
        raw=body,
    )
