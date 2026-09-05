# Payment Failure Recovery Agent

An agent that diagnoses failed payments and recovers them **within a written
policy it cannot override**. Merchant: "Kettle & Co", D2C ecommerce, India.

```
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python3 src/runner.py          # all three arms, ~20s
python3 src/report.py          # writes results.json + dashboard.html
```

No API key, no network, no Docker. Every number below reproduces from `seed=42`.

## Bounded

The agent proposes; `policy.py` decides. Limits are absolute and enforced in
code, not prompts. Across 200 payments the engine denied **86 proposed actions**:

| Rule | Denied | What it stopped |
|---|---|---|
| B4 | 49 | retry more than 72h after failure |
| B2 | 15 | a second nudge to the same customer inside 24h |
| G7 | 11 | a third immediate retry on a transient failure |
| B5 | 8 | auto-executing above Rs 25,000 |
| S5 | 3 | messaging a customer who opted out |

B6 caps actions per run. `python3 src/runner.py n=20 b6=10` halts the agent at
exactly 10 actions and dead-letters the remaining 19 payments.

## Gated

Precedence is explicit (P1-P4). Bounds beat heuristics, always:

```
G5  earliest permitted 2026-02-01T10:00  (salary-cycle heuristic)
P2  G5 proposed 2026-02-01T10:00, outside the B4 ceiling 2026-01-30T04:55;
    clamped to 2026-01-29T20:59
```

The clamp lands at 20:59, not the ceiling itself, because the ceiling falls
inside B3 quiet hours — the clamp satisfies both bounds at once.

Rules are never short-circuited: one payment logged three independent denials
(B1 exhausted, B5 over Rs 25,000, G2 risk decline) rather than stopping at the
first, because `v_blocked_actions` is only honest if every rule that would have
blocked is counted.

## Audit Trail

`audit_log` is append-only, enforced by SQL triggers, and hash-chained row to
row. 413 events, 379 decisions, 1,413 policy checks — each check naming its
rule ID, result and reason.

```
$ sqlite3 data/recovery.db "UPDATE audit_log SET ts='hacked';"
Error: audit_log is append-only: UPDATE forbidden (19)
```

The triggers block UPDATE and DELETE but **allow INSERT**, so the real attack is
a forged row appended to the log. The hash chain catches that:

```
DETECTED: seq 4: chain broken, prev_hash deadbeef... does not match previous row
DETECTED: seq 4: row_hash mismatch, stored fakehash... (row was altered)
```

`verify_integrity()` re-walks the chain and re-checks for orphans at the end of
every run. See `append_only_proof.txt`.

## Measured Recovery

Three arms, identical seeded batch, identical oracle:

| Arm | Recovered | Rate | Paid | Attempts | Per attempt |
|---|---|---|---|---|---|
| do_nothing | Rs 0 | 0% | 0 | 0 | — |
| naive_retry | Rs 255,494 | 36.32% | 77 | 491 | Rs 520 |
| agent | Rs 134,922 | 19.18% | 47 | 202 | Rs 668 |

**The agent recovers less gross revenue than naive retry, and that is the
honest result.** Naive retry wins on volume by doing things the policy forbids:

| | naive_retry | agent |
|---|---|---|
| Attempts on a risk decline (G2) | 33 | 3 |
| Attempts on a dead instrument (G1) | 84 | 5 |
| Attempts on an unmapped code (G3) | 33 | 6 |
| Actions inside quiet hours (B3) | 248 | **0** |
| Policy checks written | 0 | 1,413 |

The agent recovers **28% more per attempt** (Rs 668 vs Rs 520) with 59% fewer
attempts and 90% fewer forbidden attempts. A merchant who cannot retry a risk
decline without damaging their issuer relationship cannot deploy naive retry at
any recovery rate.

## Exceptions We Could Not Resolve

51 payments, Rs 285,437, handed to a human rather than guessed at:

| Reason | Payments | Value |
|---|---|---|
| NEEDS_HUMAN_APPROVAL | 20 | Rs 172,319 |
| RISK_DECLINE | 18 | Rs 50,239 |
| UNKNOWN_ERROR_CODE | 13 | Rs 62,879 |

Plus 49 `expired` (B4 ran out), 29 `exhausted` (B1), 24 `unrecoverable`
(dead instrument). Nothing is silently dropped.

## Failure Handled Gracefully

**Circuit breaker (C1-C3).** Three consecutive 5xx trips it; every later call
raises `CircuitOpenError` across *all* adapter methods, and it reopens only via
`reset()`. Clearing the fault does not self-heal it. In-flight payments move to
the dead-letter queue with `ADAPTER_UNAVAILABLE` — no silent retries.

**Missing LLM (C4).** `diagnose.py` is rules-only by default. `anthropic` is
imported inside the function, never at module level, so the module loads with no
SDK installed. With `use_llm=True` and no key:

```
source=fallback  cause=TRANSIENT_GATEWAY  "llm unavailable (KeyError), rules stand"
```

No exception escapes and no money decision changes. Every metric here reproduces
with no key and no network.

**Missing health signal (G4).** Two issuer/method pairs have no snapshot at all.
Those fall back to a fixed 90-minute delay, logged as `reason=no_health_signal`.

## Limitations & Methodology

**The simulator is not reality.** Ground truth lives in `latent_json`, which the
agent never reads — `visible()` rebuilds each payment from an 11-field allowlist,
so leakage is structurally impossible rather than merely avoided. But the
recovery numbers are only as good as the oracle's assumptions.

**Rules-only diagnosis is 80% accurate, and that ceiling is deliberate.** The
simulator emits a misleading `error_code` for 35 of 200 payments (17%). Rules
cannot see through those. The issuer-health signal lifts accuracy from 76% to
80% by reinterpreting ambiguous gateway errors — but it also introduces 4 new
errors where a genuine blip coincided with a downtime window. Net +8, a trade
rather than a pure win. **Every one of the agent's 14 remaining forbidden
attempts traces to a misdiagnosis**, not to a policy failure: the engine can only
gate on what it was told. This is the honest case for adding an LLM.

**P2 clamps to the latest feasible time, which costs recovery.** G5's
salary-cycle heuristic proposes the 1st of the month; B4 caps at 72h; P2 clamps
to the latest moment inside the bound. INSUFFICIENT_FUNDS retries therefore land
a mean of 66.3h after failure, when customer intent has decayed. Clamping to the
*earliest* feasible time instead recovers Rs 142,792 (20.3%) and drops expired
payments from 49 to 12. The policy as written says "latest", so the agent does
"latest" — the fix is a one-word change to policy.md, not to the code.

**The audit log is single-writer.** `append_event` takes `seq` as `MAX(seq)+1`
inside `BEGIN IMMEDIATE` because the hash covers `seq`, so the log cannot be
written from two processes concurrently.

**The LLM path is untested.** No key was present, the flag is off, and
`_diagnose_llm` has never executed. Its failure path is tested; the call is not.

**Not implemented:** real delivery of nudges (generated and logged only), live
Razorpay integration (mocked behind real method signatures), and M1/M2 mandate
rules beyond amount and spacing checks.
