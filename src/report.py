"""Exports run results to results.json and renders dashboard.html.

The JSON is inlined into the HTML at write time: the dashboard is opened over
file://, where fetch() of a sibling file is blocked by CORS. One self-contained
file, no server, no network.
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARMS = ("do_nothing", "naive_retry", "agent")
# Categorical slots 1-3 from the validated reference palette. Fixed order,
# never cycled; aqua fails 3:1 contrast so every bar carries a direct label.
SERIES = {"do_nothing": ("#2a78d6", "#3987e5"), "naive_retry": ("#eb6834", "#d95926"),
          "agent": ("#1baf7a", "#199e70")}

# Payment attempts on payments that could never succeed. NUDGE_CUSTOMER is
# excluded on purpose: G1 permits exactly one instrument-update nudge, so
# counting it as a violation would understate the agent by 20 actions.
ATTEMPTS_SQL = "a.action_type IN ('RETRY_NOW','RETRY_SCHEDULED','SWITCH_RAIL')"
VIOLATIONS = {
    "G2 attempted a risk decline": "'RISK_DECLINE'",
    "G1 attempted a dead instrument": "'INSTRUMENT_DEAD'",
    "G3 attempted an unmapped code": "'UNKNOWN'",
}


def collect(conn):
    data = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "integrity": audit.verify_integrity(conn) or ["audit chain intact, no orphans"],
            "arms": [], "blocked": [], "exceptions": [], "violations": {}, "diagnosis": []}

    for row in conn.execute(
            "SELECT arm, payments_at_risk, value_at_risk_paise, recovered_paise,"
            " recovery_rate_pct, payments_recovered, total_attempts, exception_count"
            " FROM v_run_summary"):
        data["arms"].append(dict(zip(
            ("arm", "at_risk", "value_paise", "recovered_paise", "rate_pct",
             "recovered_n", "attempts", "exceptions"), row)))
    data["arms"].sort(key=lambda a: ARMS.index(a["arm"]))
    for a in data["arms"]:
        a["per_attempt"] = round(a["recovered_paise"] / 100 / a["attempts"], 0) if a["attempts"] else 0

    for rule_id, desc, count in conn.execute("SELECT * FROM v_blocked_actions"):
        data["blocked"].append({"rule_id": rule_id, "desc": desc, "count": count})

    for code, n, amount in conn.execute(
            "SELECT reason_code, COUNT(*), SUM(amount_paise) FROM exceptions"
            " WHERE run_id='agent-42' GROUP BY reason_code ORDER BY 2 DESC"):
        data["exceptions"].append({"code": code, "count": n, "amount_paise": amount})

    for label, clause in VIOLATIONS.items():
        data["violations"][label] = [
            conn.execute(
                "SELECT COUNT(*) FROM actions a JOIN decisions d ON d.decision_id=a.decision_id"
                " JOIN payments p ON p.run_id=d.run_id AND p.payment_id=d.payment_id"
                f" WHERE d.run_id=? AND a.outcome IN ('success','fail') AND {ATTEMPTS_SQL}"
                f" AND json_extract(p.latent_json,'$.true_cause')={clause}",
                (f"{arm}-42",)).fetchone()[0] for arm in ("naive_retry", "agent")]

    for source, n in conn.execute(
            "SELECT diagnosis_source, COUNT(*) FROM decisions WHERE run_id='agent-42'"
            " GROUP BY 1"):
        data["diagnosis"].append({"source": source, "count": n})
    data["checks_written"] = conn.execute(
        "SELECT COUNT(*) FROM policy_checks").fetchone()[0]
    return data


def bar(label, value, maximum, colour, suffix=""):
    # A zero renders as nothing: a minimum-width stub would read as "a little".
    pct = 0 if not maximum or not value else max(1.5, 100 * value / maximum)
    return (f'<div class="row"><span class="lbl">{label}</span>'
            f'<span class="track"><span class="fill" style="width:{pct:.1f}%;'
            f'background:{colour}"></span></span>'
            f'<span class="val">{value:,.0f}{suffix}</span></div>')


def render(data):
    arms = data["arms"]
    agent = next(a for a in arms if a["arm"] == "agent")
    naive = next(a for a in arms if a["arm"] == "naive_retry")
    at_risk = agent["value_paise"] / 100

    tiles = [("Value at risk", f"Rs {at_risk:,.0f}", f"{agent['at_risk']} failed payments"),
             ("Agent recovered", f"Rs {agent['recovered_paise']/100:,.0f}",
              f"{agent['rate_pct']}% of value at risk"),
             ("Rupees per attempt", f"Rs {agent['per_attempt']:,.0f}",
              f"naive: Rs {naive['per_attempt']:,.0f}"),
             ("Policy checks logged", f"{data['checks_written']:,}",
              "naive_retry: 0")]
    tile_html = "".join(
        f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="s">{s}</div></div>' for k, v, s in tiles)

    max_rate = max(a["rate_pct"] or 0 for a in arms) or 1
    rate_bars = "".join(bar(a["arm"], a["rate_pct"] or 0, max_rate,
                            SERIES[a["arm"]][0], "%") for a in arms)
    max_att = max(a["attempts"] for a in arms) or 1
    att_bars = "".join(bar(a["arm"], a["attempts"], max_att, SERIES[a["arm"]][0])
                       for a in arms)
    max_deny = max((b["count"] for b in data["blocked"]), default=1)
    deny_bars = "".join(
        bar(f'{b["rule_id"]} &middot; {b["desc"]}', b["count"], max_deny, "#4a3aa7")
        for b in data["blocked"])

    arm_rows = "".join(
        f'<tr><td>{a["arm"]}</td><td>{a["at_risk"]}</td>'
        f'<td>Rs {a["recovered_paise"]/100:,.0f}</td><td>{a["rate_pct"] or 0}%</td>'
        f'<td>{a["recovered_n"]}</td><td>{a["attempts"]}</td>'
        f'<td>Rs {a["per_attempt"]:,.0f}</td><td>{a["exceptions"]}</td></tr>'
        for a in arms)
    exc_rows = "".join(
        f'<tr><td>{e["code"]}</td><td>{e["count"]}</td>'
        f'<td>Rs {(e["amount_paise"] or 0)/100:,.0f}</td></tr>' for e in data["exceptions"])
    vio_rows = "".join(
        f'<tr><td>{k}</td><td class="bad">{v[0]}</td><td class="ok">{v[1]}</td></tr>'
        for k, v in data["violations"].items())
    integrity = " &middot; ".join(data["integrity"])

    html = TEMPLATE
    for token, value in (("TILES", tile_html), ("RATE_BARS", rate_bars),
                         ("ATT_BARS", att_bars), ("DENY_BARS", deny_bars),
                         ("ARM_ROWS", arm_rows), ("EXC_ROWS", exc_rows),
                         ("VIO_ROWS", vio_rows), ("INTEGRITY", integrity),
                         ("GENERATED", data["generated_at"]),
                         ("POLICY_VERSION", data["policy_version"]),
                         ("PAYLOAD", json.dumps(data))):
        html = html.replace("__%s__" % token, str(value))
    return html


def main(db=None):
    conn = audit.get_conn(db or audit.DB_PATH)
    data = collect(conn)
    with open(os.path.join(ROOT, "policy.md")) as f:
        data["policy_version"] = f.readline().strip().split("—")[-1].strip()
    with open(os.path.join(ROOT, "results.json"), "w") as f:
        json.dump(data, f, indent=2)
    with open(os.path.join(ROOT, "dashboard.html"), "w") as f:
        f.write(render(data))
    print(f"  wrote results.json and dashboard.html")
    print(f"  integrity: {'; '.join(data['integrity'])}")
    return data


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Payment Recovery Agent</title>
<style>
:root{color-scheme:light;--bg:#f4f4f2;--surface:#fcfcfb;--line:#e2e1dc;
--ink:#0b0b0b;--ink2:#52514e;--ink3:#7a7975;--ok:#008300;--bad:#e34948;}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#111110;--surface:#1a1a19;--line:#33322f;--ink:#fff;--ink2:#c3c2b7;
--ink3:#8e8d85;--ok:#4caf50;--bad:#e66767;color-scheme:dark;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1020px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--ink2);margin:0 0 6px}
.meta{color:var(--ink3);font-size:12px;margin:0 0 28px}
.ok-badge{color:var(--ok);font-weight:600}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
color:var(--ink2);margin:34px 0 12px;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{font-size:12px;color:var(--ink2)}
.tile .v{font-size:24px;font-weight:650;letter-spacing:-.02em;margin:4px 0 2px}
.tile .s{font-size:12px;color:var(--ink3)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.row{display:flex;align-items:center;gap:12px;margin:9px 0}
.lbl{flex:0 0 268px;font-size:13px;color:var(--ink2)}
.track{flex:1;height:16px;background:transparent;position:relative}
.fill{position:absolute;left:0;top:0;height:16px;border-radius:0 4px 4px 0;
box-shadow:0 0 0 2px var(--surface)}
.val{flex:0 0 72px;text-align:right;font-variant-numeric:tabular-nums;
font-size:13px;color:var(--ink)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--ink2);font-size:12px;
text-transform:uppercase;letter-spacing:.04em;padding:6px 10px 6px 0;
border-bottom:1px solid var(--line)}
td{padding:7px 10px 7px 0;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.bad{color:var(--bad);font-weight:600}.ok{color:var(--ok);font-weight:600}
.note{color:var(--ink3);font-size:12px;margin-top:10px}
.scroll{overflow-x:auto}
</style></head><body><div class="wrap">
<h1>Payment failure recovery agent</h1>
<p class="sub">Kettle &amp; Co &middot; 200 failed payments &middot; policy __POLICY_VERSION__ &middot; seed 42</p>
<p class="meta">Generated __GENERATED__ &middot; <span class="ok-badge">__INTEGRITY__</span></p>

<div class="tiles">__TILES__</div>

<h2>Recovery rate by arm</h2>
<div class="card">__RATE_BARS__
<p class="note">All three arms run against the identical seeded batch and the identical
oracle. do_nothing is the counterfactual: no actions, no recovery.</p></div>

<h2>Attempts spent</h2>
<div class="card">__ATT_BARS__
<p class="note">Fewer attempts is better for the same recovery: every attempt costs a
gateway fee and a little customer patience.</p></div>

<h2>Actions the policy engine blocked</h2>
<div class="card">__DENY_BARS__
<p class="note">Each bar is a rule in policy.md that denied a proposed action, counted
from the policy_checks table. naive_retry consults no policy and writes no checks.</p></div>

<h2>Policy violations by arm</h2>
<div class="card scroll"><table>
<tr><th>Violation</th><th>naive_retry</th><th>agent</th></tr>
__VIO_ROWS__</table>
<p class="note">Payment attempts on payments that could never succeed, counted against
simulator ground truth after the fact. The agent&rsquo;s remaining attempts all trace to
misdiagnosis: rules-only classification reads a misleading error code, and the policy
engine can only gate on what it was told. The G1-permitted instrument-update nudge is
excluded &mdash; it is allowed, not a violation.</p></div>

<h2>Full comparison</h2>
<div class="card scroll"><table>
<tr><th>Arm</th><th>At risk</th><th>Recovered</th><th>Rate</th><th>Paid</th>
<th>Attempts</th><th>Per attempt</th><th>Exceptions</th></tr>
__ARM_ROWS__</table></div>

<h2>Exceptions we could not resolve</h2>
<div class="card scroll"><table>
<tr><th>Reason</th><th>Payments</th><th>Value</th></tr>
__EXC_ROWS__</table>
<p class="note">These are surfaced, not hidden. Every one is a payment the agent
declined to act on and handed to a human.</p></div>
</div>
<script type="application/json" id="results">__PAYLOAD__</script>
</body></html>
"""


if __name__ == "__main__":
    main()
