"""Client for the ArionPay merchant API. Use it on your server only: it holds your secret key."""

import json
import platform
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .errors import ArionPayError
from .webhook import Body, sign_request, verify_webhook

VERSION = "1.0.0"
DEFAULT_BASE_URL = "https://merchant.arionpay.com"

_PK = re.compile(r"^pk_[A-Za-z0-9_-]{8,64}$")
_ID = re.compile(r"^[0-9a-fA-F]{24}$")

# (method, url, headers, body or None, timeout seconds) -> (status, headers, body bytes); raises OSError when unreachable
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Mapping[str, str], bytes]]

SCENARIOS = ("detected", "confirm", "exact", "underpaid_1pct", "underpaid_5pct", "overpaid", "partial_half", "late")
EVENT_TYPES = ("invoice.pending", "invoice.partial", "invoice.paid", "invoice.expired", "invoice.needs_review")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[int, Mapping[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b""


def _s(v: Any, default: str = "") -> str:
    return default if v is None else str(v)


def normalize_invoice_read(r: Mapping[str, Any]) -> Dict[str, Any]:
    """Turn the signed read (GET /api/v1/invoices/<id>) into the webhook invoice field names."""
    out = {
        "id": _s(r.get("invoiceId", r.get("id"))),
        "status": _s(r.get("status")).lower(),
        "orderId": r.get("orderId"),
        "amountFiat": r.get("amountFiat"),
        "currency": _s(r.get("currency")),
        "asset": _s(r.get("settledCurrency")),
        "network": _s(r.get("network")),
        "amountCrypto": _s(r.get("amountExpected")),
        "paidCrypto": _s(r.get("amountReceived"), "0"),
        "confirmedCrypto": _s(r.get("amountConfirmed"), "0"),
        "overpaid": r.get("overpaid") is True,
        "underpaidWithinTolerance": r.get("underpaidWithinTolerance") is True,
        "late": r.get("late") is True,
        "needsReview": r.get("needsReview") is True,
        "manual": r.get("manual") is True,
        "test": r.get("test") is True,
        "transactions": r.get("transactions") if isinstance(r.get("transactions"), list) else [],
        "metadata": r.get("metadata") if isinstance(r.get("metadata"), dict) else {},
        "paidAt": r.get("settledAt"),
        "txHash": r.get("txHash"),
        "confirmations": r.get("confirmations", 0),
        "requiredConfirmations": r.get("requiredConfirmations", 0),
        "raw": dict(r),
    }  # type: Dict[str, Any]
    if "paidVia" in r:
        out["paidVia"] = r.get("paidVia")
    return out


class ArionPay:
    """ArionPay merchant API client.

        client = ArionPay(api_key=os.environ["ARIONPAY_API_KEY"], api_secret=os.environ["ARIONPAY_API_SECRET"])
        invoice = client.create_invoice(amount="49.90", currency="USD", order_id="1001")
        # send the customer to invoice["invoiceUrl"]

    Returned invoices are dicts with the same field names as the webhook's data.invoice (camelCase), so one function
    can handle a webhook and a read.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        webhook_secret: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        max_attempts: int = 3,
        transport: Optional[Transport] = None,
        app_info: str = "",
    ) -> None:
        if not isinstance(api_key, str) or not _PK.match(api_key):
            raise ValueError("api_key must be your store API key (it starts with pk_).")
        if not api_secret:
            raise ValueError("api_secret is required (your store API secret, it starts with sk_).")
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an http(s) address.")
        self._api_key = api_key
        self._api_secret = api_secret
        self._webhook_secret = webhook_secret
        self._origin = "%s://%s" % (parts.scheme, parts.netloc)
        self._base_path = parts.path.rstrip("/")
        self._timeout = float(timeout)
        self._max_attempts = max(1, min(5, int(max_attempts)))
        self._transport = transport or _urllib_transport
        self._user_agent = "arionpay-python/%s python/%s%s" % (VERSION, platform.python_version(), (" " + app_info) if app_info else "")

    # -- invoices -------------------------------------------------------------------------------------------------

    def create_invoice(
        self,
        amount: Any,
        currency: str = "USD",
        order_id: Optional[str] = None,
        description: Optional[str] = None,
        customer_email: Optional[str] = None,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        callback_url: Optional[str] = None,
        asset: Optional[str] = None,
        allowed_assets: Optional[List[str]] = None,
        expires_min: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        test: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Create an invoice. Returns {"id", "invoiceUrl", "status", "amountFiat", "currency", "asset", "address",
        "amountCrypto", "expiresAt", "orderId", "raw"}. Send the customer to invoiceUrl.

        Safe to retry: each call carries an idempotency key (a random one when you pass none), so a repeat after a
        network error returns the same invoice. Pass your own key (e.g. "order-1001-1") to be safe across restarts.
        The customer coming back to success_url does NOT prove payment: wait for the invoice.paid webhook.
        """
        if amount is None or isinstance(amount, bool):
            raise ValueError("amount is required.")
        fields = {
            "amount": str(amount),
            "currency": currency,
            "orderId": order_id,
            "description": description,
            "customerEmail": customer_email,
            "successUrl": success_url,
            "cancelUrl": cancel_url,
            "callbackUrl": callback_url,
            "asset": asset,
            "allowedAssets": allowed_assets,
            "expiresMin": expires_min,
            "metadata": metadata,
            "idempotencyKey": idempotency_key or "sdk_" + str(uuid.uuid4()),
            "test": test,
        }
        body = {k: v for k, v in fields.items() if v is not None}
        r = self._request("POST", "/api/v1/invoices", body, retry_safe=True)
        d = r.get("data") if isinstance(r.get("data"), dict) else r
        return {
            "id": _s(d.get("id", r.get("id"))),
            "invoiceUrl": _s(d.get("invoice_url", r.get("invoice_url"))),
            "status": _s(d.get("status"), "new"),
            "amountFiat": d.get("amount_fiat"),
            "currency": _s(d.get("currency"), currency),
            "asset": d.get("asset"),
            "address": d.get("address"),
            "amountCrypto": d.get("amount_crypto"),
            "expiresAt": d.get("expires_at"),
            "orderId": d.get("order_id", order_id),
            "raw": r,
        }

    def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Read an invoice, signed, straight from ArionPay (webhook field names). Use it to double-check, or to poll."""
        self._check_id(invoice_id)
        return normalize_invoice_read(self._request("GET", "/api/v1/invoices/" + invoice_id, None, retry_safe=True))

    def simulate_payment(self, invoice_id: str, scenario: str = "exact") -> Dict[str, Any]:
        """Sandbox: move a TEST invoice through a payment scenario; webhooks then fire as for a real payment.
        Create the test invoice with asset=... (e.g. "USDT_TRC20") so a coin is chosen."""
        self._check_id(invoice_id)
        if scenario not in SCENARIOS:
            raise ValueError("scenario must be one of: " + ", ".join(SCENARIOS))
        return self._request("POST", "/api/v1/invoices/%s/simulate" % invoice_id, {"scenario": scenario}, retry_safe=False)

    def send_test_webhook(self, event_type: str = "invoice.paid") -> Dict[str, Any]:
        """Ask ArionPay to send a signed sample event to your store's webhook address. It never describes a real order."""
        return self._request("POST", "/api/v1/webhooks/test", {"eventType": event_type}, retry_safe=False)

    def currencies(self) -> List[Dict[str, Any]]:
        """Coins you can pass as asset / allowed_assets (e.g. "USDT_TRC20", "BTC", "LTC"), with USD rates."""
        r = self._request("GET", "/api/v1/currencies", None, retry_safe=True, signed=False)
        return r.get("currencies") if isinstance(r.get("currencies"), list) else []

    def rates(self, currency: str = "USD") -> Dict[str, Any]:
        """Current coin prices in one fiat currency."""
        return self._request("GET", "/api/v1/rates?currency=" + urllib.parse.quote(currency), None, retry_safe=True, signed=False)

    def verify_webhook(self, raw_body: Body, signature_header: Optional[str], tolerance_sec: int = 300) -> Dict[str, Any]:
        """Check a webhook with the webhook_secret given to the constructor. See arionpay.verify_webhook."""
        return verify_webhook(raw_body, signature_header, self._webhook_secret, tolerance_sec)

    # -- transport ------------------------------------------------------------------------------------------------

    @staticmethod
    def _check_id(invoice_id: str) -> None:
        if not isinstance(invoice_id, str) or not _ID.match(invoice_id):
            raise ValueError("Invoice id must be the 24-character id ArionPay returned.")

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]], retry_safe: bool, signed: bool = True) -> Dict[str, Any]:
        raw = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        replay_retry_used = False
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._send(method, path, raw, signed)
            except ArionPayError as e:
                # Two identical signed requests in the same second have the same signature; only the first is accepted.
                if e.status == 401 and "already used" in e.message.lower() and not replay_retry_used:
                    replay_retry_used = True
                    time.sleep(1.1)
                    continue
                retry_after = 0
                if isinstance(e.body, dict):
                    try:
                        retry_after = int(e.body.get("retryAfterSec") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                temporary = e.status in (0, 502, 503, 504) or (e.status == 429 and retry_after <= 5)
                if not retry_safe or not temporary or attempt >= self._max_attempts:
                    raise
                time.sleep(max(1, retry_after) if e.status == 429 else 0.5 * (3 ** (attempt - 1)))

    def _send(self, method: str, path: str, raw: Optional[bytes], signed: bool) -> Dict[str, Any]:
        path_only, _, query = path.partition("?")
        full_path = self._base_path + path_only + ("?" + query if query else "")
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        if signed:
            ts = int(time.time())
            headers["x-api-key"] = self._api_key
            headers["x-timestamp"] = str(ts)
            headers["x-signature"] = sign_request(self._api_secret, ts, method, full_path, raw or b"")
        if raw is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, resp_headers, data = self._transport(method, self._origin + full_path, headers, raw, self._timeout)
        except (OSError, socket.timeout) as e:
            raise ArionPayError("Could not reach ArionPay: %s" % (e,), 0, "network_error")
        try:
            decoded = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            decoded = {}
        obj = decoded if isinstance(decoded, dict) else {}
        if status < 200 or status >= 300:
            message = obj.get("error") if isinstance(obj.get("error"), str) else obj.get("message") if isinstance(obj.get("message"), str) else "HTTP %d" % status
            lower = {k.lower(): v for k, v in dict(resp_headers or {}).items()}
            try:
                retry_after = int(lower.get("retry-after", 0) or 0)
            except (TypeError, ValueError):
                retry_after = 0
            err_body = dict(obj)
            if retry_after:
                err_body["retryAfterSec"] = retry_after
            raise ArionPayError("ArionPay API error (%d): %s" % (status, message), status, obj.get("code") if isinstance(obj.get("code"), str) else "", err_body)
        return obj
