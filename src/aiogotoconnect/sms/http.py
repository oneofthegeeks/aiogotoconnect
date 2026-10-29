"""Bearer-authenticated JSON calls to the GoTo API (port of http.ts)."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import aiohttp

from aiogotoconnect.sms.errors import GoToApiError, LoginExpiredError, TemporaryError

DEFAULT_TIMEOUT_S = 10.0


def error_name(err: BaseException) -> str:
    """A short, secret-free description of a request failure ("TimeoutError", "ClientOSError")."""
    return type(err).__name__


class GoToHttp:
    """On a 401: force_refresh once, retry once. Token errors propagate unchanged."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base_url: str,
        get_token: Callable[[], Awaitable[str]],
        force_refresh: Callable[[], Awaitable[str]],
        timeout_s: float,
        logger: logging.Logger,
    ) -> None:
        self._session = session
        self._api_base_url = api_base_url
        self._get_token = get_token
        self._force_refresh = force_refresh
        self._timeout_s = timeout_s
        self._logger = logger

    async def _call(
        self, method: str, url: str, path: str, token: str, body: Any, timeout_s: float
    ) -> tuple[int, str]:
        # Everything in here happens after the request left, so failures are "request_sent":
        # the caller can't know whether GoTo acted on it.
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data: str | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":"))
        try:
            # The total timeout also covers reading the body.
            async with self._session.request(
                method,
                url,
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as res:
                return res.status, await res.text(errors="replace")
        except Exception as err:  # noqa: BLE001 - any failure after sending is ambiguous
            raise TemporaryError(
                f"GoTo {method} {path} failed: {error_name(err)}", request_sent=True
            ) from None

    async def json(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout_s: float | None = None,
        query: dict[str, str] | None = None,
    ) -> Any:
        """Raises GoToApiError on a non-2xx answer; a 204 or empty body gives `{}`."""
        url = f"{self._api_base_url}{path}"
        if query:
            url += f"?{urlencode(query)}"
        timeout = self._timeout_s if timeout_s is None else timeout_s

        status, text = await self._call(method, url, path, await self._get_token(), body, timeout)
        if status == 401:
            self._logger.debug("GoTo %s %s answered 401; refreshing the token once", method, path)
            status, text = await self._call(
                method, url, path, await self._force_refresh(), body, timeout
            )
            # A freshly refreshed token was refused too: the login no longer has access.
            if status == 401:
                raise LoginExpiredError()
        if not 200 <= status < 300:
            raise GoToApiError(status, text)
        if status == 204 or text == "":
            return {}
        try:
            return json.loads(text)
        except ValueError:
            # Fixed text: the body may hold PII (numbers, message text) and errors get logged (C22).
            raise GoToApiError(status, "Malformed JSON response") from None


def create_goto_http(
    session: aiohttp.ClientSession,
    api_base_url: str,
    get_token: Callable[[], Awaitable[str]],
    force_refresh: Callable[[], Awaitable[str]],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    logger: logging.Logger | None = None,
) -> GoToHttp:
    return GoToHttp(
        session,
        api_base_url,
        get_token,
        force_refresh,
        timeout_s,
        logger or logging.getLogger("aiogotoconnect.sms"),
    )


__all__ = ["DEFAULT_TIMEOUT_S", "GoToHttp", "create_goto_http", "error_name"]
