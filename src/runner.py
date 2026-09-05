"""Entry point: runs the recovery loop over failed payments.

Three arms against one seeded batch and one oracle: do_nothing (the counter-
factual), naive_retry (fixed 1h/6h/24h, B1 only, ignores cause and health),
and agent (diagnose -> propose -> policy -> execute).

This module owns every database write. policy.py and adapter.py stay pure so
they can be tested without a database; the terminal-status transition lives
here alone so the engine and the runner can never disagree about it.
"""

import heapq
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bisect import bisect_right
from datetime import datetime, timedelta

import audit
import diagnose
import policy as pol
import simulator as sim
from adapter import AdapterError, CircuitOpenError, RazorpayAdapter

SEED, BATCH_N = 42, 200
MAX_CYCLES = 12                      # guard against a payment deferring forever
NAIVE_OFFSETS_H = (1, 6, 24)

# Ordered candidates per root cause. On a deny the agent takes the next one
# (P4: max 2 re-proposals, then ESCALATE_HUMAN).
CANDIDATES = {
    "TRANSIENT_GATEWAY":  ["RETRY_NOW", "RETRY_SCHEDULED", "ESCALATE_HUMAN"],
    "ISSUER_DOWNTIME":    ["RETRY_SCHEDULED", "NUDGE_CUSTOMER", "ESCALATE_HUMAN"],
    "INSUFFICIENT_FUNDS": ["RETRY_SCHEDULED", "NUDGE_CUSTOMER", "ESCALATE_HUMAN"],
    "AUTH_TIMEOUT":       ["SWITCH_RAIL", "NUDGE_CUSTOMER", "ESCALATE_HUMAN"],
    "INSTRUMENT_DEAD":    ["NUDGE_CUSTOMER", "STOP"],
    "RISK_DECLINE":       ["ESCALATE_HUMAN"],
    "UNKNOWN":            ["ESCALATE_HUMAN"],
}


def propose(cause, pay, rejected):
    """Pick the next untried candidate. Skips SWITCH_RAIL when G8 could never
    allow it, so a re-proposal is not wasted on a guaranteed denial."""
    for action in CANDIDATES.get(cause, ["ESCALATE_HUMAN"]):
        if action in rejected:
            continue
        if action == "SWITCH_RAIL" and not (
                pay["method"] in ("card", "netbanking") and pay["has_upi_handle"]):
            continue
        return action
    return "ESCALATE_HUMAN"


def health_index(rows):
    """(issuer, method) -> (sorted times, rows) for point-in-time lookup."""
    index = {}
    for row in rows:
        index.setdefault((row["issuer"], row["method"]), []).append(row)
    return {k: ([datetime.fromisoformat(r["observed_at"]) for r in v], v)
            for k, v in index.items()}


def snapshot_at(index, issuer, method, at):
    """Most recent snapshot at or before `at`, or None (G4 no_health_signal)."""
    entry = index.get((issuer, method))
    if not entry:
        return None
    times, rows = entry
    pos = bisect_right(times, at)
    return rows[pos - 1] if pos else None


def seed_run(conn, arm, payments, health):
    run_id = f"{arm}-{SEED}"
    conn.execute("INSERT INTO runs (run_id, arm, seed, started_at, policy_version)"
                 " VALUES (?,?,?,?,?)",
                 (run_id, arm, SEED, audit.now_iso(), "v1.2"))
    conn.executemany(
        "INSERT INTO payments (payment_id, run_id, customer_id, amount_paise,"
        " method, issuer, is_subscription, error_code, error_reason, failed_at,"
        " has_upi_handle, comms_opt_out, latent_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(p["payment_id"], run_id, p["customer_id"], p["amount_paise"], p["method"],
          p["issuer"], p["is_subscription"], p["error_code"], p["error_reason"],
          p["failed_at"], p["has_upi_handle"], p["comms_opt_out"], p["latent_json"])
         for p in payments])
    conn.executemany(
        "INSERT INTO issuer_health (run_id, issuer, method, observed_at, status,"
        " success_rate, estimated_recovery_at) VALUES (?,?,?,?,?,?,?)",
        [(run_id, h["issuer"], h["method"], h["observed_at"], h["status"],
          h["success_rate"], h["estimated_recovery_at"]) for h in health])
    conn.commit()
    return run_id


def finish(conn, run_id, state, payments):
    """Write terminal status back to payments. Only this function does it."""
    conn.executemany(
        "UPDATE payments SET terminal_status=?, recovered_paise=?, recovered_at=?,"
        " attempts_used=? WHERE run_id=? AND payment_id=?",
        [(state[p["payment_id"]]["status"], state[p["payment_id"]]["recovered"],
          state[p["payment_id"]]["recovered_at"], state[p["payment_id"]]["attempts"],
          run_id, p["payment_id"]) for p in payments])
    conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?",
                 (audit.now_iso(), run_id))
    conn.commit()


def new_state(payments):
    return {p["payment_id"]: {"status": None, "recovered": 0, "recovered_at": None,
                              "attempts": 0, "transient": 0, "last_at": None}
            for p in payments}


def write_decision(conn, run_id, pay, dx, action, params, attempt_no, checks):
    decision_id = uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO decisions (decision_id, run_id, payment_id, attempt_no,"
        " decided_at, root_cause, confidence, success_score, proposed_action,"
        " action_params_json, diagnosis_source, model_version, prompt_version)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, run_id, pay["payment_id"], attempt_no, audit.now_iso(),
         dx["root_cause"], dx["confidence"], dx["success_score"], action,
         json.dumps({k: str(v) for k, v in params.items()}, sort_keys=True),
         dx["diagnosis_source"], dx["model_version"], dx["prompt_version"]))
    conn.executemany(
        "INSERT INTO policy_checks (decision_id, rule_id, rule_desc, result,"
        " reason, checked_at) VALUES (?,?,?,?,?,?)",
        [(decision_id, c["rule_id"], c["rule_desc"], c["result"], c["reason"],
          c["checked_at"]) for c in checks])
    return decision_id


def write_action(conn, decision_id, envelope, scheduled_for=None, blocked_by=None):
    conn.execute(
        "INSERT INTO actions (action_id, decision_id, action_type, executed_at,"
        " scheduled_for, outcome, blocked_by_rule, adapter_latency_ms,"
        " adapter_response_json, amount_recovered_paise) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex[:16], decision_id, envelope["action_type"],
         envelope["executed_at"], scheduled_for, envelope["outcome"], blocked_by,
         envelope.get("adapter_latency_ms"), envelope.get("adapter_response_json"),
         envelope.get("amount_recovered_paise", 0)))


def add_exception(conn, run_id, payment_id, code, detail, amount, at):
    conn.execute(
        "INSERT INTO exceptions (run_id, payment_id, reason_code, detail,"
        " amount_paise, occurred_at) VALUES (?,?,?,?,?,?)",
        (run_id, payment_id, code, detail, amount, at))


def nudges_in_24h(log, customer_id, now):
    return sum(1 for t in log.get(customer_id, []) if now - t < timedelta(hours=24))


def run_do_nothing(conn, run_id, payments):
    """The counterfactual: no actions, so nothing is recovered."""
    state = new_state(payments)
    for p in payments:
        state[p["payment_id"]]["status"] = "expired"
    audit.append_event(conn, run_id, "RUN_COMPLETED",
                       {"arm": "do_nothing", "actions": 0})
    return state, 0


def run_naive_retry(conn, run_id, payments, adapter):
    """Fixed 1h/6h/24h retries. No diagnosis, no policy: writes decisions (A3
    needs every action to have a parent) but zero policy_checks, which is
    itself the evidence that this arm is ungated."""
    state = new_state(payments)
    actions = 0
    for p in payments:
        st = state[p["payment_id"]]
        failed_at = datetime.fromisoformat(p["failed_at"])
        for attempt, offset in enumerate(NAIVE_OFFSETS_H, start=1):
            at = failed_at + timedelta(hours=offset)
            dx = {"root_cause": "NOT_DIAGNOSED", "confidence": None,
                  "success_score": None, "diagnosis_source": "rules",
                  "model_version": None, "prompt_version": "naive-v1"}
            decision_id = write_decision(conn, run_id, p, dx, "RETRY_NOW",
                                         {"at": at}, attempt, [])
            env = adapter.payment.capture(p, "RETRY_NOW", at, attempt)
            write_action(conn, decision_id, env)
            actions += 1
            st["attempts"] = attempt
            if env["outcome"] == "success":
                st.update(status="recovered", recovered=env["amount_recovered_paise"],
                          recovered_at=at.isoformat())
                break
        if not st["status"]:
            st["status"] = "exhausted"
    conn.commit()
    audit.append_event(conn, run_id, "RUN_COMPLETED",
                       {"arm": "naive_retry", "actions": actions})
    return state, actions


def run_agent(conn, run_id, payments, index, adapter, b6_limit):
    """Time-ordered work queue: diagnose -> propose -> policy -> execute, with
    scheduled retries re-entering the queue at their approved time."""
    state, by_id = new_state(payments), {p["payment_id"]: p for p in payments}
    queue = [(datetime.fromisoformat(p["failed_at"]), i, p["payment_id"], None, None)
             for i, p in enumerate(payments)]
    heapq.heapify(queue)
    seq, nudges, actions, cycles, halted = len(queue), {}, 0, {}, False

    while queue:
        now, _, pid, pending, parent_id = heapq.heappop(queue)
        st, pay = state[pid], by_id[pid]
        if st["status"]:
            continue
        if halted:
            st["status"] = "dead_letter"
            add_exception(conn, run_id, pid, "ADAPTER_UNAVAILABLE",
                          "breaker tripped, no silent retries (C2)",
                          pay["amount_paise"], now.isoformat())
            continue
        cycles[pid] = cycles.get(pid, 0) + 1
        if cycles[pid] > MAX_CYCLES:
            st["status"] = "escalated"
            add_exception(conn, run_id, pid, "NEEDS_HUMAN_APPROVAL",
                          f"no resolution after {MAX_CYCLES} decision cycles",
                          pay["amount_paise"], now.isoformat())
            continue

        health = snapshot_at(index, pay["issuer"], pay["method"], now)
        dx = diagnose.diagnose(pay, now, health=health)
        vis = diagnose.visible(pay)

        # A retry that policy already approved for this moment executes without
        # re-deciding: re-running the timing floor would defer it forever.
        if pending:
            outcome = execute(conn, run_id, pay, st, pending, parent_id, now,
                              adapter, nudges, dx)
            actions += 1
            if outcome == "breaker":
                halted = True
            elif outcome == "retry" and not st["status"]:
                heapq.heappush(queue, (now + timedelta(minutes=1), seq, pid, None, None))
                seq += 1
            continue

        rejected, executed = [], False
        for proposal_no in range(pol.P4_MAX_REPROPOSALS + 1):
            action = propose(dx["root_cause"], vis, rejected)
            ctx = pol.build_ctx(
                pay, action, dx["root_cause"], now, params={"at": now},
                attempts_used=st["attempts"], transient_retries=st["transient"],
                nudges_24h=nudges_in_24h(nudges, pay["customer_id"], now),
                actions_this_run=actions, health=health,
                last_attempt_at=st["last_at"], terminal_status=st["status"],
                proposal_no=proposal_no, b6_limit=b6_limit)
            verdict, params, checks = pol.evaluate(ctx)
            decision_id = write_decision(conn, run_id, pay, dx, action, params,
                                         st["attempts"] + 1, checks)
            if verdict == "deny":
                blocked = next((c["rule_id"] for c in checks if c["result"] == "deny"), None)
                write_action(conn, decision_id,
                             {"action_type": action, "executed_at": now.isoformat(),
                              "outcome": "blocked", "adapter_latency_ms": None,
                              "adapter_response_json": None,
                              "amount_recovered_paise": 0}, blocked_by=blocked)
                audit.append_event(conn, run_id, "ACTION_BLOCKED",
                                   {"action": action, "rule": blocked},
                                   pid, decision_id)
                rejected.append(action)
                if blocked in ("B4", "S3"):
                    st["status"] = "expired"
                    break
                if blocked == "B6":
                    halted = True
                    break
                continue

            at = params.get("at", now)
            if at > now:                       # approved, but not yet due
                write_action(conn, decision_id,
                             {"action_type": action, "executed_at": now.isoformat(),
                              "outcome": "deferred", "adapter_latency_ms": None,
                              "adapter_response_json": None,
                              "amount_recovered_paise": 0}, scheduled_for=at.isoformat())
                heapq.heappush(queue, (at, seq, pid, action, decision_id))
                seq += 1
                executed = True
                break

            outcome = execute(conn, run_id, pay, st, action, decision_id, now,
                              adapter, nudges, dx)
            actions += 1
            executed = True
            if outcome == "breaker":
                halted = True
            elif outcome == "retry" and not st["status"]:
                heapq.heappush(queue, (now + timedelta(minutes=1), seq, pid, None, None))
                seq += 1
            break

        # G1 ordering: classify -> nudge (or log the denial) -> mark terminal.
        if dx["root_cause"] == "INSTRUMENT_DEAD" and not st["status"]:
            st["status"] = "unrecoverable"
        if not executed and not st["status"]:
            st["status"] = "escalated"
            add_exception(conn, run_id, pid, "NEEDS_HUMAN_APPROVAL",
                          f"all proposals denied: {', '.join(rejected)}",
                          pay["amount_paise"], now.isoformat())
    conn.commit()
    audit.append_event(conn, run_id, "RUN_COMPLETED",
                       {"arm": "agent", "actions": actions, "halted": halted})
    return state, actions


def execute(conn, run_id, pay, st, action, decision_id, now, adapter, nudges, dx):
    """Perform one authorized action. Returns done | retry | breaker."""
    pid, attempt = pay["payment_id"], st["attempts"] + 1
    stamp = now.isoformat()

    if action in ("ESCALATE_HUMAN", "STOP"):
        code = {"RISK_DECLINE": "RISK_DECLINE", "UNKNOWN": "UNKNOWN_ERROR_CODE"}.get(
            dx["root_cause"], "NEEDS_HUMAN_APPROVAL")
        if action == "ESCALATE_HUMAN":
            add_exception(conn, run_id, pid, code, dx["rationale"],
                          pay["amount_paise"], stamp)
            st["status"] = "escalated"
        else:
            st["status"] = "unrecoverable"
        write_action(conn, decision_id, {"action_type": action, "executed_at": stamp,
                                         "outcome": "success", "adapter_latency_ms": None,
                                         "adapter_response_json": None,
                                         "amount_recovered_paise": 0})
        audit.append_event(conn, run_id, "PAYMENT_TERMINAL",
                           {"action": action, "status": st["status"]}, pid, decision_id)
        return "done"

    try:
        if action in ("RETRY_NOW", "RETRY_SCHEDULED"):
            adapter.orders.create(pay, action, now, attempt)
            env = adapter.payment.capture(pay, action, now, attempt)
        else:
            env = adapter.payment_link.create(pay, action, now, attempt)
    except CircuitOpenError as exc:
        st["status"] = "dead_letter"
        add_exception(conn, run_id, pid, "ADAPTER_UNAVAILABLE", str(exc),
                      pay["amount_paise"], stamp)
        audit.append_event(conn, run_id, "BREAKER_TRIPPED", {"detail": str(exc)}, pid)
        return "breaker"
    except AdapterError as exc:
        write_action(conn, decision_id, {"action_type": action, "executed_at": stamp,
                                         "outcome": "error", "adapter_latency_ms": None,
                                         "adapter_response_json": json.dumps({"error": str(exc)}),
                                         "amount_recovered_paise": 0})
        return "retry"

    write_action(conn, decision_id, env)
    audit.append_event(conn, run_id, "ACTION_EXECUTED",
                       {"action": action, "outcome": env["outcome"],
                        "recovered": env["amount_recovered_paise"]}, pid, decision_id)
    if action in pol.ATTEMPT_ACTIONS:
        st["attempts"], st["last_at"] = attempt, now
    if action == "RETRY_NOW" and dx["root_cause"] == "TRANSIENT_GATEWAY":
        st["transient"] += 1
    if action == "NUDGE_CUSTOMER":
        nudges.setdefault(pay["customer_id"], []).append(now)

    if env["outcome"] == "success":
        st.update(status="recovered", recovered=env["amount_recovered_paise"],
                  recovered_at=stamp)
        audit.append_event(conn, run_id, "PAYMENT_TERMINAL",
                           {"status": "recovered"}, pid, decision_id)
        return "done"
    if st["attempts"] >= pol.B1_MAX_ATTEMPTS:
        st["status"] = "exhausted"
        return "done"
    return "retry"


def main(seed=SEED, n=BATCH_N, b6_limit=pol.B6_DEFAULT_LIMIT, db=audit.DB_PATH):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db + suffix):
            os.remove(db + suffix)

    payments = sim.generate_batch(seed, n)
    health = sim.generate_issuer_health(seed, payments)
    index = health_index(health)
    conn = audit.init_db(db)

    for arm in ("do_nothing", "naive_retry", "agent"):
        run_id = seed_run(conn, arm, payments, health)
        adapter = RazorpayAdapter(sim.resolve)
        if arm == "do_nothing":
            state, actions = run_do_nothing(conn, run_id, payments)
        elif arm == "naive_retry":
            state, actions = run_naive_retry(conn, run_id, payments, adapter)
        else:
            state, actions = run_agent(conn, run_id, payments, index, adapter, b6_limit)
        finish(conn, run_id, state, payments)
        print(f"  {arm:<12} {actions:>5} actions")

    problems = audit.verify_integrity(conn)
    print("\n=== integrity ===")
    print("  " + ("\n  ".join(problems) if problems else "audit chain intact, no orphans"))

    print("\n=== v_run_summary ===")
    cols = ("arm", "payments_at_risk", "value_at_risk_paise", "recovered_paise",
            "recovery_rate_pct", "payments_recovered", "total_attempts",
            "attempts_per_payment", "exception_count")
    print(f"  {'arm':<13}{'at risk':>9}{'recovered':>12}{'rate %':>9}"
          f"{'paid':>7}{'attempts':>10}{'exceptions':>12}")
    for row in conn.execute(f"SELECT {','.join(cols)} FROM v_run_summary"
                            " ORDER BY CASE arm WHEN 'do_nothing' THEN 1"
                            " WHEN 'naive_retry' THEN 2 ELSE 3 END"):
        print(f"  {row[0]:<13}{row[1]:>9}{row[3]/100:>12,.0f}{row[4] or 0:>9}"
              f"{row[5]:>7}{row[6]:>10}{row[8]:>12}")

    print("\n=== v_blocked_actions ===")
    for rule_id, desc, count in conn.execute("SELECT * FROM v_blocked_actions"):
        print(f"  {rule_id:<4}{count:>5}  {desc}")
    return conn


if __name__ == "__main__":
    args = dict(a.split("=") for a in sys.argv[1:] if "=" in a)
    main(seed=int(args.get("seed", SEED)), n=int(args.get("n", BATCH_N)),
         b6_limit=int(args.get("b6", pol.B6_DEFAULT_LIMIT)),
         db=args.get("db", audit.DB_PATH))
