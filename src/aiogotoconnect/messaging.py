import asyncio

import aiohttp

from aiogotoconnect.auth import TokenProvider
from aiogotoconnect.exceptions import GoToApiError, GoToAuthError, GoToError


class GoToMessaging:
    """GoTo Messaging v2 client: send SMS from an owner number to contact numbers."""

    def __init__(
        self,
        token_provider: TokenProvider,
        session: aiohttp.ClientSession | None = None,
        base_url: str = "https://api.goto.com",
    ):
        self._token_provider = token_provider
        self._session = session
        self._base_url = base_url

    async def async_send_sms(self, owner_phone: str, contacts: list[str], body: str) -> dict:
        # GoTo rejects owner == contact with a confusing error, so fail fast here.
        if owner_phone in contacts:
            raise ValueError("owner_phone must not also be a contact")

        headers = {"Authorization": f"Bearer {await self._token_provider()}"}
        payload = {"ownerPhoneNumber": owner_phone, "contactPhoneNumbers": contacts, "body": body}
        url = f"{self._base_url}/messaging/v1/messages"

        if self._session is None:
            async with aiohttp.ClientSession() as session:
                return await self._async_post(session, url, headers, payload)
        return await self._async_post(self._session, url, headers, payload)

    async def _async_post(self, session, url, headers, payload) -> dict:
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 401:
                    raise GoToAuthError("SMS send rejected: access token not accepted")
                if response.status >= 400:
                    raise await _api_error(response)
                # Success with no usable body is still success: GoTo returns JSON, but don't rely on it.
                try:
                    parsed = await response.json(content_type=None)
                except ValueError:  # JSONDecodeError: body was not JSON
                    return {}
                return {} if parsed is None else parsed  # aiohttp yields None for an empty body
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise GoToError("SMS send request failed") from error


# GoTo's error sentences are short; cap them so an unexpected HTML page can't flood a log.
_MAX_DETAIL = 200


async def _api_error(response: aiohttp.ClientResponse) -> GoToApiError:
    """Build a GoToApiError carrying GoTo's errorCode/message/reference when the body has them.

    Only those three named fields are kept, never the raw body, so nothing else the
    server echoes (request payload, headers) reaches the exception or the logs.
    """
    error_code = detail = reference = None
    try:
        parsed = await response.json(content_type=None)
    except (ValueError, aiohttp.ClientError):  # not JSON, or the body failed to read
        parsed = None
    if isinstance(parsed, dict):
        error_code = _field(parsed, "errorCode")
        detail = _field(parsed, "message")
        reference = _field(parsed, "reference")

    message = f"SMS send failed with HTTP {response.status}"
    if error_code or detail:
        message += ": " + " - ".join(part for part in (error_code, detail) if part)
    return GoToApiError(message, response.status, error_code, detail, reference)


def _field(parsed: dict, key: str) -> str | None:
    value = parsed.get(key)
    return value[:_MAX_DETAIL] if isinstance(value, str) and value else None
