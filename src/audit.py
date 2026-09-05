"""Records every decision and action to the audit trail.

The audit_log is append-only (SQL triggers) and hash-chained row to row:
    row_hash = sha256(seq|run_id|ts|event_type|payload_json|prev_hash)
verify_integrity() re-walks that chain and re-checks referential integrity.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_ROOT, "data", "recovery.db")
SCHEMA_PATH = os.path.join(_ROOT, "schema.sql")
GENESIS_HASH = "0" * 64


def get_conn(path=DB_PATH):
    """The ONLY sqlite3.connect() in this codebase.

    PRAGMA foreign_keys is per-connection and defaults to OFF; without this
    every FK in schema.sql is decorative and orphan rows are accepted (A3).
    """
    conn = sqlite3.connect(path)
    # Autocommit: append_event() drives its own BEGIN IMMEDIATE, which cannot
    # start inside the implicit transaction sqlite3 opens on any INSERT.
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path=DB_PATH, schema_path=SCHEMA_PATH):
    """Create the database from schema.sql. Safe to call repeatedly."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = get_conn(path)
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_hash(seq, run_id, ts, event_type, payload_json, prev_hash):
    """Hash formula is fixed by schema.sql. Changing it breaks every old chain."""
    parts = [str(seq), run_id, ts, event_type, payload_json, prev_hash]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def append_event(conn, run_id, event_type, payload, payment_id=None, decision_id=None):
    """Append one hash-chained event. Returns (seq, row_hash).

    seq is taken inside BEGIN IMMEDIATE because the hash covers it, so it
    cannot be left to AUTOINCREMENT after the fact.
    """
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    ts = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = 1 if row is None else row[0] + 1
        prev_hash = GENESIS_HASH if row is None else row[1]
        h = row_hash(seq, run_id, ts, event_type, payload_json, prev_hash)
        conn.execute(
            "INSERT INTO audit_log (seq, run_id, ts, event_type, payment_id,"
            " decision_id, payload_json, prev_hash, row_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (seq, run_id, ts, event_type, payment_id, decision_id,
             payload_json, prev_hash, h),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return seq, h


# (query, description) pairs. Each must return zero rows on a healthy database.
ORPHAN_CHECKS = [
    ("SELECT d.decision_id FROM decisions d LEFT JOIN payments p"
     " ON p.run_id = d.run_id AND p.payment_id = d.payment_id"
     " WHERE p.payment_id IS NULL",
     "decision with no parent payment"),
    ("SELECT a.action_id FROM actions a LEFT JOIN decisions d"
     " ON d.decision_id = a.decision_id WHERE d.decision_id IS NULL",
     "action with no parent decision (orphan money action)"),
    ("SELECT c.check_id FROM policy_checks c LEFT JOIN decisions d"
     " ON d.decision_id = c.decision_id WHERE d.decision_id IS NULL",
     "policy_check with no parent decision"),
    ("SELECT e.exception_id FROM exceptions e LEFT JOIN runs r"
     " ON r.run_id = e.run_id WHERE r.run_id IS NULL",
     "exception with no parent run"),
]


def verify_integrity(conn):
    """Re-walk the hash chain and re-check for orphans.

    Returns a list of problem strings; empty list means the trail is intact.
    Callers log the result rather than trusting that writes went well.
    """
    problems = []

    rows = conn.execute(
        "SELECT seq, run_id, ts, event_type, payload_json, prev_hash, row_hash"
        " FROM audit_log ORDER BY seq"
    ).fetchall()

    expected_prev = GENESIS_HASH
    for seq, run_id, ts, event_type, payload_json, prev_hash, stored in rows:
        if prev_hash != expected_prev:
            problems.append(
                f"seq {seq}: chain broken, prev_hash {prev_hash[:12]}..."
                f" does not match previous row {expected_prev[:12]}..."
            )
        recomputed = row_hash(seq, run_id, ts, event_type, payload_json, prev_hash)
        if recomputed != stored:
            problems.append(
                f"seq {seq}: row_hash mismatch, stored {stored[:12]}..."
                f" recomputed {recomputed[:12]}... (row was altered)"
            )
        expected_prev = stored

    for query, description in ORPHAN_CHECKS:
        found = conn.execute(query).fetchall()
        if found:
            ids = ", ".join(str(r[0]) for r in found[:5])
            problems.append(f"{len(found)} x {description}: {ids}")

    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if not fk:
        problems.append("PRAGMA foreign_keys is OFF on this connection")

    return problems
