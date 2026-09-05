"""The batch is seeded and reproducible, and the oracle is honest."""
import json
import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
import simulator as sim

BATCH = sim.generate_batch(42)
HEALTH = sim.generate_issuer_health(42, BATCH)


class Determinism(unittest.TestCase):
    def test_same_seed_is_identical(self):
        self.assertEqual(BATCH, sim.generate_batch(42))

    def test_different_seed_differs(self):
        self.assertNotEqual(BATCH, sim.generate_batch(43))

    def test_sub_minute_drift_does_not_change_the_outcome(self):
        """Arms must stay comparable: microsecond drift cannot flip a result."""
        pay = BATCH[0]
        base = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=5)
        first = sim.resolve(pay, "RETRY_NOW", base, 1)
        for sec, usec in ((7, 123), (31, 999), (59, 999999)):
            drifted = base + timedelta(seconds=sec, microseconds=usec)
            self.assertEqual(first, sim.resolve(pay, "RETRY_NOW", drifted, 1))

    def test_a_different_minute_rolls_differently(self):
        pay = [p for p in BATCH
               if json.loads(p["latent_json"])["recoverable"]][0]
        base = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=5)
        outcomes = {sim.resolve(pay, "RETRY_NOW", base + timedelta(minutes=m), 1)["success"]
                    for m in range(40)}
        self.assertEqual({True, False}, outcomes)


class Oracle(unittest.TestCase):
    def test_naive_datetime_is_refused(self):
        with self.assertRaises(AssertionError):
            sim.resolve(BATCH[0], "RETRY_NOW", datetime(2026, 1, 27, 10, 0), 1)

    def test_non_money_actions_never_recover(self):
        pay = BATCH[0]
        at = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=1)
        for action in ("ESCALATE_HUMAN", "STOP"):
            self.assertFalse(sim.resolve(pay, action, at, 1)["success"])

    def test_hard_classes_never_recover(self):
        for cause in ("INSTRUMENT_DEAD", "RISK_DECLINE"):
            pay = [p for p in BATCH
                   if json.loads(p["latent_json"])["true_cause"] == cause][0]
            at = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=2)
            for action in ("RETRY_NOW", "SWITCH_RAIL", "NUDGE_CUSTOMER"):
                self.assertFalse(sim.resolve(pay, action, at, 1)["success"], action)

    def test_retry_before_the_gate_fails(self):
        pay = [p for p in BATCH
               if json.loads(p["latent_json"])["true_cause"] == "ISSUER_DOWNTIME"][0]
        recovers = datetime.fromisoformat(
            json.loads(pay["latent_json"])["issuer_recovers_at"])
        early = sim.resolve(pay, "RETRY_NOW", recovers - timedelta(minutes=10), 1)
        self.assertFalse(early["success"])
        self.assertIn("too early", early["reason"])

    def test_intent_decays_over_time(self):
        pay = [p for p in BATCH
               if json.loads(p["latent_json"])["true_cause"] == "TRANSIENT_GATEWAY"
               and json.loads(p["latent_json"])["recoverable"]][0]
        failed = datetime.fromisoformat(pay["failed_at"])
        probs = []
        for hours in (1, 24, 72):
            reason = sim.resolve(pay, "RETRY_NOW", failed + timedelta(hours=hours), 1)["reason"]
            probs.append(float(reason.split("p=")[1].split(",")[0]))
        self.assertTrue(probs[0] > probs[1] > probs[2], probs)


class PolicyGatesArePopulated(unittest.TestCase):
    """A zero here means a policy rule can never fire and a README claim is unbacked."""

    def latent(self):
        return [json.loads(p["latent_json"]) for p in BATCH]

    def test_about_thirty_percent_unrecoverable(self):
        share = sum(1 for l in self.latent() if not l["recoverable"]) / len(BATCH)
        self.assertTrue(0.25 <= share <= 0.35, share)

    def test_big_tickets_exist_for_b5(self):
        self.assertGreaterEqual(
            sum(1 for p in BATCH if p["amount_paise"] > 2_500_000), 5)

    def test_quiet_hour_failures_exist_for_b3(self):
        self.assertGreater(sum(1 for p in BATCH if sim.to_ist(
            datetime.fromisoformat(p["failed_at"])).hour >= 21), 0)

    def test_unmapped_codes_exist_for_g3(self):
        self.assertGreater(
            sum(1 for p in BATCH if p["error_code"] in sim.UNMAPPED), 0)

    def test_both_g5_branches_are_reachable(self):
        ins = [datetime.fromisoformat(p["failed_at"]) for p, l
               in zip(BATCH, self.latent()) if l["true_cause"] == "INSUFFICIENT_FUNDS"]
        inside = sum(1 for d in ins if d + timedelta(hours=72) >= sim.FEB1)
        self.assertGreater(inside, 0, "no unclamped G5 case")
        self.assertGreater(len(ins) - inside, 0, "no P2 clamp case")

    def test_two_pairs_have_no_health_snapshot_for_g4(self):
        self.assertEqual(2, len(sim.dark_pairs(BATCH)))
        covered = {(h["issuer"], h["method"]) for h in HEALTH}
        for pair in sim.dark_pairs(BATCH):
            self.assertNotIn(pair, covered)

    def test_health_covers_the_b4_tail(self):
        last = max(datetime.fromisoformat(h["observed_at"]) for h in HEALTH)
        self.assertGreaterEqual(last, sim.T0 + timedelta(hours=192))

    def test_down_estimates_skew_optimistic(self):
        windows = sim._downtime_windows(BATCH)
        early = late = 0
        for row in (h for h in HEALTH if h["status"] == "down"):
            true_end = windows[(row["issuer"], row["method"])][1]
            if datetime.fromisoformat(row["estimated_recovery_at"]) < true_end:
                early += 1
            else:
                late += 1
        self.assertTrue(0.25 <= early / (early + late) <= 0.55, early / (early + late))


if __name__ == "__main__":
    unittest.main(verbosity=2)
