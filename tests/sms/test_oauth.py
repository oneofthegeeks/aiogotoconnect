"""Port of packages/node/test/oauth.test.ts (C1-C5, seeding, CAS losses, HTTP 401 handling)."""

import asyncio
import base64
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import (
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    TemporaryError,
)
from aiogotoconnect.sms.http import GoToHttp, create_goto_http
from aiogotoconnect.sms.oauth import AuthStatus, GoToAuth
from aiogotoconnect.sms.secrets import SecretBox
from aiogotoconnect.sms.store import FileStore, GoToSmsStore, Json, MemoryStore

from .fake_goto import FakeGoTo, FakeRequest, FakeResponse

T = "t1"
REDIRECT = "https://app.example/goto/callback"
BASE_ENV = {"GOTO_CLIENT_ID": "cid", "GOTO_CLIENT_SECRET": "csecret"}
SILENT = logging.getLogger("aiogotoconnect.sms.test_oauth")
HOUR_MS = 3600_000


@dataclass
class Clock:
    t: float = 1_000_000_000


@dataclass
class Rig:
    fake: FakeGoTo
    clock: Clock
    store: GoToSmsStore
    box: SecretBox
    auth: GoToAuth
    make: Callable[[], GoToAuth]
    session: aiohttp.ClientSession

    def token_calls(self) -> int:
        return sum(1 for r in self.fake.requests if r.path == "/oauth/token")

    async def connect(self, a: GoToAuth | None = None) -> None:
        a = a or self.auth
        await a.finish_connect("code-ok", state_of(await a.start_connect()))

    def http(self) -> GoToHttp:
        return create_goto_http(
            self.session,
            self.fake.api_base_url,
            self.auth.get_access_token,
            self.auth.force_refresh,
        )


def state_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def form_of(body: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


class Gate:
    """Holds the next token-endpoint request once armed, so a test can change the store
    mid-refresh (Node's gatedRefreshFetch)."""

    def __init__(self) -> None:
        self.armed = False
        self.reached = asyncio.Event()
        self.released = asyncio.Event()

    def trace(self) -> aiohttp.TraceConfig:
        async def on_start(
            _s: aiohttp.ClientSession,
            _ctx: SimpleNamespace,
            params: aiohttp.TraceRequestStartParams,
        ) -> None:
            if self.armed and params.url.path == "/oauth/token":
                self.armed = False
                self.reached.set()
                await self.released.wait()

        tc = aiohttp.TraceConfig()
        tc.on_request_start.append(on_start)
        return tc


RigFactory = Callable[..., Awaitable[Rig]]


@pytest.fixture
async def setup(fake_goto: FakeGoTo) -> AsyncIterator[RigFactory]:
    sessions: list[aiohttp.ClientSession] = []

    async def make_rig(
        *,
        env: Mapping[str, str] | None = None,
        store: GoToSmsStore | None = None,
        box: SecretBox | None = None,
        timeout_s: float | None = None,
        logger: logging.Logger | None = None,
        gate: Gate | None = None,
    ) -> Rig:
        session = aiohttp.ClientSession(trace_configs=[gate.trace()] if gate else None)
        sessions.append(session)
        clock = Clock()
        store_ = store if store is not None else MemoryStore()
        box_ = box or SecretBox(os.urandom(32))
        env_ = BASE_ENV if env is None else env

        def make() -> GoToAuth:
            return GoToAuth(
                store=store_,
                tenant=T,
                box=box_,
                config=ConfigResolver(store_, T, box_, {}, env_),
                session=session,
                auth_base_url=fake_goto.auth_base_url,
                redirect_uri=REDIRECT,
                logger=logger or SILENT,
                now=lambda: clock.t,
                **({"timeout_s": timeout_s} if timeout_s else {}),
            )

        return Rig(fake_goto, clock, store_, box_, make(), make, session)

    yield make_rig
    for s in sessions:
        await s.close()


def has_code(code: str) -> Callable[[BaseException], bool]:
    return lambda err: isinstance(err, GoToSmsError) and err.code == code


async def test_c1_authorize_url_has_no_scope_or_prompt(setup: RigFactory) -> None:
    r = await setup()
    u = urlsplit(await r.auth.start_connect())
    assert f"{u.scheme}://{u.netloc}{u.path}" == f"{r.fake.url}/oauth/authorize"
    q = parse_qs(u.query)
    assert sorted(q) == ["client_id", "redirect_uri", "response_type", "state"]
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == [REDIRECT]
    assert q["response_type"] == ["code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", q["state"][0])


async def test_c2_token_call_basic_auth_form_body_no_client_secret(setup: RigFactory) -> None:
    r = await setup()
    r.fake.tokens.codes["code-ok"] = REDIRECT  # the fake now also checks redirect_uri
    await r.connect()
    req = next(x for x in r.fake.requests if x.path == "/oauth/token")
    assert req.method == "POST"
    assert req.headers["authorization"] == f"Basic {base64.b64encode(b'cid:csecret').decode()}"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = form_of(req.body)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-ok"
    assert form["redirect_uri"] == REDIRECT
    assert "client_secret" not in form
    assert "client_id" not in form
    assert await r.auth.status() == AuthStatus(
        connected=True, login_expired=False, refresh_source="login"
    )


async def test_c3_state_single_use_and_expires_after_10_min(setup: RigFactory) -> None:
    r = await setup()
    s1 = state_of(await r.auth.start_connect())
    await r.auth.finish_connect("code-ok", s1)
    with pytest.raises(GoToSmsError) as e:
        await r.auth.finish_connect("code-ok", s1)
    assert e.value.code == "bad_state"

    s2 = state_of(await r.auth.start_connect())
    r.clock.t += 10 * 60_000 + 1
    with pytest.raises(GoToSmsError) as e:
        await r.auth.finish_connect("code-ok", s2)
    assert e.value.code == "bad_state"
    assert await r.store.get(T, f"state:{s2}") is None, "expired state deleted on use"

    # Wrong length / junk: a clean bad_state, never an error from compare_digest.
    for junk in ["", "short", s2 + "x", "a" * 43 + "é", "../tokens", "a" * 43 + "\n"]:
        with pytest.raises(GoToSmsError) as e:
            await r.auth.finish_connect("code-ok", junk)
        assert e.value.code == "bad_state"


async def test_c3_start_connect_sweeps_expired_states(setup: RigFactory) -> None:
    r = await setup()
    old = state_of(await r.auth.start_connect())
    r.clock.t += 10 * 60_000 + 1
    fresh = state_of(await r.auth.start_connect())
    assert [k for k, _ in await r.store.list(T, "state:")] == [f"state:{fresh}"]
    assert old != fresh


async def test_c3_two_pending_states_both_work(setup: RigFactory) -> None:
    r = await setup()
    a = state_of(await r.auth.start_connect())
    b = state_of(await r.auth.start_connect())
    await r.auth.finish_connect("code-ok", b)
    await r.auth.finish_connect("code-ok", a)
    assert (await r.auth.status()).connected is True


class YieldingStore(MemoryStore):
    """Yields to the loop after each list(), as a networked store would, so two callbacks can
    both see a state before either claims it."""

    async def list(self, tenant: str, prefix: str) -> list[tuple[str, Json]]:
        entries = await super().list(tenant, prefix)
        await asyncio.sleep(0)
        return entries


async def test_c3_concurrent_callbacks_with_one_state_only_one_wins(setup: RigFactory) -> None:
    r = await setup(store=YieldingStore())
    s = state_of(await r.auth.start_connect())
    results = await asyncio.gather(
        r.auth.finish_connect("code-ok", s),
        r.auth.finish_connect("code-ok", s),
        return_exceptions=True,
    )
    assert sum(1 for x in results if x is None) == 1
    assert sum(1 for x in results if has_code("bad_state")(cast(BaseException, x))) == 1


async def test_state_survives_a_restart(setup: RigFactory, tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    box = SecretBox(os.urandom(32))
    a = await setup(box=box, store=FileStore(path))
    state = state_of(await a.auth.start_connect())

    b = await setup(box=box, store=FileStore(path))
    await b.auth.finish_connect("code-ok", state)
    assert (await b.auth.status()).connected is True
    assert isinstance(await b.auth.get_access_token(), str)


async def test_finish_connect_rejected_code_keeps_existing_login(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    before = await r.auth.get_access_token()
    with pytest.raises(LoginExpiredError):
        await r.auth.finish_connect("code-bad", state_of(await r.auth.start_connect()))
    assert await r.auth.get_access_token() == before
    assert (await r.auth.status()).login_expired is False


async def test_token_reused_until_60s_before_expiry(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    assert r.token_calls() == 1
    first = await r.auth.get_access_token()
    stored = cast(dict[str, Any], await r.store.get(T, "tokens"))
    assert stored["access"] != first, "access token is sealed at rest"
    assert r.box.open(stored["access"]) == first

    r.clock.t += HOUR_MS - 60_000 - 1  # 1 ms before the reuse window closes
    assert await r.auth.get_access_token() == first
    assert r.token_calls() == 1

    r.clock.t += 1
    second = await r.auth.get_access_token()
    assert second != first
    assert r.token_calls() == 2


async def test_c4_single_flight_one_refresh(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    r.clock.t += HOUR_MS
    tokens = await asyncio.gather(*(r.auth.get_access_token() for _ in range(5)))
    assert len(set(tokens)) == 1
    assert r.token_calls() == 2  # code exchange + one refresh


async def test_c4_single_flight_survives_a_cancelled_waiter(setup: RigFactory) -> None:
    gate = Gate()
    r = await setup(gate=gate)
    await r.connect()
    r.clock.t += HOUR_MS
    gate.armed = True
    first = asyncio.create_task(r.auth.get_access_token())
    await gate.reached.wait()
    second = asyncio.create_task(r.auth.get_access_token())
    await asyncio.sleep(0)
    first.cancel()  # one caller gives up; the shared refresh must keep going for the other
    gate.released.set()
    token = await second
    assert first.cancelled()
    assert isinstance(token, str)
    assert r.token_calls() == 2


async def test_c4_rotation_stored_and_non_rotated_kept(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()

    async def refresh_of() -> str:
        return r.box.open(cast(dict[str, str], await r.store.get(T, "tokens"))["refresh"])

    assert await refresh_of() == "refresh-1"

    r.clock.t += HOUR_MS
    await r.auth.get_access_token()  # rotate_on_refresh False: no refresh_token in the answer
    assert await refresh_of() == "refresh-1"

    r.fake.tokens.rotate_on_refresh = True
    r.clock.t += HOUR_MS
    await r.auth.get_access_token()
    assert await refresh_of() != "refresh-1"
    assert await refresh_of() == r.fake.tokens.refresh
    assert (await r.auth.status()).refresh_source == "login"

    # And the rotated token really works for the next refresh.
    r.clock.t += HOUR_MS
    await r.auth.get_access_token()
    assert (await r.auth.status()).login_expired is False


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_c5_refresh_refused_flags_login_expired_and_fails_fast(
    setup: RigFactory, status: int
) -> None:
    r = await setup()
    await r.connect()
    r.clock.t += HOUR_MS
    r.fake.fail_next("POST", "/oauth/token", status, {"error": "invalid_grant"})
    with pytest.raises(LoginExpiredError):
        await r.auth.get_access_token()
    assert await r.store.get(T, "login_expired") is True
    assert await r.auth.status() == AuthStatus(
        connected=False, login_expired=True, refresh_source="login"
    )

    calls = r.token_calls()
    with pytest.raises(LoginExpiredError):
        await r.auth.get_access_token()
    with pytest.raises(LoginExpiredError):
        await r.auth.force_refresh()
    assert r.token_calls() == calls, "no network once the login is dead"

    # A new login clears the flag.
    await r.connect()
    assert await r.store.get(T, "login_expired") is None
    assert isinstance(await r.auth.get_access_token(), str)


async def test_c5_refresh_503_temporary_no_flag_tokens_untouched(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    before = await r.store.get(T, "tokens")
    r.clock.t += HOUR_MS
    r.fake.fail_next("POST", "/oauth/token", 503)
    with pytest.raises(TemporaryError):
        await r.auth.get_access_token()
    assert await r.store.get(T, "login_expired") is None
    assert await r.store.get(T, "tokens") == before
    # Recovers on the next call (no internal retry; one attempt per call).
    assert isinstance(await r.auth.get_access_token(), str)


async def test_c5_refresh_hang_temporary_after_timeout_no_flag(setup: RigFactory) -> None:
    r = await setup(timeout_s=0.05)
    await r.connect()
    r.clock.t += HOUR_MS
    r.fake.hang_next("POST", "/oauth/token")
    with pytest.raises(TemporaryError) as e:
        await r.auth.get_access_token()
    assert e.value.request_sent is None
    assert await r.store.get(T, "login_expired") is None


async def test_not_configured_missing_unreadable_never_connected(setup: RigFactory) -> None:
    none = await setup(env={})
    with pytest.raises(NotConfiguredError):
        await none.auth.start_connect()
    with pytest.raises(NotConfiguredError):
        await none.auth.get_access_token()

    # Stored client secret sealed with a different key -> credentials_unreadable.
    store = MemoryStore()
    await store.set(T, "config:clientId", "cid")
    await store.set(T, "config:clientSecret", SecretBox(os.urandom(32)).seal("csecret"))
    unreadable = await setup(env={}, store=store)
    with pytest.raises(NotConfiguredError):
        await unreadable.auth.start_connect()
    with pytest.raises(NotConfiguredError):
        await unreadable.auth.get_access_token()

    fresh = await setup()
    with pytest.raises(NotConfiguredError) as e:
        await fresh.auth.get_access_token()
    assert str(e.value) == "Not connected to GoTo — open the setup page and click Connect"
    assert await fresh.auth.status() == AuthStatus(connected=False, login_expired=False)


async def test_disconnect_deletes_tokens_and_login_expired(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    r.clock.t += HOUR_MS
    r.fake.fail_next("POST", "/oauth/token", 400)
    with pytest.raises(LoginExpiredError):
        await r.auth.get_access_token()
    await r.auth.disconnect()
    assert await r.store.get(T, "tokens") is None
    assert await r.store.get(T, "login_expired") is None
    assert await r.auth.status() == AuthStatus(connected=False, login_expired=False)


async def test_status_tokens_unreadable_after_key_change(setup: RigFactory) -> None:
    store = MemoryStore()
    a = await setup(store=store)
    await a.connect()
    b = await setup(store=store)  # same store, a different encryption key
    assert await b.auth.status() == AuthStatus(
        connected=False, login_expired=False, tokens_unreadable=True, refresh_source="login"
    )
    with pytest.raises(NotConfiguredError, match="can't be decrypted"):
        await b.auth.get_access_token()


async def test_seeded_from_env_first_use_then_restart_keeps_rotated(setup: RigFactory) -> None:
    probe = await setup()
    fake = probe.fake
    fake.tokens.rotate_on_refresh = True
    seed = fake.tokens.refresh
    env = {**BASE_ENV, "GOTO_REFRESH_TOKEN": seed}
    store = MemoryStore()
    box = SecretBox(os.urandom(32))

    a = await setup(env=env, store=store, box=box)
    assert isinstance(await a.auth.get_access_token(), str)
    form = form_of(fake.requests[-1].body)
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == seed
    stored = cast(dict[str, Any], await store.get(T, "tokens"))
    assert stored["source"] == "seeded-from-env"
    assert stored["seedHash"] == hashlib.sha256(seed.encode()).hexdigest()
    assert (await a.auth.status()).refresh_source == "seeded-from-env"
    assert box.open(stored["refresh"]) != seed, "rotated away from the seed"

    # Restart with the same env, token expired: must refresh with the stored (rotated) token.
    # Re-using the seed would be refused by the fake (400 -> LoginExpiredError).
    b = await setup(env=env, store=store, box=box)
    b.clock.t += HOUR_MS
    assert isinstance(await b.auth.get_access_token(), str)
    assert form_of(fake.requests[-1].body)["refresh_token"] != seed
    assert (await b.auth.status()).refresh_source == "seeded-from-env"
    assert await store.get(T, "login_expired") is None


async def test_new_env_seed_after_login_expired_recovers(setup: RigFactory) -> None:
    store = MemoryStore()
    box = SecretBox(os.urandom(32))
    a = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-1"})
    await a.auth.get_access_token()
    # GoTo revokes the login.
    a.fake.tokens.refresh = "refresh-2"
    a.clock.t += HOUR_MS
    with pytest.raises(LoginExpiredError):
        await a.auth.get_access_token()

    # Same seed after restart: still dead, no network.
    calls = a.token_calls()
    same = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-1"})
    with pytest.raises(LoginExpiredError):
        await same.auth.get_access_token()
    assert a.token_calls() == calls

    # Operator pastes a new token into env and restarts.
    b = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-2"})
    assert isinstance(await b.auth.get_access_token(), str)
    assert await store.get(T, "login_expired") is None
    assert await b.auth.status() == AuthStatus(
        connected=True, login_expired=False, refresh_source="seeded-from-env"
    )


async def test_env_seed_never_overrides_a_browser_login(setup: RigFactory) -> None:
    store = MemoryStore()
    box = SecretBox(os.urandom(32))
    a = await setup(store=store, box=box)
    await a.connect()
    b = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "other"})
    calls = b.token_calls()
    await b.auth.get_access_token()
    assert b.token_calls() == calls
    assert (await b.auth.status()).refresh_source == "login"


async def test_http_401_one_force_refresh_one_retry_then_login_expired(
    setup: RigFactory,
) -> None:
    r = await setup()
    await r.connect()
    http = r.http()
    me = await http.json("GET", "/identity/v1/Users/me")
    assert me["urn:scim:schemas:extension:getgo:1.0"]["accounts"] == [
        {"value": "acct-1", "display": "Test Account"}
    ]
    api_req = r.fake.requests[-1]
    assert api_req.headers["authorization"].startswith("Bearer ")
    assert api_req.headers["accept"] == "application/json"
    assert "content-type" not in api_req.headers

    r.fake.tokens.valid.clear()  # GoTo revoked the access token early
    calls = r.token_calls()
    await http.json("GET", "/identity/v1/Users/me")
    assert r.token_calls() == calls + 1, "exactly one forced refresh"

    r.fake.fail_next("GET", "/identity/v1/Users/me", 401)
    r.fake.fail_next("GET", "/identity/v1/Users/me", 401)
    before = len(r.fake.requests)
    with pytest.raises(LoginExpiredError):
        await http.json("GET", "/identity/v1/Users/me")
    assert len(r.fake.requests) - before == 3  # two API calls + one refresh
    # The refresh itself worked, so the login isn't flagged dead.
    assert await r.store.get(T, "login_expired") is None


async def test_http_non_2xx_empty_body_query_and_json_body(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    seen: dict[str, Any] = {}

    def echo(req: FakeRequest) -> FakeResponse:
        seen.update(
            query=urlsplit(req.path).query, body=req.body, ct=req.headers.get("content-type")
        )
        return FakeResponse(204)

    r.fake.route("POST", "/echo", echo)
    http = r.http()
    assert await http.json("POST", "/echo", {"a": 1}, query={"x": "y z"}) == {}
    assert seen == {"query": "x=y+z", "body": '{"a":1}', "ct": "application/json"}

    r.fake.fail_next("GET", "/identity", 422, "x" * 800)
    with pytest.raises(GoToApiError) as e:
        await http.json("GET", "/identity/v1/Users/me")
    assert e.value.code == "api"
    assert e.value.status == 422
    assert len(e.value.body) == 500


async def test_http_timeout_request_sent_token_errors_pass_through(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    http = r.http()
    r.fake.hang_next("GET", "/identity")
    with pytest.raises(TemporaryError) as e:
        await http.json("GET", "/identity/v1/Users/me", timeout_s=0.05)
    assert e.value.request_sent is True

    r.clock.t += HOUR_MS
    r.fake.fail_next("POST", "/oauth/token", 503)
    with pytest.raises(TemporaryError) as e:
        await http.json("GET", "/identity/v1/Users/me")
    assert e.value.request_sent is None


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def test_typod_new_env_seed_keeps_working_login(setup: RigFactory) -> None:
    store = MemoryStore()
    box = SecretBox(os.urandom(32))
    a = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-1"})
    working = await a.auth.get_access_token()

    logger = logging.getLogger("aiogotoconnect.sms.test_oauth.typo")
    handler = ListHandler()
    logger.addHandler(handler)
    try:
        env = {**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-typo"}
        b = await setup(store=store, box=box, env=env, logger=logger)
        assert await b.auth.get_access_token() == working
        assert await store.get(T, "login_expired") is None
        assert handler.messages == [
            "GOTO_REFRESH_TOKEN changed but GoTo refused it; keeping the existing login"
        ]
        assert "refresh-typo" not in ",".join(handler.messages)

        # Same bad seed: not retried, even by a new instance (the tried hash is on the tokens).
        calls = b.token_calls()
        assert await b.auth.get_access_token() == working
        c = await setup(store=store, box=box, env=env, logger=logger)
        assert await c.auth.get_access_token() == working
        assert b.token_calls() == calls
        assert len(handler.messages) == 1
        assert await c.auth.status() == AuthStatus(
            connected=True, login_expired=False, refresh_source="seeded-from-env"
        )
    finally:
        logger.removeHandler(handler)


async def test_refused_first_seed_then_new_seed_recovers(setup: RigFactory) -> None:
    store = MemoryStore()
    box = SecretBox(os.urandom(32))
    a = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "bogus"})
    with pytest.raises(LoginExpiredError):
        await a.auth.get_access_token()
    assert await store.get(T, "login_expired") is None, "no persistent flag"
    calls = a.token_calls()
    with pytest.raises(LoginExpiredError):
        await a.auth.get_access_token()
    assert a.token_calls() == calls, "same seed fails fast in this process"

    b = await setup(store=store, box=box, env={**BASE_ENV, "GOTO_REFRESH_TOKEN": "refresh-1"})
    assert isinstance(await b.auth.get_access_token(), str)
    assert (await b.auth.status()).refresh_source == "seeded-from-env"


async def test_token_endpoint_non_2xx_reports_status_and_oauth_code_only(
    setup: RigFactory,
) -> None:
    r = await setup()
    await r.connect()
    r.clock.t += HOUR_MS
    r.fake.fail_next(
        "POST",
        "/oauth/token",
        404,
        {"error": "invalid_request", "echoed": "grant_type=refresh_token&refresh_token=refresh-1"},
    )
    with pytest.raises(GoToApiError) as e:
        await r.auth.get_access_token()
    assert e.value.body == "GoTo token endpoint returned 404 (error: invalid_request)"
    assert e.value.token_endpoint is True
    assert "refresh-1" not in f"{e.value} {e.value.body}"

    r.fake.fail_next("POST", "/oauth/token", 404, "refresh_token=refresh-1")
    with pytest.raises(GoToApiError) as e:
        await r.auth.get_access_token()
    assert e.value.body == "GoTo token endpoint returned 404"


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        [],
        {"access_token": ""},
        {"access_token": "a", "expires_in": "3600"},
        {"access_token": "a", "expires_in": True},
        {"access_token": "a", "refresh_token": 5},
        {"access_token": "a", "expires_in": None},
        {"access_token": "a", "refresh_token": None},
        '{"access_token":"a","expires_in":NaN}',
    ],
)
async def test_malformed_token_response(setup: RigFactory, body: Any) -> None:
    r = await setup()
    await r.connect()
    r.clock.t += HOUR_MS
    r.fake.fail_next("POST", "/oauth/token", 200, body)
    with pytest.raises(GoToApiError) as e:
        await r.auth.get_access_token()
    assert e.value.body == "Malformed token response"
    assert e.value.token_endpoint is True


async def test_login_without_refresh_token_is_an_api_error(setup: RigFactory) -> None:
    r = await setup()
    r.fake.fail_next("POST", "/oauth/token", 200, {"access_token": "a", "expires_in": 3600})
    with pytest.raises(GoToApiError) as e:
        await r.connect()
    assert e.value.body == "GoTo returned no refresh token for the login"
    assert await r.store.get(T, "tokens") is None


async def test_http_malformed_2xx_body_not_echoed(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    r.fake.route("GET", "/garbled", lambda _req: FakeResponse(200, "not json +18015550123 hi Bob"))
    with pytest.raises(GoToApiError) as e:
        await r.http().json("GET", "/garbled")
    assert e.value.body == "Malformed JSON response"
    assert "8015550123" not in f"{e.value} {e.value.body}"


async def test_verify_refreshes_even_when_fresh_and_seeds_when_pending(setup: RigFactory) -> None:
    r = await setup()
    await r.connect()
    calls = r.token_calls()
    await r.auth.verify()
    assert r.token_calls() == calls + 1, "a fresh token is still refreshed"

    none = await setup()
    with pytest.raises(NotConfiguredError):
        await none.auth.verify()


async def test_cas_loss_login_during_refresh_wins(setup: RigFactory) -> None:
    gate = Gate()
    r = await setup(gate=gate)
    await r.connect()
    r.clock.t += HOUR_MS
    gate.armed = True
    pending = asyncio.create_task(r.auth.get_access_token())
    await gate.reached.wait()  # the refresh is parked at the token endpoint
    await r.connect()  # a fresh login replaces tokens mid-refresh
    login = cast(dict[str, str], await r.store.get(T, "tokens"))
    gate.released.set()
    assert await pending == r.box.open(login["access"])
    assert await r.store.get(T, "tokens") == login, "the refresh didn't overwrite the login"


async def test_cas_loss_disconnect_during_refresh_wins(setup: RigFactory) -> None:
    gate = Gate()
    r = await setup(gate=gate)
    await r.connect()
    r.clock.t += HOUR_MS
    gate.armed = True
    pending = asyncio.create_task(r.auth.get_access_token())
    await gate.reached.wait()
    await r.auth.disconnect()
    gate.released.set()
    with pytest.raises(NotConfiguredError):
        await pending
    assert await r.store.get(T, "tokens") is None
