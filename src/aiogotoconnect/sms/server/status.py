"""The setup page's status payload (port of Node server/status.ts). One builder for both
GET /api/status and sms.status(). Keys are Node's camelCase exactly (contract/http.md)."""

from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from aiogotoconnect.sms.config import ConfigField, Source
from aiogotoconnect.sms.core import IncomingMode, ResolvedOptions
from aiogotoconnect.sms.errors import GoToSmsError
from aiogotoconnect.sms.http import error_name
from aiogotoconnect.sms.inbound.webhook import get_webhook_secret
from aiogotoconnect.sms.store.types import Json
from aiogotoconnect.sms.types import VERSION

if TYPE_CHECKING:
    from aiogotoconnect.sms.core import Core

# Scopes the admin must tick on their GoTo OAuth client (shown on the setup page).
REQUIRED_SCOPES: list[str] = [
    "messaging.v1.send",
    "messaging.v1.read",
    "messaging.v1.notifications.manage",
    "voice-admin.v1.read",
]

# Store key: why moving the webhook to a newly selected account failed (`incoming.error`).
INBOUND_ERROR = "inbound:error"


class IncomingError(TypedDict):
    """A failed webhook re-subscribe: the package error code and GoTo's HTTP status, no body."""

    error: str
    message: str
    gotoStatus: NotRequired[int]


class IncomingStatus(TypedDict):
    mode: IncomingMode
    # Only in GET /api/status (behind authorize), never from sms.status(): it holds the secret.
    webhookUrl: NotRequired[str]
    # public_url is https.
    canWebhook: bool
    # Set when the account changed in webhook mode and moving the subscription failed.
    error: NotRequired[IncomingError]


class BackPayload(TypedDict):
    url: str
    label: str


class StatusPayload(TypedDict):
    """Never carries the client secret, tokens or the refresh token (C22). The webhook secret
    appears only inside `incoming.webhookUrl`, which only the authorized GET /api/status
    includes; programmatic sms.status() omits it (R30)."""

    version: str
    configured: bool
    connected: bool
    loginExpired: bool
    credentialsUnreadable: bool
    ready: bool
    clientId: NotRequired[str]
    accountKey: NotRequired[str]
    fromNumber: NotRequired[str]
    invalidFromNumber: NotRequired[Literal[True]]
    sources: dict[ConfigField, Source]
    refreshSource: NotRequired[Literal["login", "seeded-from-env"]]
    redirectUri: str
    incoming: IncomingStatus
    requiredScopes: list[str]
    back: NotRequired[BackPayload]
    startupError: NotRequired[str]


def startup_error_text(err: BaseException) -> str:
    """A secret-free description of a startup failure. Package errors carry fixed messages;
    anything else is named only, since its message could hold a path or a value."""
    if isinstance(err, GoToSmsError):
        return str(err)
    return f"goto-sms failed to start ({error_name(err)}); see the server logs"


def _as_incoming_error(v: Json) -> IncomingError | None:
    if not isinstance(v, dict):
        return None
    error, message, goto_status = v.get("error"), v.get("message"), v.get("gotoStatus")
    if not isinstance(error, str) or not isinstance(message, str):
        return None
    out: IncomingError = {"error": error, "message": message}
    # JS `typeof === 'number'`; booleans are not numbers. HTTP statuses are always integers.
    if isinstance(goto_status, int) and not isinstance(goto_status, bool):
        out["gotoStatus"] = goto_status
    return out


def _unset() -> dict[ConfigField, Source]:
    return {
        "clientId": "unset",
        "clientSecret": "unset",
        "accountKey": "unset",
        "fromNumber": "unset",
    }


async def build_status(
    options: ResolvedOptions,
    core: "Core | None" = None,
    startup_error: str | None = None,
    *,
    include_webhook_url: bool = False,
) -> StatusPayload:
    """Without a core (startup failed, e.g. for lack of configuration) everything reads as not
    set up, with `startupError` saying why. `include_webhook_url` is for the authorized HTTP
    route only; off by default so no other caller can leak the webhook secret."""
    can_webhook = (options.public_url or "").startswith("https://")
    redirect_uri = options.redirect_uri or ""
    back: BackPayload | None = (
        {"url": options.back.url, "label": options.back.label} if options.back else None
    )

    if core is None:
        bare: StatusPayload = {
            "version": VERSION,
            "redirectUri": redirect_uri,
            "requiredScopes": list(REQUIRED_SCOPES),
            "configured": False,
            "connected": False,
            "loginExpired": False,
            "credentialsUnreadable": False,
            "ready": False,
            "sources": _unset(),
            "incoming": {"mode": "off", "canWebhook": can_webhook},
        }
        if back:
            bare["back"] = back
        if startup_error:
            bare["startupError"] = startup_error
        return bare

    auth = await core.auth.status()
    r = await core.config.resolve()
    mode = await core.incoming_mode()
    secret = (
        await get_webhook_secret(core.store, core.tenant)
        if include_webhook_url and mode == "webhook"
        else None
    )
    webhook_url = core.webhook_url(secret) if secret else None
    configured = bool(r.client_id and r.client_secret) and not r.credentials_unreadable
    error = _as_incoming_error(await core.store.get(core.tenant, INBOUND_ERROR))

    incoming: IncomingStatus = {"mode": mode, "canWebhook": can_webhook}
    if webhook_url:
        incoming["webhookUrl"] = webhook_url
    if error:
        incoming["error"] = error

    s: StatusPayload = {
        "version": VERSION,
        "redirectUri": redirect_uri,
        "requiredScopes": list(REQUIRED_SCOPES),
        "configured": configured,
        "connected": auth.connected,
        "loginExpired": auth.login_expired,
        "credentialsUnreadable": r.credentials_unreadable or auth.tokens_unreadable,
        "ready": configured and auth.connected and r.from_number is not None,
        "sources": dict(r.sources),
        "incoming": incoming,
    }
    if back:
        s["back"] = back
    if r.client_id is not None:
        s["clientId"] = r.client_id
    if r.account_key is not None:
        s["accountKey"] = r.account_key
    if r.from_number is not None:
        s["fromNumber"] = r.from_number
    if r.invalid_from_number:
        s["invalidFromNumber"] = True
    if auth.refresh_source:
        s["refreshSource"] = auth.refresh_source
    return s
