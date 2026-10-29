"""GoTo OAuth: authorization-code login plus refresh (port of Node oauth.ts).

All state lives in the store, so a restart between Connect and the callback still works.
Refreshes are single-flight per instance because GoTo may rotate the refresh token: a second
concurrent refresh with the old one would be refused and look like a dead login.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast
from urllib.parse import urlencode

import aiohttp

from aiogotoconnect.sms.config import ConfigResolver
from aiogotoconnect.sms.errors import (
    DecryptError,
    GoToApiError,
    GoToSmsError,
    LoginExpiredError,
    NotConfiguredError,
    TemporaryError,
)
from aiogotoconnect.sms.http import error_name
from aiogotoconnect.sms.store.types import GoToSmsStore, Json

if TYPE_CHECKING:  # keeps `cryptography` (the optional [sms] extra) out of this module's imports
    from aiogotoconnect.sms.secrets import SecretBox

STATE_TTL_MS = 10 * 60_000
REUSE_MARGIN_MS = 60_000  # refresh once the access token is within 60 s of expiry
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_EXPIRES_IN_S = 3600
STATE_PREFIX = "state:"
# 32 random bytes as base64url, unpadded. fullmatch, so a trailing newline is rejected.
STATE_FORMAT = re.compile(r"[A-Za-z0-9_-]{43}")
OAUTH_ERROR_FORMAT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
SEED_REFUSED = "GoTo refused GOTO_REFRESH_TOKEN; set a new one or connect from the setup page"
NOT_CONNECTED = "Not connected to GoTo — open the setup page and click Connect"

RefreshSource = Literal["login", "seeded-from-env"]


@dataclass(frozen=True)
class AuthStatus:
    """Node's `status()` return. On the wire: connected, loginExpired, tokensUnreadable (only
    when True), refreshSource (only when tokens are stored)."""

    connected: bool
    login_expired: bool
    # A login is stored but can't be decrypted (the encryption key changed).
    tokens_unreadable: bool = False
    refresh_source: RefreshSource | None = None


class TokenRecord(TypedDict):
    """Store key `tokens` (camelCase, as Node stores it). access/refresh are SecretBox-sealed;
    expiresAt is the real expiry in epoch ms."""

    access: str
    refresh: str
    expiresAt: float
    source: RefreshSource
    # sha256 hex of the GOTO_REFRESH_TOKEN this login came from (or last tried).
    seedHash: NotRequired[str]


@dataclass(frozen=True)
class _TokenResponse:
    access_token: str
    expires_in: float | None
    refresh_token: str | None


@dataclass(frozen=True)
class _Credentials:
    client_id: str
    client_secret: str


def _oauth_error(text: str) -> str:
    """' (error: invalid_request)' from an OAuth error body, or '' when absent or odd-looking."""
    try:
        body = json.loads(text)
    except ValueError:
        return ""
    e = body.get("error") if isinstance(body, dict) else None
    return f" (error: {e})" if isinstance(e, str) and OAUTH_ERROR_FORMAT.fullmatch(e) else ""


def _sha256hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _is_number(v: object) -> bool:
    """JS `typeof v === 'number'`: JSON booleans are not numbers."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def _as_token_record(v: Json) -> TokenRecord | None:
    if not isinstance(v, dict):
        return None
    access, refresh = v.get("access"), v.get("refresh")
    expires_at, source, seed_hash = v.get("expiresAt"), v.get("source"), v.get("seedHash")
    if not isinstance(access, str) or not isinstance(refresh, str):
        return None
    if not _is_number(expires_at):
        return None
    if source not in ("login", "seeded-from-env"):
        return None
    record = TokenRecord(
        access=access,
        refresh=refresh,
        expiresAt=cast(float, expires_at),
        source=cast(RefreshSource, source),
    )
    if isinstance(seed_hash, str):
        record["seedHash"] = seed_hash
    return record


def _reject_constant(_name: str) -> Any:
    # JSON.parse rejects NaN/Infinity; Python's json accepts them unless told otherwise.
    raise ValueError("non-standard JSON constant")


class GoToAuth:
    def __init__(
        self,
        store: GoToSmsStore,
        tenant: str,
        box: "SecretBox",
        config: ConfigResolver,
        session: aiohttp.ClientSession,
        auth_base_url: str,
        redirect_uri: str,
        logger: logging.Logger,
        now: Callable[[], float],
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._tenant = tenant
        self._box = box
        self._config = config
        self._session = session
        self._auth_base_url = auth_base_url
        self._redirect_uri = redirect_uri
        self._logger = logger
        self._now = now
        self._timeout_s = timeout_s
        # The in-flight refresh/seed; every concurrent caller awaits this same task.
        self._in_flight: asyncio.Task[str] | None = None
        # Hashes of env seeds GoTo refused while no tokens were stored. In memory on purpose
        # (Node ruling R12): env only changes on restart, so this fails fast for the rest of the
        # process, while a restart with a different seed is tried normally.
        self._refused_seeds: set[str] = set()

    async def start_connect(self) -> str:
        """Returns the GoTo authorize URL and saves its state for the callback."""
        creds = await self._credentials()
        # Sweep expired states so abandoned logins don't pile up in the store.
        for key, value in await self._store.list(self._tenant, STATE_PREFIX):
            if self._state_expired(value):
                await self._store.delete(self._tenant, key)
        state = secrets.token_urlsafe(32)  # 43 chars, unpadded base64url
        await self._store.set(
            self._tenant, STATE_PREFIX + state, {"expiresAt": self._now() + STATE_TTL_MS}
        )
        # No scope and no prompt: GoTo uses the client's configured scopes, and naming one has
        # broken its login page (C1).
        q = urlencode(
            {
                "client_id": creds.client_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "state": state,
            }
        )
        return f"{self._auth_base_url}/oauth/authorize?{q}"

    async def finish_connect(self, code: str, state: str) -> None:
        """Completes the login from the OAuth callback."""
        await self._consume_state(state)
        creds = await self._credentials()
        # A refused code is LoginExpiredError, but it doesn't touch an existing login.
        t = await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri},
            creds,
        )
        if not t.refresh_token:
            raise GoToApiError(
                200, "GoTo returned no refresh token for the login", token_endpoint=True
            )
        # A plain set: a fresh login replaces whatever was there. An in-flight refresh will lose
        # its compare_and_set and pick these tokens up instead.
        record = self._record(t, self._box.seal(t.refresh_token), "login")
        await self._store.set(self._tenant, "tokens", cast(Json, record))
        await self._store.delete(self._tenant, "login_expired")
        self._logger.debug("goto-sms: connected to GoTo")

    async def get_access_token(self) -> str:
        await self._credentials()
        tokens = await self._read_tokens()
        seed = self._config.seed_refresh_token()
        seed_hash = None if seed is None else _sha256hex(seed)

        # The operator put a different GOTO_REFRESH_TOKEN in env than the one these tokens came
        # from (or last tried): re-seed. This is also how a dead seeded login recovers.
        stored_hash = tokens.get("seedHash") if tokens else None
        if seed and seed_hash and stored_hash and stored_hash != seed_hash:
            return await self._exclusive(lambda: self._seed_flight(seed, seed_hash))
        if tokens is None:
            if not seed or not seed_hash:
                raise NotConfiguredError(NOT_CONNECTED)
            if seed_hash in self._refused_seeds:
                raise LoginExpiredError(SEED_REFUSED)
            return await self._exclusive(lambda: self._seed_flight(seed, seed_hash))
        if await self._login_expired():
            raise LoginExpiredError()
        if self._fresh(tokens):
            return self._open_token(tokens["access"])
        return await self._exclusive(lambda: self._refresh_flight(False))

    async def verify(self) -> None:
        """Get a new access token from GoTo now: exchange a pending GOTO_REFRESH_TOKEN seed, else
        refresh even if the current access token is fresh. For sms.verify_connection()."""
        tokens = await self._read_tokens()
        seed = self._config.seed_refresh_token()
        stored_hash = tokens.get("seedHash") if tokens else None
        seed_pending = (
            seed is not None and stored_hash is not None and stored_hash != _sha256hex(seed)
        )
        if tokens is None or seed_pending:
            await self.get_access_token()
        else:
            await self.force_refresh()

    async def force_refresh(self) -> str:
        """Refresh now even if the access token looks fresh (GoTo answered 401)."""
        await self._credentials()
        if await self._login_expired():
            raise LoginExpiredError()
        return await self._exclusive(lambda: self._refresh_flight(True))

    async def disconnect(self) -> None:
        await self._store.delete(self._tenant, "tokens")
        await self._store.delete(self._tenant, "login_expired")
        self._logger.debug("goto-sms: disconnected from GoTo")

    async def status(self) -> AuthStatus:
        tokens = await self._read_tokens()
        login_expired = await self._login_expired()
        # A login sealed with another key fails every send, so it must not count as connected.
        tokens_unreadable = False
        if tokens is not None:
            try:
                self._open_token(tokens["refresh"])
            except NotConfiguredError:
                tokens_unreadable = True
        return AuthStatus(
            connected=tokens is not None and not login_expired and not tokens_unreadable,
            login_expired=login_expired,
            tokens_unreadable=tokens_unreadable,
            refresh_source=tokens["source"] if tokens is not None else None,
        )

    # --- internals ---

    async def _exclusive(self, flight: Callable[[], Awaitable[str]]) -> str:
        """Join the in-flight refresh, or start one. The flight is a task, so a caller that is
        cancelled stops waiting without cancelling the refresh other callers share (shield)."""
        task = self._in_flight
        if task is None:

            async def run() -> str:
                try:
                    return await flight()
                finally:
                    # Cleared inside the task, before its waiters resume, so a caller arriving
                    # after completion starts a new flight instead of reusing a stale result.
                    self._in_flight = None

            task = asyncio.ensure_future(run())
            # Mark the outcome retrieved even if every waiter was cancelled, so an orphaned
            # failure doesn't log "Task exception was never retrieved".
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._in_flight = task
        return await asyncio.shield(task)

    async def _refresh_flight(self, force: bool) -> str:
        # Re-read inside the flight: a caller may have seen tokens that a just-finished refresh
        # already replaced, and using a rotated-away refresh token would be refused.
        prev = await self._read_tokens()
        if prev is None:
            raise NotConfiguredError(NOT_CONNECTED)
        if not force and self._fresh(prev):
            return self._open_token(prev["access"])
        creds = await self._credentials()
        try:
            t = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": self._open_token(prev["refresh"])},
                creds,
            )
        except LoginExpiredError:
            await self._mark_login_expired()
            raise
        # GoTo may or may not rotate the refresh token; keep the old one when it doesn't.
        nxt = self._record(
            t,
            self._box.seal(t.refresh_token) if t.refresh_token else prev["refresh"],
            prev["source"],
            prev.get("seedHash"),
        )
        if await self._commit(prev, nxt):
            self._logger.debug("goto-sms: refreshed GoTo access token")
            return t.access_token
        return await self._current_access()

    async def _seed_flight(self, seed: str, seed_hash: str) -> str:
        prev = await self._read_tokens()
        # A concurrent caller already seeded from this exact token.
        if prev is not None and prev.get("seedHash") == seed_hash:
            return await self._refresh_flight(False)
        creds = await self._credentials()
        try:
            t = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": seed}, creds
            )
        except LoginExpiredError:
            # A refused seed never sets login_expired (Node ruling R12): it says nothing about
            # the login we already hold, if any.
            if prev is None:
                self._refused_seeds.add(seed_hash)
                raise LoginExpiredError(SEED_REFUSED) from None
            # Record the seed as tried on the existing tokens, so this same bad seed isn't
            # retried on every call, then keep serving the existing login.
            tried = prev.copy()
            tried["seedHash"] = seed_hash
            await self._commit(prev, tried)
            self._logger.warning(
                "GOTO_REFRESH_TOKEN changed but GoTo refused it; keeping the existing login"
            )
            if await self._login_expired():
                raise LoginExpiredError() from None
            return await self._refresh_flight(False)
        nxt = self._record(t, self._box.seal(t.refresh_token or seed), "seeded-from-env", seed_hash)
        if not await self._commit(prev, nxt):
            return await self._current_access()
        await self._store.delete(self._tenant, "login_expired")
        self._logger.debug("goto-sms: seeded GoTo login from GOTO_REFRESH_TOKEN")
        return t.access_token

    async def _commit(self, prev: TokenRecord | None, nxt: TokenRecord) -> bool:
        """Write `nxt` only if `tokens` is still what this refresh started from. Losing means
        another writer got there first (a finish_connect, a disconnect, or another process on a
        shared store); its tokens are newer than ours, so callers use those instead."""
        return await self._store.compare_and_set(
            self._tenant, "tokens", cast(Json, prev), cast(Json, nxt)
        )

    async def _current_access(self) -> str:
        current = await self._read_tokens()
        if current is None:
            raise NotConfiguredError(NOT_CONNECTED)
        return self._open_token(current["access"])

    def _record(
        self,
        t: _TokenResponse,
        sealed_refresh: str,
        source: RefreshSource,
        seed_hash: str | None = None,
    ) -> TokenRecord:
        expires_in = DEFAULT_EXPIRES_IN_S if t.expires_in is None else t.expires_in
        record = TokenRecord(
            access=self._box.seal(t.access_token),
            refresh=sealed_refresh,
            expiresAt=self._now() + expires_in * 1000,
            source=source,
        )
        if seed_hash:
            record["seedHash"] = seed_hash
        return record

    def _fresh(self, t: TokenRecord) -> bool:
        return self._now() < t["expiresAt"] - REUSE_MARGIN_MS

    async def _mark_login_expired(self) -> None:
        await self._store.set(self._tenant, "login_expired", True)
        self._logger.warning("goto-sms: GoTo refused the refresh token; login marked expired")

    async def _login_expired(self) -> bool:
        return (await self._store.get(self._tenant, "login_expired")) is True

    async def _read_tokens(self) -> TokenRecord | None:
        return _as_token_record(await self._store.get(self._tenant, "tokens"))

    def _open_token(self, sealed: str) -> str:
        try:
            return self._box.open(sealed)
        except DecryptError:
            raise NotConfiguredError(
                "Saved GoTo login can't be decrypted — reconnect to GoTo"
            ) from None

    async def _credentials(self) -> _Credentials:
        c = await self._config.resolve()
        if c.credentials_unreadable:
            raise NotConfiguredError(
                "Saved GoTo credentials can't be decrypted — re-enter the client secret and "
                "reconnect"
            )
        if not c.client_id or not c.client_secret:
            raise NotConfiguredError("GoTo client ID and client secret are not set")
        return _Credentials(c.client_id, c.client_secret)

    def _state_expired(self, value: Json) -> bool:
        expires_at = value.get("expiresAt") if isinstance(value, dict) else None
        return not _is_number(expires_at) or self._now() >= cast(float, expires_at)

    async def _consume_state(self, state: str) -> None:
        """Single use: the state is removed whether it matches, is expired, or not."""

        def bad() -> GoToSmsError:
            return GoToSmsError("That GoTo login was not started here or expired", "bad_state")

        # Shape check first, so junk of any length never reaches compare_digest (and the
        # candidate is plain ASCII, which compare_digest requires of str arguments).
        if not STATE_FORMAT.fullmatch(state):
            raise bad()
        candidate = state.encode("ascii")
        match: tuple[str, Json] | None = None
        for key, value in await self._store.list(self._tenant, STATE_PREFIX):
            saved = key[len(STATE_PREFIX) :].encode("utf-8")
            if len(saved) == len(candidate) and hmac.compare_digest(saved, candidate):
                match = (key, value)
                break
        if match is None:
            raise bad()
        key, value = match
        # compare_and_set makes the claim atomic: two callbacks with one state can't both pass.
        # Node writes null here; the store protocol has no top-level null, so a marker is
        # written instead. Either way the key is deleted on the next line.
        claimed = await self._store.compare_and_set(self._tenant, key, value, {"consumed": True})
        await self._store.delete(self._tenant, key)
        if not claimed or self._state_expired(value):
            raise bad()

    async def _token_request(
        self, params: Mapping[str, str], creds: _Credentials
    ) -> _TokenResponse:
        """POST to the token endpoint: Basic client auth, form body, never client_secret (C2)."""
        basic = base64.b64encode(f"{creds.client_id}:{creds.client_secret}".encode()).decode()
        try:
            async with self._session.post(
                f"{self._auth_base_url}/oauth/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data=urlencode(params),
                timeout=aiohttp.ClientTimeout(total=self._timeout_s),
            ) as res:
                status = res.status
                text = await res.text(errors="replace")
        except Exception as err:  # noqa: BLE001 - any transport failure is temporary
            raise TemporaryError(f"GoTo token request failed: {error_name(err)}") from None

        if status in (400, 401, 403):
            raise LoginExpiredError(f"GoTo refused the login (HTTP {status}); reconnect to GoTo")
        if status in (408, 429) or status >= 500:
            raise TemporaryError(f"GoTo token endpoint answered HTTP {status}")
        if not 200 <= status < 300:
            # Only the status and the OAuth `error` code: a proxy or IdP could echo the request
            # (refresh_token, code) back in the body.
            raise GoToApiError(
                status,
                f"GoTo token endpoint returned {status}{_oauth_error(text)}",
                token_endpoint=True,
            )

        # Never put a success body in an error: it holds tokens.
        def malformed() -> GoToApiError:
            return GoToApiError(status, "Malformed token response", token_endpoint=True)

        try:
            body = json.loads(text, parse_constant=_reject_constant)
        except ValueError:
            raise malformed() from None
        if not isinstance(body, dict):
            raise malformed()
        # Key presence, not `is None`: like Node's `!== undefined`, an explicit null is malformed.
        access = body.get("access_token")
        expires_in = body.get("expires_in")
        refresh = body.get("refresh_token")
        if (
            not isinstance(access, str)
            or access == ""
            or ("expires_in" in body and not _is_number(expires_in))
            or ("refresh_token" in body and not isinstance(refresh, str))
        ):
            raise malformed()
        return _TokenResponse(access, expires_in, refresh)


__all__ = ["AuthStatus", "GoToAuth", "RefreshSource", "TokenRecord"]
