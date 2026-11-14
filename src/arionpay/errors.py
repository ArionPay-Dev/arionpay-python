"""Errors raised by the ArionPay client. Messages never contain your secret key."""

from typing import Any, Optional


class ArionPayError(Exception):
    """Any failure talking to the ArionPay API.

    Attributes:
        status: HTTP status, 0 when ArionPay could not be reached.
        code: machine-readable code when the API sent one, e.g. "idempotency_key_reused", "rate_limited".
        body: decoded error body, if any.
    """

    def __init__(self, message: str, status: int = 0, code: str = "", body: Optional[Any] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body


class SignatureError(Exception):
    """A webhook that must not be trusted: bad or missing signature, stale timestamp, or not an event."""
