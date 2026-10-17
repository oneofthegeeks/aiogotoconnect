import aiohttp

from gotoconnect.auth import TokenProvider
from gotoconnect.exceptions import GoToApiError, GoToAuthError


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
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status == 401:
                raise GoToAuthError("SMS send rejected: access token not accepted")
            # Error text is deliberately not included: it could echo phone numbers or tokens.
            if response.status >= 400:
                raise GoToApiError(f"SMS send failed with HTTP {response.status}", response.status)
            return await response.json()
