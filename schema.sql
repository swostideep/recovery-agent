PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per experimental arm: 'do_nothing' | 'naive_retry' | 'agent'
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    arm           TEXT NOT NULL CHECK (arm IN ('do_nothing','naive_retry','agent')),
    seed          INTEGER NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    policy_version TEXT NOT NULL DEFAULT 'v1',
    notes         TEXT
);

-- The batch of failed payments. latent_json is simulator ground truth and is
-- NEVER read by the agent -- only by the simulator when resolving an action.
CREATE TABLE IF NOT EXISTS payments (
    payment_id       TEXT NOT NULL,
    run_id           TEXT NOT NULL REFERENCES runs(run_id),
    customer_id      TEXT NOT NULL,
    amount_paise     INTEGER NOT NULL CHECK (amount_paise > 0),
    method           TEXT NOT NULL,        -- card|upi_intent|upi_collect|netbanking|wallet|mandate
    issuer           TEXT,                 -- HDFC, ICICI, SBI, ...
    is_subscription  INTEGER NOT NULL DEFAULT 0,
    error_code       TEXT NOT NULL,
    error_reason     TEXT,
    failed_at        TEXT NOT NULL,
    has_upi_handle   INTEGER NOT NULL DEFAULT 0,
    comms_opt_out    INTEGER NOT NULL DEFAULT 0,
    latent_json      TEXT NOT NULL,
    terminal_status  TEXT,                 -- recovered|exhausted|expired|unrecoverable|escalated|dead_letter
    recovered_paise  INTEGER NOT NULL DEFAULT 0,
    recovered_at     TEXT,
    attempts_used    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, payment_id)
);

-- What the agent decided, before policy was applied.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL,
    payment_id        TEXT NOT NULL,
    attempt_no        INTEGER NOT NULL,
    decided_at        TEXT NOT NULL,
    root_cause        TEXT NOT NULL,
    confidence        REAL,
    success_score     REAL,
    proposed_action   TEXT NOT NULL,
    action_params_json TEXT,
    diagnosis_source  TEXT NOT NULL CHECK (diagnosis_source IN ('rules','llm','fallback')),
    model_version     TEXT,
    prompt_version    TEXT,
    FOREIGN KEY (run_id, payment_id) REFERENCES payments(run_id, payment_id)
);

-- One row per rule evaluated. This table IS the "gated" evidence.
CREATE TABLE IF NOT EXISTS policy_checks (
    check_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id  TEXT NOT NULL REFERENCES decisions(decision_id),
    rule_id      TEXT NOT NULL,            -- B1, G4, M3, S2, C1 ...
    rule_desc    TEXT NOT NULL,
    result       TEXT NOT NULL CHECK (result IN ('allow','deny')),
    reason       TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

-- Only actions that actually executed (or were blocked/deferred) land here.
CREATE TABLE IF NOT EXISTS actions (
    action_id         TEXT PRIMARY KEY,
    decision_id       TEXT NOT NULL REFERENCES decisions(decision_id),
    action_type       TEXT NOT NULL,
    executed_at       TEXT NOT NULL,
    scheduled_for     TEXT,
    outcome           TEXT NOT NULL CHECK (outcome IN ('success','fail','deferred','blocked','error')),
    blocked_by_rule   TEXT,
    adapter_latency_ms INTEGER,
    adapter_response_json TEXT,
    amount_recovered_paise INTEGER NOT NULL DEFAULT 0
);

-- The honest "could not resolve" list.
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    payment_id    TEXT,
    reason_code   TEXT NOT NULL,           -- ADAPTER_UNAVAILABLE, UNKNOWN_ERROR_CODE,
                                           -- NEEDS_HUMAN_APPROVAL, RISK_DECLINE, ...
    detail        TEXT,
    amount_paise  INTEGER NOT NULL DEFAULT 0,
    occurred_at   TEXT NOT NULL
);

-- Observable issuer health. Written by the simulator, readable by the agent.
-- estimated_recovery_at is deliberately noisy: it is an estimate, not truth.
-- The agent must NEVER read latent_json; this table is its only health signal.
CREATE TABLE IF NOT EXISTS issuer_health (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    issuer        TEXT NOT NULL,
    method        TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('healthy','degraded','down')),
    success_rate  REAL NOT NULL,
    estimated_recovery_at TEXT,
    UNIQUE (run_id, issuer, method, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_health_lookup
    ON issuer_health(run_id, issuer, method, observed_at);

-- Append-only, hash-chained. row_hash = sha256(seq|run_id|ts|event_type|payload_json|prev_hash)
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    ts           TEXT NOT NULL,
    event_type   TEXT NOT NULL,            -- PAYMENT_INGESTED, DIAGNOSED, POLICY_CHECK,
                                           -- ACTION_EXECUTED, ACTION_BLOCKED, BREAKER_TRIPPED,
                                           -- PAYMENT_TERMINAL, RUN_COMPLETED
    payment_id   TEXT,
    decision_id  TEXT,
    payload_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    row_hash     TEXT NOT NULL UNIQUE
);

-- Tamper-evidence enforced by the database, not by convention.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE forbidden');
END;

CREATE INDEX IF NOT EXISTS idx_payments_run     ON payments(run_id, terminal_status);
CREATE INDEX IF NOT EXISTS idx_decisions_pay    ON decisions(run_id, payment_id);
CREATE INDEX IF NOT EXISTS idx_checks_decision  ON policy_checks(decision_id);
CREATE INDEX IF NOT EXISTS idx_actions_decision ON actions(decision_id);
CREATE INDEX IF NOT EXISTS idx_audit_run        ON audit_log(run_id, seq);

-- Headline metrics table, straight out of SQL. This is your scoreboard.
CREATE VIEW IF NOT EXISTS v_run_summary AS
SELECT
    r.arm,
    r.run_id,
    COUNT(p.payment_id)                                       AS payments_at_risk,
    SUM(p.amount_paise)                                       AS value_at_risk_paise,
    SUM(p.recovered_paise)                                    AS recovered_paise,
    ROUND(100.0 * SUM(p.recovered_paise) / SUM(p.amount_paise), 2) AS recovery_rate_pct,
    SUM(CASE WHEN p.terminal_status = 'recovered' THEN 1 ELSE 0 END) AS payments_recovered,
    SUM(p.attempts_used)                                      AS total_attempts,
    ROUND(1.0 * SUM(p.attempts_used) / COUNT(p.payment_id), 2) AS attempts_per_payment,
    (SELECT COUNT(*) FROM exceptions e WHERE e.run_id = r.run_id) AS exception_count
FROM runs r
JOIN payments p ON p.run_id = r.run_id
GROUP BY r.run_id;

CREATE VIEW IF NOT EXISTS v_blocked_actions AS
SELECT pc.rule_id, pc.rule_desc, COUNT(*) AS times_denied
FROM policy_checks pc
WHERE pc.result = 'deny'
GROUP BY pc.rule_id
ORDER BY times_denied DESC;
