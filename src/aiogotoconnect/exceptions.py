class GoToError(Exception):
    """Base class for all aiogotoconnect errors."""


class GoToAuthError(GoToError):
    """Credentials were rejected: OAuth token refresh failed, SIP digest credentials refused, or SMS got a 401."""


class GoToApiError(GoToError):
    """A GoTo REST API call returned a non-success HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class GoToSipError(GoToError):
    """SIP registration or signalling failed."""


class GoToCallError(GoToSipError):
    """An outbound call could not be completed."""
