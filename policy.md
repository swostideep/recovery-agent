# Recovery Policy — v1.2

Merchant: "Kettle & Co", D2C ecommerce (India). AOV ~₹2,400.
Actor: automated recovery agent. All amounts in paise (integers).

The agent PROPOSES an action. This policy DECIDES whether it executes.
Every check below logs its rule ID to the audit trail with allow/deny + reason.

---

## 1. Root-cause classes

Failures are classified into exactly one class before any action is considered.

| Class | Meaning | Recoverable |
|---|---|---|
| TRANSIENT_GATEWAY | timeout, 5xx, network blip | yes |
| ISSUER_DOWNTIME | bank/PSP unavailable | yes, later |
| INSUFFICIENT_FUNDS | balance/limit shortfall | yes, later |
| AUTH_TIMEOUT | OTP not entered, UPI collect expired | yes, needs customer |
| INSTRUMENT_DEAD | expired/invalid card, revoked mandate | no |
| RISK_DECLINE | issuer or risk-engine rejection | no (auto) |
| UNKNOWN | unmapped code | no (auto) |

## 2. Action set (closed — nothing outside this list may execute)

- `RETRY_NOW` — re-attempt on the same rail immediately
- `RETRY_SCHEDULED(at)` — re-attempt on the same rail at a future time
- `SWITCH_RAIL` — issue a UPI intent link for a card/netbanking failure
- `NUDGE_CUSTOMER` — one message with a payment link
- `ESCALATE_HUMAN` — queue for merchant review, no money action
- `STOP` — mark terminal: no further money movement and no further decision
  cycles. STOP does not bar a nudge already authorized in the same decision
  cycle (see G1). STOP governs money, not communication.

## 3. Precedence (P)

- **P1** Bounded limits (B) and stopping rules (S) are absolute. Gating rules
  (G) and heuristics may only narrow what B and S permit, never widen it.
- **P2** Where a heuristic proposes a time outside a bound, the action is
  clamped to the latest feasible time inside the bound. If no feasible time
  exists, the payment goes terminal with `expired`.
- **P3** Every clamp or precedence resolution is logged as its own
  policy_checks row with rule_id `P2` and both rule IDs named in the reason.
- **P4** The policy engine returns exactly one of `allow` | `deny` | `clamp`.
  It may NOT substitute the action type. `clamp` narrows a parameter of the
  proposed action only (the P2 time clamp), never changes what the action is.
  On deny, the agent may re-propose: max 2 re-proposals per attempt, then
  ESCALATE_HUMAN. Every proposal is its own decisions row. The ctx passed to
  the engine strips latent state and is passed explicitly.

## 4. Bounded limits (B)

- **B1** Max 3 recovery attempts per payment, lifetime.
- **B2** Max 1 customer-facing nudge per customer per rolling 24h.
- **B3** No action executes 21:00–09:00 IST. Defer to 09:05 IST.
- **B4** No attempt more than 72h after original failure. After 72h → STOP.
- **B5** Amount > ₹25,000 → propose only, ESCALATE_HUMAN. Never auto-execute.
- **B6** Max actions per run, configurable, default 1000. Exceeding this
  halts the run.

## 5. Gating by class (G)

- **G1** INSTRUMENT_DEAD → no further payment attempt, ever. Exactly one
  instrument-update nudge is permitted, and it must be issued in the same
  decision cycle as the classification, subject to B2, B3 and S5. The payment
  then goes terminal with `unrecoverable`.
  Ordering is fixed: classify → issue permitted nudge (or log why it was
  denied) → mark terminal. A terminal payment can never be reopened by the
  agent. If the nudge is denied, the `deny` row is still written: silence
  with no record is the thing this policy exists to prevent.
- **G2** RISK_DECLINE → never auto-retry. ESCALATE_HUMAN. Retrying a risk
  decline is abuse of the issuer relationship and is prohibited.
- **G3** UNKNOWN → ESCALATE_HUMAN. The agent does not guess with money.
- **G4** ISSUER_DOWNTIME → RETRY_NOW denied. Only RETRY_SCHEDULED, no earlier
  than `issuer_health.estimated_recovery_at + 15 min`, read from the most
  recent snapshot at decision time. This estimate is observational and may be
  wrong; a retry that fails because the estimate was optimistic consumes an
  attempt under B1 like any other. If no snapshot exists for that
  issuer/method, fall back to a fixed 90-minute delay and log
  `rule_id=G4, reason=no_health_signal`.
- **G5** INSUFFICIENT_FUNDS → min 6h gap before retry; second retry needs a
  further 18h gap. Salary-cycle heuristic: if failure falls on days 25–31,
  prefer scheduling to the 1st or 2nd of the next month — but only if that
  time falls within the B4 72h window (P2). If it does not, schedule at the
  latest B4-feasible time instead and log the clamp.
- **G6** AUTH_TIMEOUT → no silent retry. Customer must act, so NUDGE_CUSTOMER
  or SWITCH_RAIL only.
- **G7** TRANSIENT_GATEWAY → RETRY_NOW allowed, max 2 times, exponential
  backoff 2 min then 15 min.
- **G8** SWITCH_RAIL is permitted only from card/netbanking to UPI, never the
  reverse, and only if the customer has a UPI handle on file.

## 6. Subscription / mandate rules (M)

- **M1** Mandate debits follow the same B and G rules.
- **M2** Revoked or expired mandate → INSTRUMENT_DEAD → STOP + nudge to
  re-authorize.
- **M3** Mandate retries respect a minimum 24h spacing regardless of class.
- **M4** Never retry a mandate debit for a higher amount than the original.

## 7. Stopping rules (S)

A payment becomes terminal when any of these is true:

- **S1** Recovered successfully.
- **S2** 3 attempts exhausted (B1).
- **S3** 72h elapsed (B4).
- **S4** Classified INSTRUMENT_DEAD, RISK_DECLINE, or UNKNOWN, after any
  G1-permitted nudge has been issued or denied. "Terminal" bars money actions
  and further decision cycles; it does not retroactively cancel a nudge
  already authorized in the same cycle.
- **S5** Customer opted out of communication.
- **S6** Escalated to human — the agent's authority ends there.

## 8. Circuit breaker & degradation (C)

- **C1** 3 consecutive adapter 5xx → trip breaker, stop all execution.
- **C2** On trip, in-flight payments move to the dead-letter queue and appear
  in the exception list with reason `ADAPTER_UNAVAILABLE`. No silent retries.
- **C3** Breaker resets only on manual reset. It does not self-heal mid-run.
- **C4** If LLM diagnosis is unavailable or returns malformed output, fall back
  to rules-only classification and log `diagnosis_source=fallback`. A missing
  LLM never blocks or unblocks a money action.

## 9. Audit requirements (A)

- **A1** Every decision writes: inputs, root cause, confidence, score,
  proposed action, every policy check with rule ID and result, action taken,
  outcome, amount recovered.
- **A2** The audit log is append-only, enforced by SQL triggers, and
  hash-chained row to row.
- **A3** Every executed action must be traceable to a decision, and every
  decision to a payment. No orphan money actions. Enforced by foreign keys,
  which require `PRAGMA foreign_keys = ON` on every connection, and re-checked
  by `verify_integrity()` at the end of every run.

## 10. Out of scope (deliberate)

No real messages are sent. No real funds move. Nudge content is generated and
logged, not delivered. Live rail integration is behind `adapter.py`, which
exposes real Razorpay method signatures against a mock.
