"""Batch diagnostics. Not part of the recovery path -- the runner never
imports this. Every count here gates a policy rule: a zero means that rule
can never fire and the matching README claim is unsupported."""

import json
from collections import Counter
from datetime import datetime, timedelta

from simulator import (B4_HOURS, BIG_TICKET_PAISE, CFG, FEB1, T0, UNMAPPED,
                       WINDOW_HOURS, dark_pairs, to_ist)


def print_batch_stats(payments):
    """Every count here gates a policy rule. A zero means that rule can never
    fire and the corresponding demo claim is unsupported."""
    n = len(payments)
    lat = [json.loads(p["latent_json"]) for p in payments]
    fa = [datetime.fromisoformat(p["failed_at"]) for p in payments]
    quiet = sum(1 for d in fa if to_ist(d).hour >= 21 or to_ist(d).hour < 9)
    print(f"\n=== batch: {n} payments, {T0:%b %d} + {WINDOW_HOURS}h ===")
    print(f"  unrecoverable          {sum(1 for l in lat if not l['recoverable']):>4}"
          f"  ({100*sum(1 for l in lat if not l['recoverable'])//n}%)")
    print(f"  amount > Rs 25,000     {sum(1 for p in payments if p['amount_paise'] > BIG_TICKET_PAISE):>4}  (B5)")
    print(f"  failed in quiet hours  {quiet:>4}  (B3)")
    print(f"  unmapped error codes   {sum(1 for p in payments if p['error_code'] in UNMAPPED):>4}  (G3)")
    print(f"  failed on days 25-31   {sum(1 for d in fa if d.day >= 25):>4}  (G5)")
    ins = [(p, l, d) for p, l, d in zip(payments, lat, fa)
           if l["true_cause"] == "INSUFFICIENT_FUNDS"]
    feasible = sum(1 for _, _, d in ins if d + timedelta(hours=B4_HOURS) >= FEB1)
    print(f"  INSUFFICIENT_FUNDS     {len(ins):>4}, of which Feb 1 is inside the")
    print(f"                              B4 window for {feasible} (G5 unclamped)"
          f" and outside for {len(ins)-feasible} (P2 clamp)")
    print(f"  issuer/method pairs with no health snapshot: "
          f"{', '.join('/'.join(p) for p in dark_pairs(payments))}  (G4 fallback)")
    print("  per method:      " + "  ".join(f"{k}={v}" for k, v in
          sorted(Counter(p["method"] for p in payments).items())))
    print("  per true_cause:  " + "  ".join(f"{k}={v}" for k, v in
          sorted(Counter(l["true_cause"] for l in lat).items())))
    mis = Counter(l["true_cause"] for p, l in zip(payments, lat)
                  if p["error_code"] not in CFG["codes"][l["true_cause"]])
    print(f"  error_code disagrees with true_cause: {sum(mis.values())} total")
    print("       " + "  ".join(f"{k}={v}" for k, v in sorted(mis.items())))
