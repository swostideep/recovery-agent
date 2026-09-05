# Recovery Policy — v1

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
- `STOP` — mark terminal, no further attempts

## 3. Bounded limits (B)

- **B1** Max 3 recovery attempts per payment, lifetime.
- **B2** Max 1 customer-facing nudge per customer per rolling 24h.
- **B3** No action executes 21:00–09:00 IST. Defer to 09:05 IST.
- **B4** No attempt more than 72h after original failure. After 72h → STOP.
- **B5** Amount > ₹25,000 → propose only, ESCALATE_HUMAN. Never auto-execute.
- **B6** Max 500 actions per run. Exceeding this halts the run.

## 4. Gating by class (G)

- **G1** INSTRUMENT_DEAD → STOP. Never retry. Nudge permitted once, to update
  the instrument, subject to B2.
- **G2** RISK_DECLINE → never auto-retry. ESCALATE_HUMAN. Retrying a risk
  decline is abuse of the issuer relationship and is prohibited.
- **G3** UNKNOWN → ESCALATE_HUMAN. The agent does not guess with money.
- **G4** ISSUER_DOWNTIME → RETRY_NOW is denied. Only RETRY_SCHEDULED, no
  earlier than issuer health recovery + 15 min buffer.
- **G5** INSUFFICIENT_FUNDS → minimum 6h gap before any retry. Second retry
  requires a further 18h gap. Salary-cycle heuristic: if failure is on days
  25–31, prefer scheduling to the 1st or 2nd.
- **G6** AUTH_TIMEOUT → no silent retry. Customer must act, so NUDGE_CUSTOMER
  or SWITCH_RAIL only.
- **G7** TRANSIENT_GATEWAY → RETRY_NOW allowed, max 2 times, exponential
  backoff 2 min then 15 min.
- **G8** SWITCH_RAIL is permitted only from card/netbanking to UPI, never the
  reverse, and only if the customer has a UPI handle on file.

## 5. Subscription / mandate rules (M)

- **M1** Mandate debits follow the same B and G rules.
- **M2** Revoked or expired mandate → INSTRUMENT_DEAD → STOP + nudge to
  re-authorize.
- **M3** Mandate retries respect a minimum 24h spacing regardless of class.
- **M4** Never retry a mandate debit for a higher amount than the original.

## 6. Stopping rules (S)

A payment becomes terminal when any of these is true:

- **S1** Recovered successfully.
- **S2** 3 attempts exhausted (B1).
- **S3** 72h elapsed (B4).
- **S4** Classified INSTRUMENT_DEAD, RISK_DECLINE, or UNKNOWN.
- **S5** Customer opted out of communication.
- **S6** Escalated to human — the agent's authority ends there.

## 7. Circuit breaker & degradation (C)

- **C1** 3 consecutive adapter 5xx → trip breaker, stop all execution.
- **C2** On trip, in-flight payments move to the dead-letter queue and appear
  in the exception list with reason `ADAPTER_UNAVAILABLE`. No silent retries.
- **C3** Breaker resets only on manual reset. It does not self-heal mid-run.
- **C4** If LLM diagnosis is unavailable or returns malformed output, fall back
  to rules-only classification and log `diagnosis_source=fallback`. A missing
  LLM never blocks or unblocks a money action.

## 8. Audit requirements (A)

- **A1** Every decision writes: inputs, root cause, confidence, score,
  proposed action, every policy check with rule ID and result, action taken,
  outcome, amount recovered.
- **A2** The audit log is append-only, enforced by SQL triggers, and
  hash-chained row to row.
- **A3** Every executed action must be traceable to a decision, and every
  decision to a payment. No orphan money actions.

## 9. Out of scope (deliberate)

No real messages are sent. No real funds move. Nudge content is generated and
logged, not delivered. Live rail integration is behind `adapter.py`, which
exposes real Razorpay method signatures against a mock.
