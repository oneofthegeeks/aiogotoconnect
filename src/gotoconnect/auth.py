import asyncio
import base64
import time
from collections.abc import Awaitable, Callable

import aiohttp

from gotoconnect.exceptions import GoToAuthError

# Any async callable returning a bearer token. Home Assistant passes its own.
TokenProvider = Callable[[], Awaitable[str]]

DEFAULT_TOKEN_URL = "https://authentication.logmeininc.com/oauth/token"

# Refresh this many seconds before expiry so a token never expires mid-request.
REFRESH_MARGIN_SECONDS = 60


class GoToOAuth:
    """Standalone OAuth provider: refresh-token grant with cached access token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_url: str = DEFAULT_TOKEN_URL,
        session: aiohttp.ClientSession | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self.refresh_token = refresh_token  # GoTo may rotate this on every refresh
        self._token_url = token_url
        self._session = session
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._refresh_lock = asyncio.Lock()

    def _needs_refresh(self) -> bool:
        expiring = time.monotonic() >= self._expires_at - REFRESH_MARGIN_SECONDS
        return self._access_token is None or expiring

    async def async_get_access_token(self) -> str:
        if not self._needs_refresh():
            return self._access_token
        # Lock + re-check: concurrent callers wait, then reuse the token the first one fetched.
        # Needed because a rotating refresh_token is single-use.
        async with self._refresh_lock:
            if self._needs_refresh():
                await self._async_refresh()
        return self._access_token

    async def _async_refresh(self) -> None:
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {credentials}"}
        form = {"grant_type": "refresh_token", "refresh_token": self.refresh_token}

        if self._session is None:
            async with aiohttp.ClientSession() as session:
                payload = await self._async_post_token(session, headers, form)
        else:
            payload = await self._async_post_token(self._session, headers, form)

        # Error text is deliberately not included: it could echo a secret.
        try:
            access_token = payload["access_token"]
            expires_in = float(payload["expires_in"])
        except (KeyError, TypeError, ValueError) as error:
            raise GoToAuthError("Token response missing access_token or expires_in") from error
        self._access_token = access_token
        self.refresh_token = payload.get("refresh_token", self.refresh_token)
        self._expires_at = time.monotonic() + expires_in

    async def _async_post_token(self, session, headers, form) -> dict:
        try:
            async with session.post(self._token_url, headers=headers, data=form) as response:
                if response.status != 200:
                    raise GoToAuthError(f"Token refresh failed with HTTP {response.status}")
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise GoToAuthError("Token refresh request failed") from error
        except ValueError as error:  # JSONDecodeError (non-JSON body)
            raise GoToAuthError("Token response was not valid JSON") from error
