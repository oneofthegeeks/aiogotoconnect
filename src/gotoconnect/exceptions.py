class GoToError(Exception):
    """Base class for all gotoconnect errors."""


class GoToAuthError(GoToError):
    """OAuth token refresh failed."""


class GoToApiError(GoToError):
    """A GoTo REST API call returned a non-success HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class GoToSipError(GoToError):
    """SIP registration or signalling failed."""


class GoToCallError(GoToSipError):
    """An outbound call could not be completed."""
