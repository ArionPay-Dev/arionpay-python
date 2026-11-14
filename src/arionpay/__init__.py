"""Official Python client for ArionPay: create crypto invoices and verify webhooks."""

from .client import DEFAULT_BASE_URL, EVENT_TYPES, SCENARIOS, VERSION, ArionPay, normalize_invoice_read
from .errors import ArionPayError, SignatureError
from .webhook import DEFAULT_TOLERANCE_SEC, is_test_event, is_valid_signature, parse_signature_header, sign_request, verify_webhook

__version__ = VERSION
__all__ = [
    "ArionPay",
    "ArionPayError",
    "SignatureError",
    "verify_webhook",
    "is_valid_signature",
    "is_test_event",
    "parse_signature_header",
    "sign_request",
    "normalize_invoice_read",
    "DEFAULT_BASE_URL",
    "DEFAULT_TOLERANCE_SEC",
    "EVENT_TYPES",
    "SCENARIOS",
    "VERSION",
]
