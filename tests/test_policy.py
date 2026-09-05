"""Every claim the README makes about bounds, gating and precedence."""
import json
import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
import policy as pol
import simulator as sim

BATCH = sim.generate_batch(42)


def find(cause):
    return [p for p in BATCH
            if json.loads(p["latent_json"])["true_cause"] == cause][0]


def run(pay, action, cause, **kw):
    now = kw.pop("now", None) or datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=1)
    return pol.evaluate(pol.build_ctx(pay, action, cause, now, **kw))


def rule_ids(checks, result=None):
    return {c["rule_id"] for c in checks if result is None or c["result"] == result}


class Bounds(unittest.TestCase):
    def test_b1_attempt_cap_is_absolute(self):
        verdict, _, checks = run(find("TRANSIENT_GATEWAY"), "RETRY_NOW",
                                 "TRANSIENT_GATEWAY", attempts_used=3)
        self.assertEqual("deny", verdict)
        self.assertIn("B1", rule_ids(checks, "deny"))

    def test_b2_one_nudge_per_customer_per_24h(self):
        verdict, _, checks = run(find("AUTH_TIMEOUT"), "NUDGE_CUSTOMER",
                                 "AUTH_TIMEOUT", nudges_24h=1)
        self.assertEqual("deny", verdict)
        self.assertIn("B2", rule_ids(checks, "deny"))

    def test_b3_defers_out_of_quiet_hours(self):
        pay = find("TRANSIENT_GATEWAY")
        night = datetime.fromisoformat(pay["failed_at"]).replace(hour=23, minute=30)
        verdict, params, checks = run(pay, "RETRY_NOW", "TRANSIENT_GATEWAY", now=night)
        if verdict != "deny":
            self.assertIn("B3", rule_ids(checks))
            self.assertTrue(9 <= sim.to_ist(params["at"]).hour < 21,
                            f"scheduled into quiet hours: {params['at']}")

    def test_b3_applies_to_nudges_too(self):
        """G1 makes the nudge subject to B3. It once bypassed timing entirely."""
        pay = find("INSTRUMENT_DEAD")
        night = datetime.fromisoformat(pay["failed_at"]).replace(hour=2, minute=0)
        verdict, params, checks = run(pay, "NUDGE_CUSTOMER", "INSTRUMENT_DEAD", now=night)
        if verdict != "deny":
            self.assertTrue(9 <= sim.to_ist(params["at"]).hour < 21,
                            f"nudge scheduled into quiet hours: {params['at']}")

    def test_b5_big_ticket_never_auto_executes(self):
        big = [p for p in BATCH if p["amount_paise"] > 2_500_000][0]
        self.assertEqual("deny", run(big, "RETRY_NOW", "TRANSIENT_GATEWAY")[0])
        self.assertEqual("allow", run(big, "ESCALATE_HUMAN", "TRANSIENT_GATEWAY")[0])

    def test_b6_run_cap_denies(self):
        verdict, _, checks = run(find("TRANSIENT_GATEWAY"), "RETRY_NOW",
                                 "TRANSIENT_GATEWAY", actions_this_run=10, b6_limit=10)
        self.assertEqual("deny", verdict)
        self.assertIn("B6", rule_ids(checks, "deny"))


class Gating(unittest.TestCase):
    def test_g1_denies_attempts_permits_one_nudge(self):
        pay = find("INSTRUMENT_DEAD")
        self.assertEqual("deny", run(pay, "RETRY_NOW", "INSTRUMENT_DEAD")[0])
        self.assertIn(run(pay, "NUDGE_CUSTOMER", "INSTRUMENT_DEAD")[0], ("allow", "clamp"))

    def test_g2_risk_decline_never_retried(self):
        pay = find("RISK_DECLINE")
        for action in ("RETRY_NOW", "RETRY_SCHEDULED", "SWITCH_RAIL", "NUDGE_CUSTOMER"):
            self.assertEqual("deny", run(pay, action, "RISK_DECLINE")[0], action)
        self.assertEqual("allow", run(pay, "ESCALATE_HUMAN", "RISK_DECLINE")[0])

    def test_g3_unknown_escalates(self):
        pay = find("UNKNOWN")
        self.assertEqual("deny", run(pay, "RETRY_NOW", "UNKNOWN")[0])
        self.assertEqual("allow", run(pay, "ESCALATE_HUMAN", "UNKNOWN")[0])

    def test_g4_denies_retry_now_and_uses_health_estimate(self):
        pay = find("ISSUER_DOWNTIME")
        self.assertEqual("deny", run(pay, "RETRY_NOW", "ISSUER_DOWNTIME")[0])
        now = datetime.fromisoformat(pay["failed_at"])
        est = (now + timedelta(hours=3)).isoformat()
        _, params, _ = run(pay, "RETRY_SCHEDULED", "ISSUER_DOWNTIME", now=now,
                           health={"status": "down", "estimated_recovery_at": est})
        self.assertGreaterEqual(params["at"], datetime.fromisoformat(est)
                                + timedelta(minutes=15))

    def test_g4_falls_back_without_a_snapshot(self):
        pay = find("ISSUER_DOWNTIME")
        now = datetime.fromisoformat(pay["failed_at"])
        _, params, _ = run(pay, "RETRY_SCHEDULED", "ISSUER_DOWNTIME", now=now, health=None)
        self.assertGreaterEqual(params["at"], now + timedelta(minutes=90))

    def test_g6_auth_timeout_forbids_silent_retry(self):
        pay = find("AUTH_TIMEOUT")
        self.assertEqual("deny", run(pay, "RETRY_NOW", "AUTH_TIMEOUT")[0])
        self.assertEqual("deny", run(pay, "RETRY_SCHEDULED", "AUTH_TIMEOUT")[0])

    def test_g8_switch_rail_requires_card_and_a_handle(self):
        upi = [p for p in BATCH if p["method"] == "upi_collect"][0]
        self.assertEqual("deny", run(upi, "SWITCH_RAIL", "AUTH_TIMEOUT")[0])
        nohandle = [p for p in BATCH
                    if p["method"] == "card" and not p["has_upi_handle"]][0]
        self.assertEqual("deny", run(nohandle, "SWITCH_RAIL", "AUTH_TIMEOUT")[0])


class Precedence(unittest.TestCase):
    def test_p2_clamps_the_heuristic_inside_the_bound(self):
        pay = [p for p in BATCH
               if json.loads(p["latent_json"])["true_cause"] == "INSUFFICIENT_FUNDS"
               and datetime.fromisoformat(p["failed_at"]).day <= 27][0]
        now = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=1)
        verdict, params, checks = run(pay, "RETRY_SCHEDULED", "INSUFFICIENT_FUNDS", now=now)
        ceiling = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=72)
        self.assertIn("P2", rule_ids(checks))
        self.assertLessEqual(params["at"], ceiling, "clamp escaped the B4 bound")

    def test_b4_denies_when_no_feasible_time_remains(self):
        pay = find("INSUFFICIENT_FUNDS")
        late = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=71)
        verdict, _, checks = run(pay, "RETRY_SCHEDULED", "INSUFFICIENT_FUNDS", now=late)
        self.assertEqual("deny", verdict)
        self.assertIn("B4", rule_ids(checks, "deny"))

    def test_no_short_circuit_every_rule_is_logged(self):
        big = [p for p in BATCH if p["amount_paise"] > 2_500_000][0]
        _, _, checks = run(big, "RETRY_NOW", "RISK_DECLINE", attempts_used=3)
        self.assertGreaterEqual(len(rule_ids(checks, "deny")), 3, checks)

    def test_terminal_payment_accepts_nothing(self):
        verdict, _, checks = run(find("TRANSIENT_GATEWAY"), "RETRY_NOW",
                                 "TRANSIENT_GATEWAY", terminal_status="recovered")
        self.assertEqual("deny", verdict)
        self.assertIn("S1", rule_ids(checks, "deny"))

    def test_engine_never_substitutes_the_action(self):
        """P4: clamp narrows a parameter, it never changes the action type."""
        for pay, cause in ((find("TRANSIENT_GATEWAY"), "TRANSIENT_GATEWAY"),
                           (find("INSUFFICIENT_FUNDS"), "INSUFFICIENT_FUNDS")):
            for action in ("RETRY_NOW", "RETRY_SCHEDULED", "NUDGE_CUSTOMER"):
                verdict, params, _ = run(pay, action, cause)
                self.assertIn(verdict, ("allow", "deny", "clamp"))
                self.assertNotIn("action", params)

    def test_ctx_never_carries_latent_state(self):
        ctx = pol.build_ctx(BATCH[0], "RETRY_NOW", "TRANSIENT_GATEWAY",
                            datetime.fromisoformat(BATCH[0]["failed_at"]))
        self.assertNotIn("latent_json", ctx["pay"])
        self.assertNotIn("latent_json", json.dumps(ctx, default=str))


if __name__ == "__main__":
    unittest.main(verbosity=2)
