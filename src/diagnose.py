"""Classifies why a payment failed.

Rules-only by default. Every submitted metric must reproduce with no API key
and no network, so `anthropic` is never imported at module level -- the import
lives inside _diagnose_llm(), behind use_llm=False. C4: if the LLM is absent,
unavailable, or returns malformed output, the rules answer stands and
diagnosis_source is logged as 'fallback'. A missing LLM never moves money.

The visible error_code is not ground truth: the simulator emits a misleading
code for ~17% of payments, and rules cannot see through that. That ceiling is
the honest case for adding an LLM later, not something to paper over.
"""

PROMPT_VERSION = "diagnose-v1"

# error_code -> (root_cause, confidence). Unlisted codes fall through to
# UNKNOWN, which is what routes them to G3 escalation.
CODE_MAP = {
    "issuer_down":           ("ISSUER_DOWNTIME", 0.92),
    "insufficient_funds":    ("INSUFFICIENT_FUNDS", 0.95),
    "authentication_failed": ("AUTH_TIMEOUT", 0.84),
    "upi_collect_expired":   ("AUTH_TIMEOUT", 0.93),
    "card_expired":          ("INSTRUMENT_DEAD", 0.96),
    "mandate_revoked":       ("INSTRUMENT_DEAD", 0.94),
    "risk_declined":         ("RISK_DECLINE", 0.90),
    "GATEWAY_ERROR":         ("TRANSIENT_GATEWAY", 0.55),
    "payment_failed":        ("TRANSIENT_GATEWAY", 0.50),
}

# Codes that mean "something broke upstream" without saying what. These are the
# only ones the issuer-health signal is allowed to reinterpret.
AMBIGUOUS = ("GATEWAY_ERROR", "payment_failed")

# Rules-only prior on recovering the money at all, before policy narrows it.
CAUSE_PRIOR = {
    "TRANSIENT_GATEWAY": 0.70, "ISSUER_DOWNTIME": 0.62,
    "INSUFFICIENT_FUNDS": 0.45, "AUTH_TIMEOUT": 0.38,
    "INSTRUMENT_DEAD": 0.05, "RISK_DECLINE": 0.02, "UNKNOWN": 0.10,
}

# The agent sees these fields and no others. latent_json is absent by
# construction, not by discipline.
VISIBLE_FIELDS = ("payment_id", "customer_id", "amount_paise", "method",
                  "issuer", "is_subscription", "error_code", "error_reason",
                  "failed_at", "has_upi_handle", "comms_opt_out")


def visible(payment):
    """Strip the payment down to what the agent is allowed to know."""
    return {k: payment[k] for k in VISIBLE_FIELDS if k in payment}


def classify(pay, health=None):
    """error_code -> (root_cause, confidence), with two observable overrides."""
    code = pay["error_code"]
    cause, confidence = CODE_MAP.get(code, ("UNKNOWN", 0.30))

    # M2: a revoked mandate on a subscription is a dead instrument, whatever
    # the generic code says.
    if pay["is_subscription"] and code == "mandate_revoked":
        return "INSTRUMENT_DEAD", 0.97

    # An ambiguous upstream error at an issuer whose published health says
    # 'down' is downtime, not a blip. This is observable, not latent.
    if code in AMBIGUOUS and health and health.get("status") == "down":
        return "ISSUER_DOWNTIME", 0.88
    if code in AMBIGUOUS and health and health.get("status") == "degraded":
        return "TRANSIENT_GATEWAY", 0.66
    return cause, confidence


def score(pay, cause, now):
    """Rank-only prior. Never gates money -- that is the policy engine's job."""
    from datetime import datetime
    prior = CAUSE_PRIOR.get(cause, 0.10)
    hours = (now - datetime.fromisoformat(pay["failed_at"])).total_seconds() / 3600
    prior *= 0.5 ** (hours / 36.0)              # staleness costs, like intent
    if pay["comms_opt_out"] and cause in ("AUTH_TIMEOUT", "INSTRUMENT_DEAD"):
        prior *= 0.35                            # customer action needed, no channel
    if pay["has_upi_handle"] and pay["method"] in ("card", "netbanking"):
        prior *= 1.20                            # SWITCH_RAIL is available (G8)
    if pay["amount_paise"] > 2_500_000:
        prior *= 0.80                            # B5 routes these to a human
    return round(min(prior, 0.99), 4)


def _diagnose_llm(pay, rules_cause, rules_conf):
    """UNTESTED: no API key is present and the flag is off. Import is local on
    purpose so the module loads without the SDK."""
    import json
    import os

    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = (
        "You are classifying a failed Indian payment into exactly one of: "
        "TRANSIENT_GATEWAY, ISSUER_DOWNTIME, INSUFFICIENT_FUNDS, AUTH_TIMEOUT, "
        "INSTRUMENT_DEAD, RISK_DECLINE, UNKNOWN. The gateway error code is "
        "often misleading. Reply with JSON only: "
        '{"root_cause": "...", "confidence": 0.0, "rationale": "..."}\n\n'
        f"Payment: {json.dumps(pay, sort_keys=True)}\n"
        f"Rules-based guess: {rules_cause} (confidence {rules_conf})"
    )
    reply = client.messages.create(
        model="claude-opus-5", max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    parsed = json.loads(reply.content[0].text)
    if parsed["root_cause"] not in CAUSE_PRIOR:
        raise ValueError(f"model returned unknown class {parsed['root_cause']!r}")
    return parsed["root_cause"], float(parsed["confidence"]), parsed.get("rationale", "")


def diagnose(payment, now, health=None, use_llm=False, llm_threshold=0.70):
    """Returns the decisions-table fields for one payment. Rules-only unless
    use_llm is on AND rules confidence is below the threshold."""
    pay = visible(payment)
    cause, confidence = classify(pay, health)
    source, model, rationale = "rules", None, f"error_code={pay['error_code']}"

    if use_llm and confidence < llm_threshold:
        try:
            cause, confidence, rationale = _diagnose_llm(pay, cause, confidence)
            source, model = "llm", "claude-opus-5"
        except Exception as exc:                 # C4: never blocks money
            source = "fallback"
            rationale = f"llm unavailable ({type(exc).__name__}), rules stand"

    return {
        "root_cause": cause,
        "confidence": round(confidence, 4),
        "success_score": score(pay, cause, now),
        "diagnosis_source": source,
        "model_version": model,
        "prompt_version": PROMPT_VERSION,
        "rationale": rationale,
    }
