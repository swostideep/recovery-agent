"""Mock Razorpay-shaped interface for retries, links, and notifications.

Method shapes mirror the real SDK (client.order.create, client.payment.capture,
client.payment_link.create) so swapping in live Razorpay is a constructor
change, not a rewrite. Nothing here touches the network, the database, or
latent_json: the adapter hands the payment to resolve() and reads only
{success, reason} back off the result.

Circuit breaker is C1-C3: 3 consecutive 5xx trips it, every later call raises
CircuitOpenError, and it reopens only via reset(). It does not self-heal.
"""

import hashlib
import json
from types import SimpleNamespace

BREAKER_THRESHOLD = 3
RETRY_ACTIONS = ("RETRY_NOW", "RETRY_SCHEDULED")
LINK_ACTIONS = ("NUDGE_CUSTOMER", "SWITCH_RAIL")


class AdapterError(Exception):
    """5xx-equivalent. Counts toward the breaker (C1)."""


class CircuitOpenError(Exception):
    """Breaker is tripped. Manual reset only (C3)."""


def _digest(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class RazorpayAdapter:
    def __init__(self, resolve, fail_mode=False, breaker_threshold=BREAKER_THRESHOLD):
        self._resolve = resolve
        self.fail_mode = fail_mode
        self.breaker_threshold = breaker_threshold
        self.consecutive_5xx = 0
        self.tripped = False
        self.call_count = 0
        # Real-SDK call shapes: adapter.orders.create(...), .payment.capture(...)
        self.orders = SimpleNamespace(create=self._orders_create)
        self.payment = SimpleNamespace(capture=self._payment_capture)
        self.payment_link = SimpleNamespace(create=self._payment_link_create)

    def reset(self):
        """C3: the only way out of a tripped breaker."""
        self.tripped = False
        self.consecutive_5xx = 0

    def _guard(self, label, payment_id, action, at_time, attempt_no):
        """Breaker, then injected failure, then latency. Shared by every call
        so no method can accidentally skip the breaker."""
        if self.tripped:
            raise CircuitOpenError(
                f"breaker tripped after {self.breaker_threshold} consecutive 5xx;"
                " manual reset required (C3)")
        self.call_count += 1
        assert at_time.tzinfo is not None, f"naive at_time refused: {at_time!r}"
        if self.fail_mode:
            self.consecutive_5xx += 1
            if self.consecutive_5xx >= self.breaker_threshold:
                self.tripped = True
            raise AdapterError(
                f"502 Bad Gateway from mock rail"
                f" (consecutive 5xx: {self.consecutive_5xx})")
        self.consecutive_5xx = 0        # any clean contact clears the count
        return 120 + _digest(label, payment_id, action, at_time, attempt_no) % 780

    def _envelope(self, action, at_time, latency, body, outcome, recovered, reason):
        """Shaped to the actions table columns so the runner inserts directly."""
        return {
            "action_type": action,
            "executed_at": at_time.isoformat(),
            "outcome": outcome,
            "adapter_latency_ms": latency,
            "adapter_response_json": json.dumps(body, sort_keys=True),
            "amount_recovered_paise": recovered,
            "reason": reason,
        }

    def _orders_create(self, payment, action, at_time, attempt_no=1):
        """Creates an order. Moves no money, so it never calls the oracle --
        it is the API round-trip that the breaker watches."""
        if action not in RETRY_ACTIONS:
            raise ValueError(f"orders.create does not serve {action}")
        latency = self._guard("orders.create", payment["payment_id"], action,
                              at_time, attempt_no)
        body = {
            "id": f"order_{_digest(payment['payment_id'], attempt_no):08x}",
            "entity": "order",
            "amount": payment["amount_paise"],
            "amount_paid": 0,
            "currency": "INR",
            "receipt": payment["payment_id"],
            "status": "created",
            "attempts": attempt_no,
        }
        return self._envelope(action, at_time, latency, body, "success", 0,
                              "order created")

    def _payment_capture(self, payment, action, at_time, attempt_no=1):
        """The money movement. Outcome comes from the oracle, never from here."""
        if action not in RETRY_ACTIONS:
            raise ValueError(f"payment.capture does not serve {action}")
        latency = self._guard("payment.capture", payment["payment_id"], action,
                              at_time, attempt_no)
        result = self._resolve(payment, action, at_time, attempt_no)
        ok = result["success"]
        body = {
            "id": f"pay_{_digest(payment['payment_id'], action, attempt_no):08x}",
            "entity": "payment",
            "amount": payment["amount_paise"],
            "currency": "INR",
            "status": "captured" if ok else "failed",
            "method": payment["method"],
            "error_description": None if ok else result["reason"],
        }
        return self._envelope(action, at_time, latency, body,
                              "success" if ok else "fail",
                              payment["amount_paise"] if ok else 0,
                              result["reason"])

    def _payment_link_create(self, payment, action, at_time, attempt_no=1):
        """Nudge or rail switch. The link is 'paid' only if the oracle says the
        customer acted; content is generated and logged, never delivered."""
        if action not in LINK_ACTIONS:
            raise ValueError(f"payment_link.create does not serve {action}")
        latency = self._guard("payment_link.create", payment["payment_id"], action,
                              at_time, attempt_no)
        result = self._resolve(payment, action, at_time, attempt_no)
        ok = result["success"]
        ref = _digest(payment["payment_id"], action, attempt_no)
        body = {
            "id": f"plink_{ref:08x}",
            "entity": "payment_link",
            "amount": payment["amount_paise"],
            "currency": "INR",
            "status": "paid" if ok else "created",
            "short_url": f"https://rzp.io/i/{ref:08x}",
            "upi_link": action == "SWITCH_RAIL",
            "notify": {"sms": True, "email": True},
            "reminder_enable": False,
        }
        return self._envelope(action, at_time, latency, body,
                              "success" if ok else "fail",
                              payment["amount_paise"] if ok else 0,
                              result["reason"])
