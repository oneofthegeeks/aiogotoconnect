"""Error classes (port of Node errors.ts). Codes are identical to Node's."""

from aiogotoconnect.sms.types import SendResult


class GoToSmsError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class LoginExpiredError(GoToSmsError):
    def __init__(self, message: str = "GoTo login expired; re-run setup") -> None:
        super().__init__(message, "login_expired")


class TemporaryError(GoToSmsError):
    def __init__(
        self, message: str = "Temporary failure", *, request_sent: bool | None = None
    ) -> None:
        super().__init__(message, "temporary")
        # True when the API request had already gone out (network error or timeout after
        # sending), so it may have taken effect. None when it never left (e.g. no token).
        self.request_sent = request_sent


class NotConfiguredError(GoToSmsError):
    def __init__(self, message: str = "GoTo SMS is not configured") -> None:
        super().__init__(message, "not_configured")


class GoToApiError(GoToSmsError):
    def __init__(self, status: int, body: str, *, token_endpoint: bool = False) -> None:
        super().__init__(f"GoTo API error {status}", "api")
        self.status = status
        self.body = body[:500]
        # True when the OAuth token endpoint raised this, so no API request (e.g. a send) went
        # out. Senders rely on it: a 2xx GoToApiError otherwise means the API accepted the call.
        self.token_endpoint = token_endpoint


class SendFailedError(GoToSmsError):
    def __init__(self, result: SendResult) -> None:
        reason = f" ({result.reason})" if result.reason else ""
        super().__init__(f"Send failed: {result.status}{reason}", "send_failed")
        self.result = result


class DecryptError(GoToSmsError):
    """A sealed secret could not be opened: wrong key, tampering, or a malformed string."""

    def __init__(
        self, message: str = "Could not decrypt stored secret (wrong key or corrupted data)"
    ) -> None:
        super().__init__(message, "decrypt_failed")
