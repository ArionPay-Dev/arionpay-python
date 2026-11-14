"""Runs against a real ArionPay server. Skipped unless ARIONPAY_IT_BASE_URL is set (see the Node SDK test for the setup).

The store's webhook address must be http://127.0.0.1:<ARIONPAY_IT_HOOK_PORT>/hook (dev server with WEBHOOK_ALLOW_PRIVATE).
"""

import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from arionpay import ArionPay, ArionPayError, SignatureError, is_test_event

ENV = os.environ


@unittest.skipUnless(ENV.get("ARIONPAY_IT_BASE_URL"), "set ARIONPAY_IT_BASE_URL to run against a real server")
class IntegrationTest(unittest.TestCase):
    def test_full_sandbox_payment_with_real_webhook(self):
        ap = ArionPay(ENV["ARIONPAY_IT_API_KEY"], ENV["ARIONPAY_IT_API_SECRET"], webhook_secret=ENV["ARIONPAY_IT_WEBHOOK_SECRET"], base_url=ENV["ARIONPAY_IT_BASE_URL"])
        port = int(ENV.get("ARIONPAY_IT_HOOK_PORT", "18099"))
        received = []
        refused = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    received.append(ap.verify_webhook(raw, self.headers.get("ArionPay-Signature")))
                    self.send_response(200)
                except SignatureError as e:
                    refused.append(str(e))
                    self.send_response(401)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", port), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def wait_for(pred, seconds=60):
            end = time.time() + seconds
            while time.time() < end:
                for e in received:
                    if pred(e):
                        return e
                time.sleep(0.5)
            self.fail("webhook not received; got %s, refused %s" % ([e["type"] for e in received], refused))

        try:
            self.assertTrue(any(c["symbol"] == "USDT_TRC20" for c in ap.currencies()))
            order_id = "py-it-%d" % time.time()
            inv = ap.create_invoice(amount="7.25", currency="USD", order_id=order_id, asset="USDT_TRC20", test=True, metadata={"cart": "py"}, callback_url="http://127.0.0.1:%d/hook" % port)
            self.assertRegex(inv["id"], r"^[0-9a-f]{24}$")
            self.assertTrue(inv["invoiceUrl"].endswith("/pay/" + inv["id"]))

            key = "py-key-%d" % time.time()
            a = ap.create_invoice(amount="5", order_id=order_id + "-b", idempotency_key=key, test=True)
            b = ap.create_invoice(amount="5", order_id=order_id + "-b", idempotency_key=key, test=True)
            self.assertEqual(a["id"], b["id"])
            with self.assertRaises(ArionPayError) as cm:
                ap.create_invoice(amount="6", order_id=order_id + "-b", idempotency_key=key, test=True)
            self.assertEqual(cm.exception.code, "idempotency_key_reused")

            read = ap.get_invoice(inv["id"])
            self.assertEqual(read["status"], "new")
            self.assertEqual(read["metadata"], {"cart": "py"})

            ap.simulate_payment(inv["id"], "exact")
            paid = wait_for(lambda e: e["type"] == "invoice.paid" and e["data"]["invoice"]["id"] == inv["id"])
            self.assertEqual(paid["data"]["invoice"]["orderId"], order_id)
            self.assertTrue(paid["data"]["invoice"]["test"])
            self.assertFalse(is_test_event(paid))

            read = ap.get_invoice(inv["id"])
            self.assertEqual(read["status"], "paid")
            self.assertEqual(read["amountFiat"], 7.25)

            t = ap.send_test_webhook("invoice.expired")
            self.assertTrue(t.get("delivered"), t)
            self.assertTrue(is_test_event(wait_for(lambda e: e["id"] == t["eventId"])))

            with self.assertRaises(ArionPayError) as cm:
                ap.get_invoice("0" * 24)
            self.assertEqual(cm.exception.status, 404)
            with self.assertRaises(ArionPayError) as cm:
                ArionPay(ENV["ARIONPAY_IT_API_KEY"], "sk_wrong", base_url=ENV["ARIONPAY_IT_BASE_URL"]).get_invoice(inv["id"])
            self.assertEqual(cm.exception.status, 401)
            self.assertEqual(refused, [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
