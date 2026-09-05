"""Rules-only classification, the health override, and the C4 fallback."""
import json
import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
import diagnose as d
import simulator as sim

BATCH = sim.generate_batch(42)
NOW = sim.T0 + timedelta(hours=130)


class Isolation(unittest.TestCase):
    def test_visible_strips_ground_truth(self):
        self.assertNotIn("latent_json", d.visible(BATCH[0]))

    def test_anthropic_is_not_imported_at_module_level(self):
        """Every metric must reproduce with no SDK and no key."""
        with open(d.__file__) as f:
            for line in f:
                self.assertFalse(line.startswith(("import anthropic", "from anthropic")))

    def test_result_matches_decisions_columns(self):
        result = d.diagnose(BATCH[0], NOW)
        for key in ("root_cause", "confidence", "success_score",
                    "diagnosis_source", "model_version", "prompt_version"):
            self.assertIn(key, result)
        self.assertIn(result["diagnosis_source"], ("rules", "llm", "fallback"))


class Classification(unittest.TestCase):
    def test_unmapped_code_becomes_unknown(self):
        pay = [p for p in BATCH if p["error_code"] in sim.UNMAPPED][0]
        self.assertEqual("UNKNOWN", d.diagnose(pay, NOW)["root_cause"])

    def test_health_reinterprets_an_ambiguous_code(self):
        pay = [p for p in BATCH if p["error_code"] in d.AMBIGUOUS][0]
        self.assertEqual("TRANSIENT_GATEWAY",
                         d.diagnose(pay, NOW, health={"status": "healthy"})["root_cause"])
        self.assertEqual("ISSUER_DOWNTIME",
                         d.diagnose(pay, NOW, health={"status": "down"})["root_cause"])

    def test_health_does_not_override_an_unambiguous_code(self):
        pay = [p for p in BATCH if p["error_code"] == "card_expired"][0]
        self.assertEqual("INSTRUMENT_DEAD",
                         d.diagnose(pay, NOW, health={"status": "down"})["root_cause"])

    def test_accuracy_is_at_least_seventy_five_percent(self):
        ok = sum(1 for p in BATCH
                 if d.diagnose(p, NOW)["root_cause"]
                 == json.loads(p["latent_json"])["true_cause"])
        self.assertGreaterEqual(ok / len(BATCH), 0.75)


class C4Fallback(unittest.TestCase):
    def test_missing_llm_falls_back_and_never_raises(self):
        result = d.diagnose(BATCH[0], NOW, use_llm=True, llm_threshold=1.0)
        self.assertEqual("fallback", result["diagnosis_source"])
        self.assertIn(result["root_cause"], d.CAUSE_PRIOR)

    def test_fallback_keeps_the_rules_answer(self):
        rules = d.diagnose(BATCH[0], NOW)
        fell_back = d.diagnose(BATCH[0], NOW, use_llm=True, llm_threshold=1.0)
        self.assertEqual(rules["root_cause"], fell_back["root_cause"])

    def test_high_confidence_never_consults_the_llm(self):
        pay = [p for p in BATCH if p["error_code"] == "card_expired"][0]
        self.assertEqual("rules", d.diagnose(pay, NOW, use_llm=True,
                                             llm_threshold=0.70)["diagnosis_source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
