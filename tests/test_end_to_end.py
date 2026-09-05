"""The full pipeline, into a throwaway database. These are the README claims."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime

import context  # noqa: F401
import audit
import runner

MONEY = "('RETRY_NOW','RETRY_SCHEDULED','SWITCH_RAIL','NUDGE_CUSTOMER')"


def run_pipeline(**kw):
    path = os.path.join(tempfile.mkdtemp(), "e2e.db")
    with contextlib.redirect_stdout(io.StringIO()):
        conn = runner.main(db=path, **kw)
    return conn


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = run_pipeline()

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def test_all_three_arms_completed(self):
        arms = {r[0] for r in self.q("SELECT arm FROM runs")}
        self.assertEqual({"do_nothing", "naive_retry", "agent"}, arms)
        self.assertEqual(0, len(self.q("SELECT 1 FROM runs WHERE finished_at IS NULL")))

    def test_audit_chain_and_referential_integrity(self):
        self.assertEqual([], audit.verify_integrity(self.conn))

    def test_do_nothing_recovers_nothing(self):
        self.assertEqual(0, self.q("SELECT recovered_paise FROM v_run_summary"
                                   " WHERE arm='do_nothing'")[0][0])

    def test_every_payment_reaches_a_terminal_state(self):
        for arm in ("do_nothing", "naive_retry", "agent"):
            open_rows = self.q("SELECT COUNT(*) FROM payments WHERE run_id=?"
                               " AND terminal_status IS NULL", f"{arm}-42")[0][0]
            self.assertEqual(0, open_rows, arm)

    def test_agent_never_acts_in_quiet_hours(self):
        """B3, including nudges: G1 makes the nudge subject to it."""
        rows = self.q(f"""SELECT a.executed_at FROM actions a
            JOIN decisions d ON d.decision_id=a.decision_id
            WHERE d.run_id='agent-42' AND a.outcome IN ('success','fail')
              AND a.action_type IN {MONEY}""")
        for (stamp,) in rows:
            hour = datetime.fromisoformat(stamp).hour
            self.assertTrue(9 <= hour < 21, f"acted at {stamp}")

    def test_agent_never_exceeds_the_b1_attempt_cap(self):
        worst = self.q("SELECT MAX(attempts_used) FROM payments"
                       " WHERE run_id='agent-42'")[0][0]
        self.assertLessEqual(worst, 3)

    def test_agent_never_auto_executes_above_the_b5_ceiling(self):
        leaked = self.q(f"""SELECT COUNT(*) FROM actions a
            JOIN decisions d ON d.decision_id=a.decision_id
            JOIN payments p ON p.run_id=d.run_id AND p.payment_id=d.payment_id
            WHERE d.run_id='agent-42' AND a.action_type IN {MONEY}
              AND a.outcome IN ('success','fail') AND p.amount_paise > 2500000""")[0][0]
        self.assertEqual(0, leaked)

    def test_agent_never_nudges_an_opted_out_customer(self):
        leaked = self.q("""SELECT COUNT(*) FROM actions a
            JOIN decisions d ON d.decision_id=a.decision_id
            JOIN payments p ON p.run_id=d.run_id AND p.payment_id=d.payment_id
            WHERE d.run_id='agent-42' AND a.action_type='NUDGE_CUSTOMER'
              AND a.outcome IN ('success','fail') AND p.comms_opt_out=1""")[0][0]
        self.assertEqual(0, leaked)

    def test_every_action_has_a_parent_decision(self):
        self.assertEqual(0, self.q("""SELECT COUNT(*) FROM actions a
            LEFT JOIN decisions d ON d.decision_id=a.decision_id
            WHERE d.decision_id IS NULL""")[0][0])

    def test_agent_logs_policy_checks_and_naive_logs_none(self):
        agent = self.q("""SELECT COUNT(*) FROM policy_checks c
            JOIN decisions d ON d.decision_id=c.decision_id
            WHERE d.run_id='agent-42'""")[0][0]
        naive = self.q("""SELECT COUNT(*) FROM policy_checks c
            JOIN decisions d ON d.decision_id=c.decision_id
            WHERE d.run_id='naive_retry-42'""")[0][0]
        self.assertGreater(agent, 500)
        self.assertEqual(0, naive)

    def test_agent_beats_naive_on_rupees_per_attempt(self):
        rates = {}
        for arm in ("naive_retry", "agent"):
            rec, att = self.q("SELECT recovered_paise, total_attempts"
                              " FROM v_run_summary WHERE arm=?", arm)[0]
            rates[arm] = rec / att
        self.assertGreater(rates["agent"], rates["naive_retry"])

    def test_agent_attempts_unrecoverable_payments_far_less_than_naive(self):
        counts = {}
        for arm in ("naive_retry", "agent"):
            counts[arm] = self.q(f"""SELECT COUNT(*) FROM actions a
                JOIN decisions d ON d.decision_id=a.decision_id
                JOIN payments p ON p.run_id=d.run_id AND p.payment_id=d.payment_id
                WHERE d.run_id=? AND a.outcome IN ('success','fail')
                  AND a.action_type IN ('RETRY_NOW','RETRY_SCHEDULED','SWITCH_RAIL')
                  AND json_extract(p.latent_json,'$.recoverable')=0""", f"{arm}-42")[0][0]
        self.assertLess(counts["agent"], counts["naive_retry"] / 2)

    def test_exceptions_are_surfaced_not_swallowed(self):
        self.assertGreater(self.q("SELECT COUNT(*) FROM exceptions"
                                  " WHERE run_id='agent-42'")[0][0], 0)


class B6Halt(unittest.TestCase):
    def test_run_halts_at_the_configured_cap(self):
        conn = run_pipeline(n=20, b6_limit=10)
        executed = conn.execute(f"""SELECT COUNT(*) FROM actions a
            JOIN decisions d ON d.decision_id=a.decision_id
            WHERE d.run_id='agent-42' AND a.outcome IN ('success','fail')
              AND a.action_type IN {MONEY}""").fetchone()[0]
        self.assertLessEqual(executed, 10)
        self.assertGreater(conn.execute(
            "SELECT COUNT(*) FROM payments WHERE run_id='agent-42'"
            " AND terminal_status='dead_letter'").fetchone()[0], 0)


class Reproducibility(unittest.TestCase):
    def test_two_runs_produce_identical_results(self):
        def summary(conn):
            return conn.execute("SELECT arm, recovered_paise, total_attempts"
                                " FROM v_run_summary ORDER BY arm").fetchall()
        self.assertEqual(summary(run_pipeline()), summary(run_pipeline()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
