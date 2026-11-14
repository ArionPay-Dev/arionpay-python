import hashlib
import hmac
import json
import time
import unittest

import arionpay
from arionpay import ArionPay, ArionPayError, SignatureError, is_test_event, is_valid_signature, parse_signature_header, sign_request, verify_webhook

SECRET = "whsec_unit_0001"
EVENT = {"id": "evt_abc", "type": "invoice.paid", "created": 1, "livemode": True, "data": {"invoice": {"id": "6af75d7ce83f10cb386f0981", "status": "paid"}}}


def h(key, data):
    return hmac.new(key.encode(), data if isinstance(data, bytes) else data.encode(), hashlib.sha256).hexdigest()


def signed(body, t=None, secret=SECRET):
    t = int(time.time()) if t is None else t
    b = body if isinstance(body, bytes) else body.encode()
    return "t=%d,v1=%s" % (t, h(secret, ("%d." % t).encode() + b))


class FakeTransport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, payload = a
        return status, {}, json.dumps(payload).encode()


def client(t, **kw):
    kw.setdefault("base_url", "https://example.test/")
    return ArionPay("pk_unit_000001", "sk_unit", transport=t, **kw)


class SignatureTests(unittest.TestCase):
    def test_request_signatures_match_backend(self):
        self.assertEqual(sign_request("sk_x", 1700000000, "POST", "/api/v1/invoices", b'{"a":1}'), h("sk_x", '1700000000.{"a":1}'))
        self.assertEqual(sign_request("sk_x", 1700000000, "get", "/api/v1/invoices/abc?x=1", b"ignored"), h("sk_x", "1700000000.GET /api/v1/invoices/abc?x=1"))

    def test_header_parsing(self):
        v1 = "a" * 64
        self.assertEqual(parse_signature_header("t=123,v1=" + v1), (123, v1))
        self.assertEqual(parse_signature_header(" v1=%s , t=5 " % v1.upper()), (5, v1))
        for bad in ["", None, "t=1", "v1=" + v1, "t=x,v1=" + v1, "t=1,v1=zz", "t=1,v1=%s0" % v1]:
            self.assertIsNone(parse_signature_header(bad), bad)

    def test_valid_bytes_and_str(self):
        body = json.dumps(EVENT)
        hdr = signed(body)
        self.assertEqual(verify_webhook(body, hdr, SECRET)["id"], "evt_abc")
        self.assertEqual(verify_webhook(body.encode(), hdr, SECRET)["type"], "invoice.paid")
        self.assertTrue(is_valid_signature(bytearray(body.encode()), hdr, SECRET))

    def test_non_ascii(self):
        ev = json.loads(json.dumps(EVENT))
        ev["data"]["invoice"]["description"] = "Café ☕ – 東京"
        body = json.dumps(ev, ensure_ascii=False).encode("utf-8")
        self.assertEqual(verify_webhook(body, signed(body), SECRET)["data"]["invoice"]["description"], "Café ☕ – 東京")

    def test_failures(self):
        body = json.dumps(EVENT)
        hdr = signed(body)
        with self.assertRaises(SignatureError):
            verify_webhook(body.replace("paid", "PAID"), hdr, SECRET)
        with self.assertRaises(SignatureError):
            verify_webhook(json.dumps(json.loads(body), indent=2), hdr, SECRET)
        with self.assertRaises(SignatureError):
            verify_webhook(body, hdr, "whsec_other")
        with self.assertRaisesRegex(SignatureError, "empty"):
            verify_webhook(body, hdr, "")
        old = int(time.time()) - 301
        with self.assertRaisesRegex(SignatureError, "stale"):
            verify_webhook(body, signed(body, old), SECRET)
        with self.assertRaisesRegex(SignatureError, "stale"):
            verify_webhook(body, signed(body, old + 602), SECRET)
        self.assertTrue(is_valid_signature(body, signed(body, old), SECRET, tolerance_sec=600))
        with self.assertRaisesRegex(SignatureError, "raw request body"):
            verify_webhook(json.loads(body), hdr, SECRET)
        with self.assertRaisesRegex(SignatureError, "Missing or malformed"):
            verify_webhook(body, None, SECRET)
        self.assertFalse(is_valid_signature({"x": 1}, hdr, SECRET))

    def test_signed_but_not_event(self):
        for body in ["not json", "{}", "[]", '{"id":"","type":"invoice.paid","data":{"invoice":{}}}', '{"id":"evt_1","type":"invoice.paid"}', '{"id":"evt_1","type":"invoice.paid","data":{"invoice":null}}', b"\xff\xfe"]:
            with self.assertRaises(SignatureError, msg=repr(body)):
                verify_webhook(body, signed(body), SECRET)

    def test_test_events(self):
        self.assertTrue(is_test_event(dict(EVENT, test=True)))
        self.assertTrue(is_test_event(dict(EVENT, id="evt_test_1")))
        self.assertTrue(is_test_event(dict(EVENT, data={"invoice": {"id": "inv_test_x"}})))
        self.assertFalse(is_test_event(EVENT))


class ClientTests(unittest.TestCase):
    def test_constructor_checks(self):
        with self.assertRaisesRegex(ValueError, "pk_"):
            ArionPay("nope", "s")
        with self.assertRaisesRegex(ValueError, "api_secret"):
            ArionPay("pk_abcdefgh", "")
        with self.assertRaisesRegex(ValueError, "http"):
            ArionPay("pk_abcdefgh", "s", base_url="ftp://x")

    def test_create_signs_exact_body(self):
        t = FakeTransport([(201, {"status": "success", "id": "a" * 24, "invoice_url": "https://x/pay/1", "data": {"id": "a" * 24, "invoice_url": "https://x/pay/1", "amount_fiat": 9.5, "currency": "EUR", "status": "new", "order_id": "7"}})])
        inv = client(t).create_invoice(amount="9.50", currency="EUR", order_id="7", metadata={"name": "Zoë"})
        self.assertEqual(inv["invoiceUrl"], "https://x/pay/1")
        self.assertEqual(inv["amountFiat"], 9.5)
        c = t.calls[0]
        self.assertEqual(c["url"], "https://example.test/api/v1/invoices")
        body = json.loads(c["body"].decode())
        self.assertEqual(body["amount"], "9.50")
        self.assertNotIn("description", body)
        self.assertTrue(body["idempotencyKey"].startswith("sdk_"))
        self.assertEqual(c["headers"]["x-signature"], h("sk_unit", ("%s." % c["headers"]["x-timestamp"]).encode() + c["body"]))

    def test_get_signed_over_path_with_base_path(self):
        t = FakeTransport([(200, {"invoiceId": "b" * 24, "status": "PAID", "amountFiat": 1, "currency": "USD", "test": True})])
        inv = client(t, base_url="https://example.test/gw/").get_invoice("b" * 24)
        self.assertEqual(inv["status"], "paid")
        self.assertTrue(inv["test"])
        c = t.calls[0]
        self.assertEqual(c["url"], "https://example.test/gw/api/v1/invoices/" + "b" * 24)
        self.assertEqual(c["headers"]["x-signature"], h("sk_unit", "%s.GET /gw/api/v1/invoices/%s" % (c["headers"]["x-timestamp"], "b" * 24)))

    def test_replay_resigned_once(self):
        t = FakeTransport([(401, {"error": "This signed request was already used. Sign each request with a fresh timestamp."}), (200, {"invoiceId": "c" * 24, "status": "NEW"})])
        self.assertEqual(client(t).get_invoice("c" * 24)["status"], "new")
        self.assertEqual(len(t.calls), 2)
        self.assertNotEqual(t.calls[0]["headers"]["x-timestamp"], t.calls[1]["headers"]["x-timestamp"])

    def test_create_retry_keeps_idempotency_key(self):
        t = FakeTransport([OSError("connection refused"), (201, {"id": "d" * 24, "invoice_url": "u", "data": {}})])
        client(t).create_invoice(amount=1)
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(json.loads(t.calls[0]["body"])["idempotencyKey"], json.loads(t.calls[1]["body"])["idempotencyKey"])

    def test_no_retry_for_simulate_and_codes(self):
        t = FakeTransport([(503, {"error": "busy"})])
        with self.assertRaises(ArionPayError) as cm:
            client(t).simulate_payment("e" * 24, "exact")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(len(t.calls), 1)
        t = FakeTransport([(409, {"error": "This Idempotency-Key was already used", "code": "idempotency_key_reused"})])
        with self.assertRaises(ArionPayError) as cm:
            client(t).create_invoice(amount="1", idempotency_key="k")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "idempotency_key_reused"))
        self.assertEqual(len(t.calls), 1)

    def test_unreachable(self):
        t = FakeTransport([OSError("down"), OSError("down")])
        with self.assertRaises(ArionPayError) as cm:
            client(t, max_attempts=2).get_invoice("f" * 24)
        self.assertEqual(cm.exception.status, 0)
        self.assertIn("Could not reach", str(cm.exception))
        self.assertEqual(len(t.calls), 2)

    def test_bad_input_never_reaches_network(self):
        t = FakeTransport([])
        with self.assertRaises(ValueError):
            client(t).get_invoice("../admin")
        with self.assertRaises(ValueError):
            client(t).simulate_payment("a" * 24, "free_money")
        with self.assertRaises(ValueError):
            client(t).create_invoice(amount=None)
        self.assertEqual(t.calls, [])

    def test_version(self):
        self.assertEqual(arionpay.__version__, arionpay.VERSION)


if __name__ == "__main__":
    unittest.main()
