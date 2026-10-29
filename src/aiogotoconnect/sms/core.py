"""The wired-up module instances the setup handlers work on (port of Node client.ts `Core` and
`ResolvedOptions`). Internal: create_goto_sms builds and fills these; handlers only read them.

Component types are imported for type checking only, so importing this module never pulls in
`cryptography` (the optional [sms] extra) and never creates an import cycle.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from aiogotoconnect.sms.inbound.keywords import DEFAULT_REPLIES, KeywordReplies
from aiogotoconnect.sms.store.types import GoToSmsStore

if TYPE_CHECKING:
    import aiohttp

    from aiogotoconnect.sms.config import ConfigResolver
    from aiogotoconnect.sms.http import GoToHttp
    from aiogotoconnect.sms.inbound.poller import Poller
    from aiogotoconnect.sms.inbound.processor import InboundProcessor
    from aiogotoconnect.sms.log import MessageLog
    from aiogotoconnect.sms.oauth import GoToAuth
    from aiogotoconnect.sms.optouts import OptOuts
    from aiogotoconnect.sms.secrets import SecretBox
    from aiogotoconnect.sms.send import Sender

IncomingMode = Literal["webhook", "polling", "off"]
KeySource = Literal["option", "env", "keyfile"]

DEFAULT_AUTH_BASE = "https://authentication.logmeininc.com"
DEFAULT_API_BASE = "https://api.goto.com"


@dataclass(frozen=True)
class AuthorizeRequest:
    """What `authorize` receives: a read-only view of the request (Node passes the raw request).
    `path` is relative to the mount; header names are lower-case."""

    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    cookies: Mapping[str, str]


Authorize = Callable[[AuthorizeRequest], bool | Awaitable[bool]]


@dataclass(frozen=True)
class BackLink:
    """The setup page's "← Back to <label>" link (wire: `back: {url, label}`)."""

    url: str
    label: str


def _now_ms() -> float:
    return time.time() * 1000


@dataclass
class ResolvedOptions:
    """Options with defaults applied (Node's ResolvedOptions, snake_case; intervals in seconds)."""

    # No trailing slash; None when neither the option nor GOTO_SMS_PUBLIC_URL is set.
    public_url: str | None = None
    # Leading slash, no trailing slash; "" for the root mount (so URLs never get "//").
    mount_path: str = "/goto-sms"
    # public_url + mount_path + "/callback", or None without a public_url.
    redirect_uri: str | None = None
    data_dir: str | None = None
    authorize: Authorize | None = None
    allow_unauthenticated_setup: bool = False
    back: BackLink | None = None
    login_url: str | None = None
    log_only: bool = False
    replies: KeywordReplies = field(default_factory=lambda: DEFAULT_REPLIES.copy())
    yes_is_opt_in: bool = True
    poll_interval_s: float = 30.0
    send_timeout_s: float = 10.0
    inbound_per_hour: int = 10
    log_retention_days: float = 90
    # Node's `fetch`: the aiohttp session GoTo calls go through (None until the facade makes one).
    session: "aiohttp.ClientSession | None" = None
    auth_base_url: str = DEFAULT_AUTH_BASE
    api_base_url: str = DEFAULT_API_BASE
    env: Mapping[str, str] = field(default_factory=dict)
    # Epoch milliseconds, as everywhere in the package.
    now: Callable[[], float] = _now_ms


@dataclass
class Core:
    """Every module instance, for the setup handlers. Internal; may change in any release."""

    store: GoToSmsStore
    tenant: str
    box: "SecretBox"
    key_source: KeySource
    config: "ConfigResolver"
    auth: "GoToAuth"
    http: "GoToHttp"
    sender: "Sender"
    opt_outs: "OptOuts"
    log: "MessageLog"
    processor: "InboundProcessor"
    poller: "Poller"
    logger: logging.Logger
    options: ResolvedOptions
    # Background work started by handlers (post-204 inbound processing); close() awaits it.
    # Each task removes itself when done.
    inflight: "set[asyncio.Task[Any]]" = field(default_factory=set)

    async def incoming_mode(self) -> IncomingMode:
        """Stored `inbound:mode`; "off" when unset or unrecognized."""
        mode = await self.store.get(self.tenant, "inbound:mode")
        if mode == "webhook":
            return "webhook"
        if mode == "polling":
            return "polling"
        return "off"

    def webhook_url(self, secret: str) -> str | None:
        """public_url + mount_path + "/inbound/" + secret, or None without a public_url."""
        public_url = self.options.public_url
        if not public_url:
            return None
        return f"{public_url}{self.options.mount_path}/inbound/{secret}"
