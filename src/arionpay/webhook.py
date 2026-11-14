"""Request and webhook signatures, matching the ArionPay backend.

Signed API requests:
    POST: x-signature = hex HMAC-SHA256(secret key, "<timestamp>.<raw body>")
    GET:  x-signature = hex HMAC-SHA256(secret key, "<timestamp>.GET <path and query>")

Webhooks:
    ArionPay-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(webhook secret, "<t>.<raw body>")>
"""

import hashlib
import hmac
import json
import re
import time
from typing import Any, Dict, Optional, Tuple, Union

from .errors import SignatureError

DEFAULT_TOLERANCE_SEC = 300
"""Webhooks signed more than this many seconds ago (or ahead) are refused. ArionPay signs again on every retry."""

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,12}$")

Body = Union[bytes, bytearray, str]


def _as_bytes(raw: Body) -> bytes:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        return raw.encode("utf-8")
    raise SignatureError("raw_body must be the raw request body (bytes or str), not a parsed object.")


def sign_request(secret_key: str, timestamp: int, method: str, path_and_query: str, raw_body: Body = b"") -> str:
    """Signature for one request to the merchant API (lower-case hex)."""
    if method.upper() == "GET":
        data = ("%d.GET %s" % (timestamp, path_and_query)).encode("utf-8")
    else:
        data = ("%d." % timestamp).encode("utf-8") + _as_bytes(raw_body)
    return hmac.new(secret_key.encode("utf-8"), data, hashlib.sha256).hexdigest()


def parse_signature_header(header: Optional[str]) -> Optional[Tuple[int, str]]:
    """Read an ArionPay-Signature header. Returns (t, v1) or None when it is malformed."""
    values = {}  # type: Dict[str, str]
    for part in str(header or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            values[k.strip()] = v.strip()
    t, v1 = values.get("t", ""), values.get("v1", "")
    if not _DIGITS.match(t) or not _HEX64.match(v1):
        return None
    return int(t), v1.lower()


def is_valid_signature(
    raw_body: Body, header: Optional[str], webhook_secret: str, tolerance_sec: int = DEFAULT_TOLERANCE_SEC, now: Optional[int] = None
) -> bool:
    """True when the header is a valid, fresh signature of exactly this body. Never raises for bad input."""
    if not webhook_secret:
        return False
    parsed = parse_signature_header(header)
    if parsed is None:
        return False
    t, v1 = parsed
    now = int(time.time()) if now is None else int(now)
    if abs(now - t) > tolerance_sec:
        return False
    try:
        body = _as_bytes(raw_body)
    except SignatureError:
        return False
    expected = hmac.new(webhook_secret.encode("utf-8"), ("%d." % t).encode("utf-8") + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def verify_webhook(
    raw_body: Body, header: Optional[str], webhook_secret: str, tolerance_sec: int = DEFAULT_TOLERANCE_SEC, now: Optional[int] = None
) -> Dict[str, Any]:
    """Check a webhook and return the event as a dict. Raises SignatureError when it must not be trusted.

    raw_body must be the exact bytes ArionPay sent (Flask: request.get_data(), Django: request.body,
    FastAPI: await request.body()), not a parsed and re-serialised object.
    """
    if not webhook_secret:
        raise SignatureError("The webhook secret is empty. Copy it from the Webhooks page of your ArionPay dashboard (it starts with whsec_).")
    body = _as_bytes(raw_body)
    if parse_signature_header(header) is None:
        raise SignatureError("Missing or malformed ArionPay-Signature header.")
    if not is_valid_signature(body, header, webhook_secret, tolerance_sec, now):
        raise SignatureError("Invalid webhook signature or stale timestamp. Check the webhook secret and your server clock.")
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SignatureError("The webhook body is not JSON.")
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("id"), str)
        or not event["id"]
        or not isinstance(event.get("type"), str)
        or not isinstance(event.get("data"), dict)
        or not isinstance(event["data"].get("invoice"), dict)
    ):
        raise SignatureError("The webhook body is not an ArionPay event.")
    return event


def is_test_event(event: Dict[str, Any]) -> bool:
    """True for events made by "Send test webhook": a made-up invoice that must never change an order."""
    invoice = (event.get("data") or {}).get("invoice") or {}
    return bool(event.get("test")) or str(event.get("id", "")).startswith("evt_test_") or str(invoice.get("id", "")).startswith("inv_test_")
