"""Generates the synthetic payment-failure dataset into SQLite.

Three things live here: the seeded batch of failed payments, the observable
issuer-health signal the agent reads for G4, and resolve() -- the ground-truth
oracle. latent_json is simulator-only. No function here returns it to a caller
that is not resolve(); the agent must never see it.

All timestamps are IST with an explicit +05:30 offset. _iso() refuses to emit
a naive datetime, because a naive timestamp compared against a B3 quiet-hours
boundary is wrong by 5h30m and looks fine until a judge checks it.
"""

import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 1, 26, 0, 0, tzinfo=IST)
FEB1 = datetime(2026, 2, 1, 0, 0, tzinfo=IST)
WINDOW_HOURS = 120          # Jan 26 -> Jan 31, so failures land on days 25-31
B4_HOURS = 72               # health snapshots must outlast the B4 tail
BIG_TICKET_PAISE = 2_500_000
FLOOR_PAISE = 19_900

CFG = {
    "methods": [("card", .35), ("upi_collect", .20), ("upi_intent", .15),
                ("netbanking", .15), ("wallet", .05), ("mandate", .10)],
    "issuers": ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PAYTM"],
    "causes": [("TRANSIENT_GATEWAY", .22), ("ISSUER_DOWNTIME", .16),
               ("INSUFFICIENT_FUNDS", .22), ("AUTH_TIMEOUT", .18),
               ("INSTRUMENT_DEAD", .12), ("RISK_DECLINE", .06),
               ("UNKNOWN", .04)],
    "codes": {
        "TRANSIENT_GATEWAY": ["GATEWAY_ERROR", "payment_failed"],
        "ISSUER_DOWNTIME": ["issuer_down", "GATEWAY_ERROR"],
        "INSUFFICIENT_FUNDS": ["insufficient_funds"],
        "AUTH_TIMEOUT": ["authentication_failed", "upi_collect_expired"],
        "INSTRUMENT_DEAD": ["card_expired", "mandate_revoked"],
        "RISK_DECLINE": ["risk_declined"],
        "UNKNOWN": ["UNMAPPED_ERR_71", "UNMAPPED_ERR_92", "BAD_REQUEST_ERROR",
                    "UNMAPPED_PSP_X4"],
    },
    "confusion_rate": .15, "never_returns_rate": .12,
    "big_tickets": 6, "dark_pairs": 2, "n_customers": 150,
}
UNMAPPED = set(CFG["codes"]["UNKNOWN"])
HARD = ("INSTRUMENT_DEAD", "RISK_DECLINE", "UNKNOWN")


def _iso(dt):
    assert dt.tzinfo is not None, f"naive timestamp refused: {dt!r}"
    return dt.isoformat()


def to_ist(dt):
    """Normalize an aware datetime to IST. Only the B3 quiet-hours check needs
    this -- every other comparison is between instants and is offset-safe."""
    assert dt.tzinfo is not None, f"naive timestamp refused: {dt!r}"
    return dt.astimezone(IST)


def _pick(rng, weighted):
    r, acc = rng.random(), 0.0
    for value, weight in weighted:
        acc += weight
        if r < acc:
            return value
    return weighted[-1][0]


def generate_batch(seed, n=200):
    """Seeded batch of failed payments. Keys match the payments table minus
    run_id and the outcome columns, which the runner stamps at insert."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        cause = _pick(rng, CFG["causes"])
        method = _pick(rng, CFG["methods"])
        failed_at = T0 + timedelta(minutes=rng.randrange(WINDOW_HOURS * 60))
        code = rng.choice(CFG["codes"][cause])
        if rng.random() < CFG["confusion_rate"]:      # visible code misleads
            other = rng.choice([c for c in CFG["codes"] if c != cause])
            code = rng.choice(CFG["codes"][other])
        latent = {
            "true_cause": cause,
            "recoverable": cause not in HARD
                           and rng.random() > CFG["never_returns_rate"],
            "issuer_down_from": None, "issuer_recovers_at": None,
            "funds_available_at": None,
            "customer_intent_decay": round(rng.uniform(8, 48), 1),
            "responds_to_nudge": rng.random() < .45,
            "responds_to_upi_switch": rng.random() < .55,
            "base_success_prob": round(rng.uniform(.35, .90), 3),
        }
        if cause == "ISSUER_DOWNTIME":
            latent["issuer_down_from"] = _iso(
                failed_at - timedelta(minutes=rng.randrange(30, 120)))
            latent["issuer_recovers_at"] = _iso(
                failed_at + timedelta(minutes=rng.randrange(45, 600)))
        if cause == "INSUFFICIENT_FUNDS":             # some land beyond B4
            latent["funds_available_at"] = _iso(
                failed_at + timedelta(hours=rng.uniform(2, 96)))
        out.append({
            "payment_id": f"pay_{seed}_{i:04d}",
            "customer_id": f"cust_{rng.randrange(CFG['n_customers']):04d}",
            "amount_paise": max(FLOOR_PAISE, int(rng.lognormvariate(12.388, .55))),
            "method": method,
            "issuer": rng.choice(CFG["issuers"]),
            "is_subscription": 1 if method == "mandate" else 0,
            "error_code": code,
            "error_reason": f"simulated {code}",
            "failed_at": _iso(failed_at),
            "has_upi_handle": 1 if rng.random() < .60 else 0,
            "comms_opt_out": 1 if rng.random() < .08 else 0,
            "latent_json": json.dumps(latent, sort_keys=True),
        })
    for p in rng.sample(out, CFG["big_tickets"]):     # guarantee B5 fires
        p["amount_paise"] = BIG_TICKET_PAISE + rng.randrange(1, 4_000_00)
    return out


def _downtime_windows(payments):
    """Per (issuer, method) downtime span, derived from the same ground truth
    the payments were generated against, so the signal cannot contradict them."""
    windows = {}
    for p in payments:
        latent = json.loads(p["latent_json"])
        if latent["true_cause"] != "ISSUER_DOWNTIME":
            continue
        pair = (p["issuer"], p["method"])
        span = (datetime.fromisoformat(latent["issuer_down_from"]),
                datetime.fromisoformat(latent["issuer_recovers_at"]))
        cur = windows.get(pair)
        windows[pair] = span if cur is None else (min(cur[0], span[0]),
                                                  max(cur[1], span[1]))
    return windows


def dark_pairs(payments):
    """Issuer/method pairs deliberately left with no snapshot, so G4's
    no_health_signal fallback is exercised. Chosen from pairs that DO have
    downtime -- a fallback that only fires on healthy pairs proves nothing."""
    return sorted(_downtime_windows(payments))[:CFG["dark_pairs"]]


def _estimate(rng, recovers_at):
    """+-25 min jitter, skewed EARLY ~40% of the time while an issuer is down:
    an agent that trusts the estimate retries too soon and burns a B1 attempt."""
    minutes = -rng.randrange(5, 26) if rng.random() < .40 else rng.randrange(0, 26)
    return _iso(recovers_at + timedelta(minutes=minutes))


def generate_issuer_health(seed, payments):
    """Hourly snapshots per issuer/method, T0 through T0+192h (Feb 3 00:00),
    covering the 120h batch window plus the 72h B4 tail."""
    rng = random.Random(seed + 1)
    windows, dark = _downtime_windows(payments), set(dark_pairs(payments))
    end_at = T0 + timedelta(hours=WINDOW_HOURS + B4_HOURS)
    rows = []
    for issuer, method in sorted({(p["issuer"], p["method"]) for p in payments}):
        if (issuer, method) in dark:
            continue
        win, t = windows.get((issuer, method)), T0
        while t <= end_at:
            status, est = "healthy", None
            if win and win[0] <= t < win[1]:
                status, est = "down", _estimate(rng, win[1])
            elif win and (win[0] - timedelta(hours=1) <= t < win[0]
                          or win[1] <= t < win[1] + timedelta(hours=1)):
                status = "degraded"
            rate = {"healthy": rng.uniform(.90, .99),
                    "degraded": rng.uniform(.40, .70),
                    "down": rng.uniform(.00, .05)}[status]
            rows.append({"run_id": None, "issuer": issuer, "method": method,
                         "observed_at": _iso(t), "status": status,
                         "success_rate": round(rate, 4),
                         "estimated_recovery_at": est})
            t += timedelta(hours=1)
    return rows


def resolve(payment, action, at_time, attempt_no):
    """Ground-truth oracle. Deterministic in (payment, action, minute, attempt)
    so the do_nothing / naive_retry / agent arms stay comparable."""
    assert at_time.tzinfo is not None, f"naive at_time refused: {at_time!r}"
    if action in ("ESCALATE_HUMAN", "STOP"):
        return {"success": False, "reason": f"{action} moves no money"}
    latent = json.loads(payment["latent_json"])
    minute = at_time.replace(second=0, microsecond=0)
    key = "|".join([payment["payment_id"], action, _iso(minute), str(attempt_no)])
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))

    cause = latent["true_cause"]
    if cause in ("INSTRUMENT_DEAD", "RISK_DECLINE"):
        return {"success": False, "reason": f"{cause}: no action can recover this"}
    if not latent["recoverable"]:
        return {"success": False, "reason": "customer never returns"}

    if action == "NUDGE_CUSTOMER":
        if payment["comms_opt_out"]:
            return {"success": False, "reason": "customer opted out of comms"}
        if not latent["responds_to_nudge"]:
            return {"success": False, "reason": "customer ignored the nudge"}
    elif action == "SWITCH_RAIL":
        if not payment["has_upi_handle"]:
            return {"success": False, "reason": "no UPI handle on file"}
        if not latent["responds_to_upi_switch"]:
            return {"success": False, "reason": "customer did not use the UPI link"}
    elif action in ("RETRY_NOW", "RETRY_SCHEDULED"):
        for field, label in (("issuer_recovers_at", "issuer still down"),
                             ("funds_available_at", "funds still short")):
            gate = latent[field]
            if gate and at_time < datetime.fromisoformat(gate):
                return {"success": False, "reason": f"retried too early: {label}"}
    else:
        return {"success": False, "reason": f"unknown action {action}"}

    hours = (at_time - datetime.fromisoformat(payment["failed_at"])).total_seconds() / 3600
    decay = 0.5 ** (hours / latent["customer_intent_decay"])
    prob = latent["base_success_prob"] * decay
    ok = rng.random() < prob
    return {"success": ok, "reason": ("recovered" if ok else "attempt failed")
            + f" (p={prob:.3f}, intent decay {decay:.2f})"}
