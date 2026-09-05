"""Circuit breaker C1-C3 and the closed action set at the rail boundary."""
import json
import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
import simulator as sim
from adapter import AdapterError, CircuitOpenError, RazorpayAdapter


class Breaker(unittest.TestCase):
    def setUp(self):
        self.pay = sim.generate_batch(42)[0]
        self.at = datetime.fromisoformat(self.pay["failed_at"]) + timedelta(hours=2)

    def call(self, adapter):
        return adapter.payment.capture(self.pay, "RETRY_NOW", self.at, 1)

    def test_three_consecutive_5xx_trip_it(self):
        a = RazorpayAdapter(sim.resolve, fail_mode=True)
        for expected in (1, 2, 3):
            with self.assertRaises(AdapterError):
                self.call(a)
            self.assertEqual(expected, a.consecutive_5xx)
        self.assertTrue(a.tripped)
        with self.assertRaises(CircuitOpenError):
            self.call(a)

    def test_trip_blocks_every_method(self):
        a = RazorpayAdapter(sim.resolve, fail_mode=True)
        for _ in range(3):
            with self.assertRaises(AdapterError):
                self.call(a)
        for fn, action in ((a.orders.create, "RETRY_NOW"),
                           (a.payment_link.create, "NUDGE_CUSTOMER")):
            with self.assertRaises(CircuitOpenError):
                fn(self.pay, action, self.at, 1)

    def test_no_self_heal_manual_reset_only(self):
        a = RazorpayAdapter(sim.resolve, fail_mode=True)
        for _ in range(3):
            with self.assertRaises(AdapterError):
                self.call(a)
        a.fail_mode = False
        with self.assertRaises(CircuitOpenError):
            self.call(a)          # clearing the fault must not reopen it
        a.reset()
        self.assertFalse(a.tripped)
        self.assertIn(self.call(a)["outcome"], ("success", "fail"))

    def test_clean_call_clears_the_counter(self):
        a = RazorpayAdapter(sim.resolve, fail_mode=True)
        for _ in range(2):
            with self.assertRaises(AdapterError):
                self.call(a)
        a.fail_mode = False
        self.call(a)
        self.assertEqual(0, a.consecutive_5xx)
        self.assertFalse(a.tripped)


class Boundary(unittest.TestCase):
    def setUp(self):
        self.pay = sim.generate_batch(42)[0]
        self.at = datetime.fromisoformat(self.pay["failed_at"]) + timedelta(hours=2)
        self.a = RazorpayAdapter(sim.resolve)

    def test_method_refuses_an_action_it_does_not_serve(self):
        with self.assertRaises(ValueError):
            self.a.orders.create(self.pay, "NUDGE_CUSTOMER", self.at, 1)
        with self.assertRaises(ValueError):
            self.a.payment_link.create(self.pay, "RETRY_NOW", self.at, 1)

    def test_envelope_matches_actions_columns(self):
        env = self.a.payment.capture(self.pay, "RETRY_NOW", self.at, 1)
        for key in ("action_type", "executed_at", "outcome", "adapter_latency_ms",
                    "adapter_response_json", "amount_recovered_paise"):
            self.assertIn(key, env)
        json.loads(env["adapter_response_json"])

    def test_latency_is_deterministic(self):
        a, b = (self.a.payment.capture(self.pay, "RETRY_NOW", self.at, 1)
                for _ in range(2))
        self.assertEqual(a["adapter_latency_ms"], b["adapter_latency_ms"])

    def test_adapter_never_reads_ground_truth(self):
        import adapter as mod
        with open(mod.__file__) as f:
            body = [ln for ln in f if not ln.strip().startswith("#")]
        code = "".join(body).split('"""', 2)[-1]
        self.assertNotIn("latent_json", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
