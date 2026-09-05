"""The audit trail claims: append-only, hash-chained, foreign keys enforced."""
import os
import sqlite3
import tempfile
import unittest

import context  # noqa: F401
import audit


class AuditTrail(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        self.conn = audit.init_db(self.path)
        self.conn.execute("INSERT INTO runs VALUES('r','agent',1,'t',NULL,'v1.3',NULL)")

    def test_update_is_blocked_by_trigger(self):
        audit.append_event(self.conn, "r", "X", {"a": 1})
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            self.conn.execute("UPDATE audit_log SET ts='hacked'")
        self.assertIn("append-only", str(cm.exception))
        self.assertNotEqual("hacked",
                            self.conn.execute("SELECT ts FROM audit_log").fetchone()[0])

    def test_delete_is_blocked_by_trigger(self):
        audit.append_event(self.conn, "r", "X", {"a": 1})
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit_log")
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])

    def test_healthy_chain_verifies(self):
        for event in ("PAYMENT_INGESTED", "DIAGNOSED", "ACTION_EXECUTED"):
            audit.append_event(self.conn, "r", event, {"e": event})
        self.assertEqual([], audit.verify_integrity(self.conn))

    def test_forged_row_is_detected(self):
        """Triggers allow INSERT, so a forged append is the real attack."""
        audit.append_event(self.conn, "r", "X", {"a": 1})
        self.conn.execute("INSERT INTO audit_log VALUES(2,'r','2026-01-01','FORGED',"
                          "NULL,NULL,'{\"amount\":999999}','deadbeef','fakehash')")
        problems = audit.verify_integrity(self.conn)
        self.assertTrue(any("chain broken" in p for p in problems), problems)
        self.assertTrue(any("row_hash mismatch" in p for p in problems), problems)

    def test_foreign_keys_are_enforced(self):
        """PRAGMA foreign_keys defaults OFF per connection; get_conn sets it."""
        self.assertEqual(1, self.conn.execute("PRAGMA foreign_keys").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO decisions VALUES('d','no-run','no-pay',1,"
                              "'t','UNKNOWN',0.1,0.1,'STOP',NULL,'rules',NULL,NULL)")

    def test_orphan_action_is_reported(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("INSERT INTO actions VALUES('a','missing','RETRY_NOW','t',"
                          "NULL,'success',NULL,10,'{}',0)")
        self.assertTrue(any("orphan money action" in p
                            for p in audit.verify_integrity(self.conn)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
