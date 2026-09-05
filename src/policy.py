"""Decides whether a proposed action may execute. Rules transcribed from
policy.md v1.2; every rule_id here matches a rule ID there, so grepping G4 in
this file lands on the paragraph a judge just read.

Contract (P4): evaluate() returns exactly one of allow | deny | clamp. It never
substitutes the action type -- clamp narrows the `at` parameter only. On deny
the agent may re-propose (max 2, then ESCALATE_HUMAN); each proposal is its own
decisions row. The engine is pure: it writes nothing and returns the
policy_checks rows for runner.py to persist.

Only rules that actually evaluated are logged. A rule that does not apply to
this action or class returns None and produces no row, so policy_checks stays
readable instead of burying real denials under thousands of no-ops.
"""

from datetime import timedelta

from diagnose import visible
from simulator import to_ist

B1_MAX_ATTEMPTS = 3
B2_NUDGE_PER_24H = 1
B4_WINDOW_HOURS = 72
B5_ESCALATE_ABOVE_PAISE = 2_500_000
B6_DEFAULT_LIMIT = 1000
G4_NO_SIGNAL_DELAY_MIN = 90
G4_RECOVERY_BUFFER_MIN = 15
G5_FIRST_GAP_H, G5_SECOND_GAP_H = 6, 18
G7_BACKOFF_MIN = (2, 15)
M3_MANDATE_GAP_H = 24
P4_MAX_REPROPOSALS = 2

ATTEMPT_ACTIONS = ("RETRY_NOW", "RETRY_SCHEDULED", "SWITCH_RAIL")
MONEY_ACTIONS = ATTEMPT_ACTIONS + ("NUDGE_CUSTOMER",)
ALL_ACTIONS = MONEY_ACTIONS + ("ESCALATE_HUMAN", "STOP")
TERMINAL_RULE = {"recovered": "S1", "exhausted": "S2", "expired": "S3",
                 "unrecoverable": "S4", "escalated": "S6", "dead_letter": "C2"}


def build_ctx(payment, action, root_cause, now, params=None, attempts_used=0,
              transient_retries=0, nudges_24h=0, actions_this_run=0, health=None,
              last_attempt_at=None, terminal_status=None, proposal_no=0,
              b6_limit=B6_DEFAULT_LIMIT):
    """Engine input. Built from visible() so latent_json is structurally absent
    (P4), not merely untouched by convention."""
    assert action in ALL_ACTIONS, f"action outside the closed set: {action}"
    return {
        "pay": visible(payment), "action": action, "root_cause": root_cause,
        "now": now, "params": dict(params or {}), "attempts_used": attempts_used,
        "transient_retries": transient_retries, "nudges_24h": nudges_24h,
        "actions_this_run": actions_this_run, "health": health,
        "last_attempt_at": last_attempt_at, "terminal_status": terminal_status,
        "proposal_no": proposal_no, "b6_limit": b6_limit,
    }


# --- bounded limits (B) -- absolute, may not be widened by any G rule (P1) ---

def b1_attempt_cap(ctx):
    if ctx["action"] not in ATTEMPT_ACTIONS:
        return None
    if ctx["attempts_used"] >= B1_MAX_ATTEMPTS:
        return "deny", f"{ctx['attempts_used']} attempts used, lifetime cap is {B1_MAX_ATTEMPTS}"
    return "allow", f"attempt {ctx['attempts_used'] + 1} of {B1_MAX_ATTEMPTS}"


def b2_nudge_cap(ctx):
    if ctx["action"] != "NUDGE_CUSTOMER":
        return None
    if ctx["nudges_24h"] >= B2_NUDGE_PER_24H:
        return "deny", f"customer already nudged {ctx['nudges_24h']}x in rolling 24h"
    return "allow", "no nudge to this customer in the last 24h"


def b5_big_ticket(ctx):
    amount = ctx["pay"]["amount_paise"]
    if amount <= B5_ESCALATE_ABOVE_PAISE:
        return None
    if ctx["action"] == "ESCALATE_HUMAN":
        return "allow", f"Rs {amount/100:,.0f} above auto-execute ceiling, escalating"
    return "deny", f"Rs {amount/100:,.0f} exceeds Rs 25,000: propose only, never auto-execute"


def b6_run_cap(ctx):
    if ctx["action"] not in MONEY_ACTIONS:
        return None
    if ctx["actions_this_run"] >= ctx["b6_limit"]:
        return "deny", f"run cap {ctx['b6_limit']} reached, halting"
    return "allow", f"action {ctx['actions_this_run'] + 1} of {ctx['b6_limit']}"


# --- gating by class (G) ---

def g1_instrument_dead(ctx):
    if ctx["root_cause"] != "INSTRUMENT_DEAD":
        return None
    if ctx["action"] in ATTEMPT_ACTIONS:
        return "deny", "dead instrument: no payment attempt can ever succeed"
    if ctx["action"] == "NUDGE_CUSTOMER":
        return "allow", "one instrument-update nudge permitted before terminal"
    return "allow", "non-money action permitted"


def g2_risk_decline(ctx):
    if ctx["root_cause"] != "RISK_DECLINE":
        return None
    if ctx["action"] in MONEY_ACTIONS:
        return "deny", "retrying a risk decline abuses the issuer relationship"
    return "allow", "escalating to human review"


def g3_unknown(ctx):
    if ctx["root_cause"] != "UNKNOWN":
        return None
    if ctx["action"] in MONEY_ACTIONS:
        return "deny", "unmapped error code: the agent does not guess with money"
    return "allow", "escalating to human review"


def g4_issuer_downtime(ctx):
    if ctx["root_cause"] != "ISSUER_DOWNTIME":
        return None
    if ctx["action"] == "RETRY_NOW":
        return "deny", "issuer is down: only a scheduled retry is permitted"
    return "allow", "scheduled retry permitted subject to the health signal"


def g6_auth_timeout(ctx):
    if ctx["root_cause"] != "AUTH_TIMEOUT":
        return None
    if ctx["action"] in ("RETRY_NOW", "RETRY_SCHEDULED"):
        return "deny", "customer must act: no silent retry after an auth timeout"
    return "allow", "customer-facing action permitted"


def g7_transient_cap(ctx):
    if ctx["root_cause"] != "TRANSIENT_GATEWAY" or ctx["action"] != "RETRY_NOW":
        return None
    if ctx["transient_retries"] >= len(G7_BACKOFF_MIN):
        return "deny", f"{ctx['transient_retries']} immediate retries already, cap is 2"
    return "allow", f"immediate retry {ctx['transient_retries'] + 1} of 2"


def g8_switch_rail(ctx):
    if ctx["action"] != "SWITCH_RAIL":
        return None
    if ctx["pay"]["method"] not in ("card", "netbanking"):
        return "deny", f"rail switch only from card/netbanking, not {ctx['pay']['method']}"
    if not ctx["pay"]["has_upi_handle"]:
        return "deny", "no UPI handle on file"
    return "allow", "card/netbanking to UPI with a handle on file"


# --- mandates (M) and opt-out (S5) ---

def m4_mandate_amount(ctx):
    if not ctx["pay"]["is_subscription"] or ctx["action"] not in ATTEMPT_ACTIONS:
        return None
    proposed = ctx["params"].get("amount_paise", ctx["pay"]["amount_paise"])
    if proposed > ctx["pay"]["amount_paise"]:
        return "deny", f"mandate retry {proposed} exceeds original {ctx['pay']['amount_paise']}"
    return "allow", "mandate retry at or below the original amount"


def s5_opted_out(ctx):
    if ctx["action"] != "NUDGE_CUSTOMER":
        return None
    if ctx["pay"]["comms_opt_out"]:
        return "deny", "customer opted out of communication"
    return "allow", "customer reachable"


RULES = [
    ("B1", "Max 3 recovery attempts per payment, lifetime", b1_attempt_cap),
    ("B2", "Max 1 customer nudge per customer per rolling 24h", b2_nudge_cap),
    ("B5", "Above Rs 25,000 propose only, never auto-execute", b5_big_ticket),
    ("B6", "Max actions per run", b6_run_cap),
    ("G1", "INSTRUMENT_DEAD: no attempt ever, one nudge permitted", g1_instrument_dead),
    ("G2", "RISK_DECLINE: never auto-retry", g2_risk_decline),
    ("G3", "UNKNOWN: the agent does not guess with money", g3_unknown),
    ("G4", "ISSUER_DOWNTIME: scheduled retry only", g4_issuer_downtime),
    ("G6", "AUTH_TIMEOUT: customer must act", g6_auth_timeout),
    ("G7", "TRANSIENT_GATEWAY: max 2 immediate retries", g7_transient_cap),
    ("G8", "SWITCH_RAIL only card/netbanking to UPI with a handle", g8_switch_rail),
    ("M4", "Never retry a mandate above the original amount", m4_mandate_amount),
    ("S5", "Customer opted out of communication", s5_opted_out),
]


# --- timing: earliest permitted moment, then the B3/B4 clamps ---

def _next_open(t):
    """B3: nothing executes 21:00-09:00 IST, deferred to 09:05."""
    ist = to_ist(t)
    if ist.hour >= 21:
        ist = (ist + timedelta(days=1)).replace(hour=9, minute=5, second=0, microsecond=0)
    elif ist.hour < 9:
        ist = ist.replace(hour=9, minute=5, second=0, microsecond=0)
    else:
        return t, None
    return ist, "deferred out of 21:00-09:00 IST quiet hours to 09:05"


def _salary_target(failed_at):
    """G5: failures on days 25-31 prefer the 1st or 2nd of the next month."""
    ist = to_ist(failed_at)
    if ist.day < 25:
        return None
    year, month = (ist.year + 1, 1) if ist.month == 12 else (ist.year, ist.month + 1)
    return ist.replace(year=year, month=month, day=1, hour=10, minute=0,
                       second=0, microsecond=0)


def earliest_permitted(ctx):
    """Returns (hard, preferred, rule, why).

    hard is the minimum the policy REQUIRES; preferred adds any heuristic on
    top. P2 clamps `preferred` back inside a bound, but if `hard` itself lies
    outside the bound then no feasible time exists and B4 denies. Collapsing
    these two was a bug: it denied every salary-cycle case instead of clamping.
    """
    from datetime import datetime
    now, last = ctx["now"], ctx["last_attempt_at"]
    hard, rule, why = now, None, None

    if ctx["root_cause"] == "ISSUER_DOWNTIME" and ctx["action"] == "RETRY_SCHEDULED":
        est = (ctx["health"] or {}).get("estimated_recovery_at")
        if est:
            hard = datetime.fromisoformat(est) + timedelta(minutes=G4_RECOVERY_BUFFER_MIN)
            why = f"health estimate {est} + {G4_RECOVERY_BUFFER_MIN}min buffer"
        else:
            hard = now + timedelta(minutes=G4_NO_SIGNAL_DELAY_MIN)
            why = "no_health_signal, fixed 90min delay"
        rule = "G4"

    elif ctx["root_cause"] == "INSUFFICIENT_FUNDS" and ctx["action"] in ATTEMPT_ACTIONS:
        gap = G5_SECOND_GAP_H if ctx["attempts_used"] >= 1 else G5_FIRST_GAP_H
        hard = (last or now) + timedelta(hours=gap)
        rule, why = "G5", f"{gap}h minimum gap for a funds shortfall"

    elif ctx["root_cause"] == "TRANSIENT_GATEWAY" and ctx["action"] == "RETRY_NOW":
        mins = G7_BACKOFF_MIN[min(ctx["transient_retries"], len(G7_BACKOFF_MIN) - 1)]
        hard = (last or now) + timedelta(minutes=mins)
        rule, why = "G7", f"exponential backoff {mins}min"

    if ctx["pay"]["is_subscription"] and ctx["action"] in ATTEMPT_ACTIONS and last:
        gap = last + timedelta(hours=M3_MANDATE_GAP_H)
        if gap > hard:
            hard, rule, why = gap, "M3", "mandates retry no oftener than 24h"

    hard = max(hard, now)
    preferred = hard
    if ctx["root_cause"] == "INSUFFICIENT_FUNDS" and ctx["action"] in ATTEMPT_ACTIONS:
        target = _salary_target(_failed_at(ctx))
        if target and target > preferred:
            preferred = target
            why = f"{why}; salary-cycle heuristic prefers {target.date()}"
    return hard, preferred, rule, why


def _latest_feasible(ceiling, hard):
    """P2: the latest moment at or before the B4 ceiling that B3 also permits.
    None when the bound and quiet hours leave nothing above `hard`."""
    ist = to_ist(ceiling)
    if 9 <= ist.hour < 21:
        candidate = ceiling
    else:
        day = ist if ist.hour >= 21 else ist - timedelta(days=1)
        candidate = day.replace(hour=20, minute=59, second=0, microsecond=0)
    return candidate if candidate >= hard else None


def _failed_at(ctx):
    from datetime import datetime
    return datetime.fromisoformat(ctx["pay"]["failed_at"])


def evaluate(ctx):
    """Returns (verdict, params, checks). verdict is allow | deny | clamp."""
    checks, ts = [], to_ist(ctx["now"]).isoformat()

    def log(rule_id, desc, result, reason):
        checks.append({"rule_id": rule_id, "rule_desc": desc, "result": result,
                       "reason": reason, "checked_at": ts})

    if ctx["terminal_status"]:
        rule = TERMINAL_RULE.get(ctx["terminal_status"], "S4")
        log(rule, "Terminal payments accept no further action", "deny",
            f"already terminal: {ctx['terminal_status']}")
        return "deny", ctx["params"], checks

    if ctx["proposal_no"] > P4_MAX_REPROPOSALS:
        log("P4", "Max 2 re-proposals per attempt", "deny",
            f"proposal {ctx['proposal_no']} exceeds the re-proposal cap")
        return "deny", ctx["params"], checks

    denied = False
    for rule_id, desc, fn in RULES:
        outcome = fn(ctx)
        if outcome is None:
            continue
        result, reason = outcome
        log(rule_id, desc, result, reason)
        if result == "deny":
            denied = True                       # no short-circuit: A1 wants them all

    if denied:
        return "deny", ctx["params"], checks
    if ctx["action"] not in ATTEMPT_ACTIONS:
        return "allow", ctx["params"], checks

    proposed = ctx["params"].get("at", ctx["now"])
    hard, preferred, floor_rule, why = earliest_permitted(ctx)
    ceiling = _failed_at(ctx) + timedelta(hours=B4_WINDOW_HOURS)
    final = max(proposed, preferred)
    if floor_rule and final > proposed:
        log(floor_rule, why or "timing floor", "allow",
            f"earliest permitted {final.isoformat()}")

    shifted, quiet_why = _next_open(final)
    if quiet_why:
        log("B3", "No action executes 21:00-09:00 IST", "allow", quiet_why)
        final = shifted

    if hard > ceiling:
        log("B4", "No attempt more than 72h after failure", "deny",
            f"hard minimum {hard.isoformat()} is past the 72h ceiling"
            f" {ceiling.isoformat()}: no feasible time remains")
        return "deny", ctx["params"], checks

    if final > ceiling:
        candidate = _latest_feasible(ceiling, hard)
        if candidate is None:
            log("B4", "No attempt more than 72h after failure", "deny",
                f"{floor_rule or 'proposal'} vs B4: nothing feasible inside the"
                f" bound at or after {hard.isoformat()}")
            return "deny", ctx["params"], checks
        log("P2", "Heuristic clamped to the latest time inside the bound",
            "allow", f"{floor_rule or 'proposal'} proposed {final.isoformat()},"
            f" outside the B4 ceiling {ceiling.isoformat()};"
            f" clamped to {candidate.isoformat()}")
        final = candidate

    params = dict(ctx["params"])
    params["at"] = final
    return ("clamp" if final != proposed else "allow"), params, checks
