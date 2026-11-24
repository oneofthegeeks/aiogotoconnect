class GoToError(Exception):
    """Base class for all aiogotoconnect errors."""


class GoToAuthError(GoToError):
    """Credentials were rejected: OAuth token refresh failed, SIP digest credentials refused, or SMS got a 401."""


class GoToApiError(GoToError):
    """A GoTo REST API call returned a non-success HTTP status.

    ``error_code``, ``detail`` and ``reference`` come from GoTo's JSON error body
    (``errorCode``, ``message``, ``reference``) when it has one, else None. ``detail``
    is GoTo's own sentence (e.g. "Forbidden access to phone number +1801..."), which
    can name the sender number; ``reference`` is what GoTo support asks for.
    """

    def __init__(
        self,
        message: str,
        status: int,
        error_code: str | None = None,
        detail: str | None = None,
        reference: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.detail = detail
        self.reference = reference


class GoToSipError(GoToError):
    """SIP registration or signalling failed."""


class GoToCallError(GoToSipError):
    """An outbound call could not be completed."""
