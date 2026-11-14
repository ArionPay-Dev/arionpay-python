# ArionPay for Python

Official Python client for [ArionPay](https://arionpay.com). Create crypto invoices, send customers to the payment page, and check webhooks. The money goes straight to your own wallet: ArionPay never holds it.

- No dependencies (standard library only). Python 3.8 or newer.
- Works with Flask, Django, FastAPI or plain scripts.
- Safe retries: every invoice request carries an idempotency key, so a network error can never create two invoices.

```bash
pip install arionpay
```

## Keys

In your ArionPay dashboard, open your store:

| Value | Where | Starts with |
| --- | --- | --- |
| API key | Store page, **API key** | `pk_` |
| API secret | Store page, **API secret** (asks for your authenticator code) | `sk_` |
| Webhook secret | **Webhooks** page, **signing secret** | `whsec_` |

Keep them on your server, for example in environment variables.

## Take a payment

```python
import os
from arionpay import ArionPay

arionpay = ArionPay(
    api_key=os.environ["ARIONPAY_API_KEY"],
    api_secret=os.environ["ARIONPAY_API_SECRET"],
    webhook_secret=os.environ["ARIONPAY_WEBHOOK_SECRET"],
)

invoice = arionpay.create_invoice(
    amount="49.90",
    currency="USD",
    order_id="1001",
    success_url="https://shop.example.com/thanks/1001",
    cancel_url="https://shop.example.com/cart",
    metadata={"user_id": 42},
)
# send the customer to invoice["invoiceUrl"]
```

The customer coming back to `success_url` does **not** prove they paid. Deliver the order when the `invoice.paid` webhook arrives (or when `get_invoice` says `paid`).

Invoices are returned as dicts with the same field names as the webhook's `data.invoice` (`id`, `status`, `orderId`, `amountFiat`, `currency`, `asset`, `paidCrypto`, `transactions`, `test`, `metadata`, ...), so one function can handle a webhook and a read.

## Webhooks

Set your webhook address on the **Webhooks** page of the dashboard (a public `https://` address), or per invoice with `callback_url`. Check the signature against the **raw** body, before you parse it.

### Flask

```python
from flask import Flask, request
from arionpay import SignatureError, is_test_event

app = Flask(__name__)

@app.post("/arionpay/webhook")
def arionpay_webhook():
    try:
        event = arionpay.verify_webhook(request.get_data(), request.headers.get("ArionPay-Signature"))
    except SignatureError:
        return {"error": "invalid_signature"}, 401
    if is_test_event(event):
        return {"received": True}  # "Send test webhook": not a real order
    handle_event(event)
    return {"received": True}
```

### Django

```python
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from arionpay import SignatureError, is_test_event

@csrf_exempt
@require_POST
def arionpay_webhook(request):
    try:
        event = arionpay.verify_webhook(request.body, request.headers.get("ArionPay-Signature"))
    except SignatureError:
        return JsonResponse({"error": "invalid_signature"}, status=401)
    if not is_test_event(event):
        handle_event(event)
    return JsonResponse({"received": True})
```

### FastAPI

```python
from fastapi import FastAPI, Request, Response
from arionpay import SignatureError, is_test_event

app = FastAPI()

@app.post("/arionpay/webhook")
async def arionpay_webhook(request: Request):
    raw = await request.body()
    try:
        event = arionpay.verify_webhook(raw, request.headers.get("arionpay-signature"))
    except SignatureError:
        return Response(status_code=401)
    if not is_test_event(event):
        handle_event(event)
    return {"received": True}
```

### Handling an event

```python
def handle_event(event):
    if already_processed(event["id"]):          # the same event can arrive more than once
        return
    invoice = event["data"]["invoice"]
    order = find_order(invoice["orderId"])
    if event["type"] == "invoice.paid" and order and not order.paid:
        same_total = f"{float(order.total):.2f}" == f"{float(invoice['amountFiat']):.2f}"
        if same_total and order.currency == invoice["currency"] and invoice["test"] == order.is_test:
            mark_order_paid(order, invoice)      # deliver here
    remember_processed(event["id"])
```

| Event | Meaning | What to do |
| --- | --- | --- |
| `invoice.pending` | A payment was seen on the network and waits for confirmations. | Show "payment on its way". Do not deliver yet. |
| `invoice.partial` | Less than the full amount arrived. The invoice stays open for the rest. | Do not deliver. |
| `invoice.paid` | Fully paid and confirmed. Also sent when you accept a payment by hand in the dashboard. | Deliver the order. |
| `invoice.expired` | Time ran out without full payment. A late payment can still make it `paid`. | Keep the order unpaid. |
| `invoice.needs_review` | Something needs a person, for example more money arrived after the invoice was paid. | Tell a human. Do not change the order. |

ArionPay sends again when your server does not answer `2xx` (for about 3 days).

### Rules for a safe integration

1. Check the signature on the raw body. Never trust a webhook you could not verify.
2. Deliver only on `invoice.paid` (or `get_invoice(...)["status"] == "paid"`).
3. Match the invoice to your order: your `order_id` or `metadata`, and the same amount and currency as the order's current total.
4. Ignore test events (`is_test_event(event)`), and in your live shop ignore invoices with `"test": True`.
5. Handle each `event["id"]` once, and answer `2xx` quickly.
6. Do not compare crypto amounts yourself. ArionPay decides when an invoice is paid, including payments inside your store's payment tolerance and payments you accept in Payment Radar.

## Test without real money

```python
invoice = arionpay.create_invoice(amount="10", order_id="test-1", asset="USDT_TRC20", test=True)
arionpay.simulate_payment(invoice["id"], "exact")   # your webhook gets invoice.paid, like a real payment
```

Scenarios: `exact`, `detected`, `confirm`, `underpaid_1pct`, `underpaid_5pct`, `overpaid`, `partial_half`, `late`.

`arionpay.send_test_webhook("invoice.paid")` sends a signed sample event to your store's webhook address. It describes a made-up invoice (`inv_test_...`) and must never change an order.

## Reference

| Method | Returns |
| --- | --- |
| `create_invoice(amount, currency="USD", order_id=None, ...)` | `{"id", "invoiceUrl", "status", "amountFiat", "currency", "asset", "address", "amountCrypto", "expiresAt", "orderId"}` |
| `get_invoice(invoice_id)` | The invoice, with the webhook field names. |
| `simulate_payment(invoice_id, scenario)` | Sandbox only (test invoices). |
| `send_test_webhook(event_type="invoice.paid")` | `{"delivered", "httpStatus", "eventId"}` |
| `currencies()` | Coins you can pass as `asset` / `allowed_assets`. |
| `rates(currency="USD")` | Current coin prices. |
| `verify_webhook(raw_body, signature_header)` | The event dict, or raises `SignatureError`. Also a module function taking the secret as third argument. |

`create_invoice` keyword arguments: `currency`, `order_id`, `description`, `customer_email`, `success_url`, `cancel_url`, `callback_url`, `asset`, `allowed_assets`, `expires_min` (5 to 1440), `metadata`, `idempotency_key`, `test`.

Errors are `ArionPayError` with `.status` (HTTP status, `0` when ArionPay could not be reached), `.code` (for example `idempotency_key_reused`, `rate_limited`, `unknown_asset`) and `.message`. Messages never contain your secret.

Options: `api_key`, `api_secret`, `webhook_secret`, `base_url` (default `https://merchant.arionpay.com`), `timeout` (seconds, default 20), `max_attempts` (default 3), `app_info`.

## License

MIT
