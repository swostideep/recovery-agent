"""Measures what the LLM is actually worth, on the payments where rules are blind.

Rules-only diagnosis has a hard ceiling: the simulator emits a misleading
error_code for ~17% of payments, and the code is all the rules can see. This
script measures whether an LLM sees through them, on the subset where rules
report low confidence -- the only place it is allowed to run (C4, diagnose.py).

    export ANTHROPIC_API_KEY=sk-ant-...
    python3 src/llm_eval.py

Costs one API call per uncertain payment (~60 for the default batch). It writes
llm_eval.json and changes nothing else: the submitted metrics stay rules-only
and reproducible with no key.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import timedelta

import diagnose
import simulator as sim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLD = 0.70


def load_key():
    """Read .env without a dependency, so this works however the key was set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.startswith("ANTHROPIC_API_KEY=") and line.strip().split("=", 1)[1]:
                os.environ["ANTHROPIC_API_KEY"] = line.strip().split("=", 1)[1]
                return True
    return False


def main(seed=42, n=200):
    payments = sim.generate_batch(seed, n)
    health = sim.generate_issuer_health(seed, payments)
    index = {}
    for row in health:
        index.setdefault((row["issuer"], row["method"]), []).append(row)

    if not load_key():
        print("No ANTHROPIC_API_KEY found in the environment or .env.\n"
              "The LLM path is optional and ships disabled: every number in the\n"
              "README reproduces without it. To measure the LLM's contribution:\n"
              "    echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env\n"
              "    python3 src/llm_eval.py")
        return None

    from datetime import datetime
    uncertain, rules_ok, llm_ok, flipped, errors = [], 0, 0, [], 0
    for pay in payments:
        now = datetime.fromisoformat(pay["failed_at"]) + timedelta(hours=1)
        snaps = index.get((pay["issuer"], pay["method"]))
        snap = snaps[0] if snaps else None
        base = diagnose.diagnose(pay, now, health=snap)
        if base["confidence"] >= THRESHOLD:
            continue                      # rules are confident; the LLM never runs
        truth = json.loads(pay["latent_json"])["true_cause"]
        result = diagnose.diagnose(pay, now, health=snap, use_llm=True,
                                   llm_threshold=THRESHOLD)
        if result["diagnosis_source"] == "fallback":
            errors += 1
        uncertain.append(pay["payment_id"])
        rules_ok += base["root_cause"] == truth
        llm_ok += result["root_cause"] == truth
        if base["root_cause"] != result["root_cause"]:
            flipped.append({"payment_id": pay["payment_id"], "error_code": pay["error_code"],
                            "truth": truth, "rules": base["root_cause"],
                            "llm": result["root_cause"],
                            "llm_right": result["root_cause"] == truth})

    total = len(uncertain)
    report = {"uncertain_payments": total, "rules_correct": rules_ok,
              "llm_correct": llm_ok, "delta": llm_ok - rules_ok,
              "llm_errors_fell_back": errors, "changed_answers": flipped}
    with open(os.path.join(ROOT, "llm_eval.json"), "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n=== LLM contribution on the {total} payments where rules are uncertain ===")
    print(f"  rules-only correct : {rules_ok}/{total} ({100*rules_ok//max(total,1)}%)")
    print(f"  with LLM correct   : {llm_ok}/{total} ({100*llm_ok//max(total,1)}%)")
    print(f"  net                : {llm_ok - rules_ok:+d} payments")
    print(f"  fell back to rules : {errors} (C4 path)")
    print(f"  answers changed    : {len(flipped)}, of which"
          f" {sum(1 for f in flipped if f['llm_right'])} were improvements")
    print("\n  wrote llm_eval.json")
    return report


if __name__ == "__main__":
    main()
