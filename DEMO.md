# Demo run-sheet

Four minutes. No install, no key, no network. Run from the project root.

## 0. Before you start (30s)

```bash
cd recovery-agent
python3 src/runner.py && python3 src/report.py && open dashboard.html
```

Leave the dashboard open in a browser tab. Everything below re-derives it.

---

## 1. "The policy is a document, and the agent cannot override it." (45s)

```bash
head -60 policy.md
```

Point at **P1**: bounds are absolute, heuristics may only narrow them. Then:

```bash
grep -n "def g4_issuer_downtime" -A 8 src/policy.py
```

Every rule ID in `policy.md` is a function of the same name in `policy.py`. A
judge who reads G4 in the doc can grep G4 in the code and find one function.

---

## 2. The audit log refuses to be rewritten (60s) — **the strongest moment**

```bash
sqlite3 data/recovery.db "UPDATE audit_log SET ts='hacked';"
```

```
Error: audit_log is append-only: UPDATE forbidden (19)
```

Then the point most people miss — **the trigger allows INSERT**, so the real
attack is appending a forged row, and the hash chain is what catches that:

```bash
python3 -m unittest discover -s tests -t tests -k forged -v
```

```
Triggers allow INSERT, so a forged append is the real attack. ... ok
Ran 1 test — OK
```

Say: *triggers stop the clumsy attack, the hash chain stops the clever one.*

---

## 3. Precedence, live (45s)

```bash
grep -n "P2" results.json | head -1
sqlite3 data/recovery.db "SELECT reason FROM policy_checks WHERE rule_id='P2' LIMIT 1;"
```

```
G5 proposed 2026-02-01T10:00:00+05:30, outside the B4 ceiling
2026-01-29T07:26:00+05:30; clamped to 2026-01-26T13:26:00+05:30
```

A heuristic asked for a date outside a hard bound. The engine clamped it,
logged both rule IDs, and never changed the action type (P4).

---

## 4. It stops itself (30s)

```bash
python3 src/runner.py n=20 b6=10 db=data/b6_demo.db
```

The separate `db=` matters: without it this run overwrites the database
the dashboard was built from.

The agent halts at exactly 10 actions and dead-letters the rest. B6 is a bound
the agent enforces against itself mid-run.

---

## 5. The numbers, including the ones that lose (60s)

Open the dashboard. Lead with the honest framing:

> Naive retry recovers **more gross revenue** — 36% vs 20%. It gets there with
> 248 actions inside customer quiet hours, 33 retries of risk declines, and 84
> retries of dead cards. The agent does **zero** of the first and 89% fewer of
> the rest, logs 1,540 policy checks against naive's zero, and hands 51 payments
> it could not resolve to a human instead of guessing.

Then: *a merchant who retries risk declines loses their issuer relationship
regardless of what it recovers. We measured the cost of the constraint instead
of hiding it.*

---

## 6. If asked "is any of this actually tested?"

```bash
python3 -m unittest discover -s tests -t tests
```

```
Ran 80 tests in 4.6s
OK
```

Runs on `/usr/bin/python3` with nothing installed.

---

## Questions you should expect

**"Why is your agent worse than a cron job?"** It isn't, on the axes a merchant
is exposed on. Gross recovery ignores issuer-relationship damage, quiet-hours
complaints, and wasted gateway fees. See the violations table.

**"Where's the AI?"** Diagnosis is rules-only and reproducible with no key —
deliberately, because every money decision must be deterministic (C4). The LLM
path exists behind `use_llm=False`. The honest case for turning it on is in
Limitations: rules misclassify 35 of 200 payments because the simulator emits a
misleading error code, and **every one of the agent's 16 forbidden attempts
traces to a misdiagnosis**, not a policy failure.

**"How do I know the agent isn't cheating?"** `latent_json` holds ground truth.
`diagnose.visible()` rebuilds each payment from an 11-field allowlist, so the
agent structurally cannot read it. `tests/test_policy.py` asserts it, and
`tests/test_adapter.py` asserts the string never appears in the adapter's code.
