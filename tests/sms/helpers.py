"""Port of packages/node/test/helpers/sms.ts.

Node's `connectedSms` builds a connected createGoToSms instance. Until the Python facade
exists (Task 11), `connected_core` wires the same modules into a `Core` by hand, with the same
defaults: a MemoryStore, publicUrl https://app.example, mount /goto-sms, env {}, configured
(client id/secret, from number) and connected through ConfigResolver.save + finish_connect.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from aiogotoconnect.sms.client import GoToSms, create_goto_sms
from aiogotoconnect.sms.config import ConfigResolver, config_value
from aiogotoconnect.sms.core import Authorize, BackLink, Core, ResolvedOptions
from aiogotoconnect.sms.http import create_goto_http
from aiogotoconnect.sms.inbound.keywords import DEFAULT_REPLIES
from aiogotoconnect.sms.inbound.poller import Poller
from aiogotoconnect.sms.inbound.processor import InboundProcessor, MessageHandler, call_handlers
from aiogotoconnect.sms.log import MessageLog
from aiogotoconnect.sms.oauth import GoToAuth
from aiogotoconnect.sms.optouts import OptOuts
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.send import Sender
from aiogotoconnect.sms.store import MemoryStore
from aiogotoconnect.sms.types import InboundMessage

from .fake_goto import FakeGoTo

FROM = "+18015550100"  # our sending number
PEER = "+18015550123"  # the person texting in
LOGGER = logging.getLogger("aiogotoconnect.sms.tests")


def state_of(url: str) -> str:
    """The `state` query parameter of a GoTo authorize URL."""
    return parse_qs(urlsplit(url).query)["state"][0]


@dataclass
class SmsRig:
    """What Node's tests reach through `sms` and `sms.core`."""

    core: Core
    session: aiohttp.ClientSession
    # Stand-in for sms.on("message", ...): every handler here gets each inbound message.
    handlers: list[MessageHandler] = field(default_factory=list)
    got: list[InboundMessage] = field(default_factory=list)

    async def close(self) -> None:
        """What GoToSms.close() will do: stop polling, wait for in-flight work, then let go."""
        self.core.poller.stop()
        await self.core.poller.idle()
        if self.core.inflight:
            await asyncio.gather(*self.core.inflight, return_exceptions=True)
        await self.session.close()


def _normalize_mount(p: str) -> str:
    trimmed = p.rstrip("/")
    if trimmed == "":
        return ""
    return trimmed if trimmed.startswith("/") else f"/{trimmed}"


async def connected_core(
    fake: FakeGoTo,
    *,
    connect: bool = True,
    authorize: Authorize | None = None,
    allow_unauthenticated_setup: bool = False,
    env: dict[str, str] | None = None,
    public_url: str | None = "https://app.example",
    mount_path: str = "/goto-sms",
    account_key: str | None = None,
    back: BackLink | None = None,
    login_url: str | None = None,
    now: Callable[[], float] | None = None,
    logger: logging.Logger = LOGGER,
    tenant: str = "default",
    timeout_s: float = 10.0,
) -> SmsRig:
    env = {} if env is None else env
    raw_url = config_value(public_url)
    url = raw_url.rstrip("/") if raw_url else None
    mount = _normalize_mount(mount_path)
    store = MemoryStore(now) if now else MemoryStore()
    options = ResolvedOptions(
        public_url=url,
        mount_path=mount,
        redirect_uri=f"{url}{mount}/callback" if url else None,
        authorize=authorize,
        allow_unauthenticated_setup=allow_unauthenticated_setup,
        back=back,
        login_url=login_url,
        auth_base_url=fake.auth_base_url,
        api_base_url=fake.api_base_url,
        env=env,
        **({"now": now} if now else {}),
    )
    clock = options.now
    session = aiohttp.ClientSession()
    box = SecretBox(os.urandom(32))
    config_options: dict[str, str | None] = {}
    if account_key is not None:
        config_options["accountKey"] = account_key
    config = ConfigResolver(store, tenant, box, config_options, env)
    auth = GoToAuth(
        store=store,
        tenant=tenant,
        box=box,
        config=config,
        session=session,
        auth_base_url=options.auth_base_url,
        redirect_uri=options.redirect_uri or "",
        logger=logger,
        now=clock,
    )
    http = create_goto_http(
        session,
        options.api_base_url,
        auth.get_access_token,
        auth.force_refresh,
        timeout_s=timeout_s,
        logger=logger,
    )
    opt_outs = OptOuts(store, tenant, clock)
    log = MessageLog(store, tenant, clock, retention_days=options.log_retention_days)
    sender = Sender(
        http=http,
        auth=auth,
        config=config,
        opt_outs=opt_outs,
        log=log,
        store=store,
        tenant=tenant,
        now=clock,
        send_timeout_s=options.send_timeout_s,
        max_body=1600,
        log_only=False,
        env=env,
        logger=logger,
    )
    handlers: list[MessageHandler] = []

    async def emit(m: InboundMessage) -> None:
        await call_handlers(list(handlers), m, logger)

    processor = InboundProcessor(
        store=store,
        tenant=tenant,
        sender=sender,
        opt_outs=opt_outs,
        log=log,
        emit=emit,
        replies=DEFAULT_REPLIES.copy(),
        yes_is_opt_in=True,
        per_hour=10,
        now=clock,
        logger=logger,
    )
    poller = Poller(
        http=http,
        config=config,
        processor=processor,
        store=store,
        tenant=tenant,
        poll_interval_s=options.poll_interval_s,
        now=clock,
        logger=logger,
    )
    core = Core(
        store=store,
        tenant=tenant,
        box=box,
        key_source="option",
        config=config,
        auth=auth,
        http=http,
        sender=sender,
        opt_outs=opt_outs,
        log=log,
        processor=processor,
        poller=poller,
        logger=logger,
        options=options,
    )
    rig = SmsRig(core=core, session=session, handlers=handlers)
    handlers.append(rig.got.append)
    if connect:
        await config.save({"clientId": "cid", "clientSecret": "csecret", "fromNumber": FROM})
        await auth.finish_connect("code-ok", state_of(await auth.start_connect()))
    return rig


def recorder() -> tuple[logging.Logger, list[logging.LogRecord]]:
    """A logger that records every line, for asserting what is (and never is) logged."""
    records: list[logging.LogRecord] = []

    class _Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(f"aiogotoconnect.sms.tests.rec{id(records)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(_Keep())
    return logger, records


async def connected_sms(fake: FakeGoTo, *, connect: bool = True, **opts: Any) -> GoToSms:
    """Port of Node's `connectedSms`: create_goto_sms on the fake with a MemoryStore, started,
    then configured (client id/secret, from number) and connected via save + finish_connect."""
    defaults: dict[str, Any] = {
        "encryption_key": "test-key",
        "public_url": "https://app.example",
        "env": {},
        "logger": LOGGER,
        "auth_base_url": fake.url,
        "api_base_url": fake.url,
    }
    if "store" not in opts:
        now = opts.get("now")
        defaults["store"] = MemoryStore(now) if now else MemoryStore()
    sms = create_goto_sms(**{**defaults, **opts})
    await sms.start()
    if connect:
        core = sms.core
        await core.config.save({"clientId": "cid", "clientSecret": "csecret", "fromNumber": FROM})
        await core.auth.finish_connect("code-ok", state_of(await core.auth.start_connect()))
    return sms
