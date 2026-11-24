"""Communications outputs from one approved packet (prompt §13), citation coverage and the consistency check.

Every factual line ends with fact-ID citations, e.g. "[F-0007]". A line is factual when it contains a fact token:
an amount ($1,234.56), a date (YYYY-MM-DD), a record id (WO-, MSG-, TD-, CASE-, BL-, JE-, SA-, PRM-, MTR-, RD-, CM-)
or an account id. The consistency check confirms every amount, date, bill id and account id is identical across
all outputs and matches the packet; a failure blocks OUTPUT_APPROVAL.
"""
import re

from .util import fmt_money

MONEY = re.compile(r"\$[\d,]+\.\d{2}")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
RECORD = re.compile(r"\b(?:WO|MSG|TD|CASE|BL|JE|SA|PRM|MTR|RD|CM)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
ACCOUNT = re.compile(r"\b88\d{8}\b")
BILL = re.compile(r"\bBL-[A-Za-z0-9]+-\d{6}\b")
CITE = re.compile(r"\[(F-\d{4}(?:\s*,\s*F-\d{4})*)\]\s*$")
CITE_ANY = re.compile(r"F-\d{4}")
NAMES = {"customer_summary": "Customer summary", "executive_brief": "Executive brief", "jira_ticket": "Jira ticket",
         "regulator_response": "Regulator response"}


def c(*fids):
    ids = sorted({f for f in fids if f})
    return " [%s]" % ", ".join(ids) if ids else ""


def factual_lines(md):
    out = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or set(line) <= set("-|: "):
            continue
        body = CITE.sub("", line)
        body = re.sub(r"\bBILL-NEW-\d+\b|\bUT-\d+\b", "", body)
        if MONEY.search(body) or DATE.search(body) or RECORD.search(body) or ACCOUNT.search(body):
            m = CITE.search(line)
            out.append({"line": line, "cited": bool(m), "fact_ids": CITE_ANY.findall(m.group(1)) if m else []})
    return out


def citation_stats(outputs, known_fact_ids):
    total = cited = 0
    bad = []
    for name, md in outputs.items():
        if not md:
            continue
        for fl in factual_lines(md):
            total += 1
            ok = fl["cited"] and all(f in known_fact_ids for f in fl["fact_ids"])
            cited += 1 if ok else 0
            if not ok:
                bad.append({"output": name, "line": fl["line"][:160]})
    return {"factual": total, "cited": cited, "coverage": (cited / total) if total else 1.0, "uncited": bad}


def tokens(md):
    body = "\n".join(CITE.sub("", l) for l in md.splitlines())
    body = re.sub(r"\bBILL-NEW-\d+\b", "", body)
    return {"amounts": set(MONEY.findall(body)), "dates": set(DATE.findall(body)), "bill_ids": set(BILL.findall(body)),
            "account_ids": set(ACCOUNT.findall(body))}


def consistency(outputs, packet):
    allowed = packet["allowed"]
    issues, per = [], {}
    for name, md in outputs.items():
        if not md:
            continue
        tk = tokens(md)
        per[name] = {k: sorted(v) for k, v in tk.items()}
        for kind in ("amounts", "dates", "bill_ids", "account_ids"):
            for v in sorted(tk[kind] - set(allowed[kind])):
                issues.append("%s: %s %s does not match the packet" % (NAMES.get(name, name), kind[:-1].replace("_", " "), v))
    primary = fmt_money(packet["amount"]) if packet.get("amount") else None
    with_amounts = [n for n, p in per.items() if p["amounts"]]
    if primary:
        for n in with_amounts:
            if primary not in per[n]["amounts"]:
                issues.append("%s does not state the packet amount %s" % (NAMES.get(n, n), primary))
    accs = {a for p in per.values() for a in p["account_ids"]}
    if len(accs) > 1:
        issues.append("Outputs disagree on the account id: %s" % ", ".join(sorted(accs)))
    return {"pass": not issues, "issues": issues, "tokens": per}


# ---- renderers (the mock LLM's templated text) --------------------------------------------------
def _bills_line(p):
    b = p["bills"]
    return ", ".join(x["bill_id"] for x in b)


def customer_summary(p):
    f = p["facts"]
    L = ["# %s — what we found and what we have done" % p["customer_name"], "",
         "Account %s, %s.%s" % (p["account_id"], p["premise_address"], c(f.get("account"), f.get("sa"))), "",
         "## What we found"]
    L.append("Your supply was physically disconnected on %s (work order %s).%s" % (p["disconnect_date"], p["wo_id"], c(f.get("wo"))))
    if p["type"] == "ADVANCE":
        L.append("Our billing system did not receive the disconnection, so your account %s was still open.%s" % (p["sa_id"], c(f.get("sa"), f.get("msg"))))
    else:
        for b in p["bills"]:
            L.append("Bill %s for %s to %s included %s day(s) after that date.%s" % (b["bill_id"], b["period_start"], b["period_end"], b["post_days"], c(b.get("fact_id"), f.get("wo"))))
    L += ["", "## What we have done"]
    if p["type"] == "PREVENT":
        b = p["bills"][0]
        if b.get("action") == "CANCEL":
            L.append("We stopped draft bill %s for %s before it was issued, so you will not receive it.%s" % (b["bill_id"], fmt_money(b["before"]), c(f.get("calc"))))
        else:
            L.append("We corrected draft bill %s before it was issued: it is now %s instead of %s, a reduction of %s.%s" % (
                b["bill_id"], fmt_money(b["after"]), fmt_money(b["before"]), fmt_money(p["amount"]), c(f.get("calc"))))
    elif p["type"] == "REMEDIATE":
        L.append("We have credited %s to your account for bill(s) %s.%s" % (fmt_money(p["amount"]), _bills_line(p), c(f.get("calc"))))
    else:
        L.append("We closed your account %s with effect from %s, so no charges are raised for later days.%s" % (p["sa_id"], p["disconnect_date"], c(f.get("sa"), f.get("wo"))))
        L.append("This avoids an estimated %s on your next bill.%s" % (fmt_money(p["amount"]), c(f.get("projection"))))
    L += ["", "## What happens next",
          "Your next statement will show this correction, and you do not need to do anything.",
          "We will send you an update by %s.%s" % (p["next_update"], c(f.get("next_update"))),
          "", "If anything here looks wrong, reply to this letter or call us and quote your account number.", ""]
    return "\n".join(L)


def executive_brief(p):
    f = p["facts"]
    L = ["# Executive brief — %s (%s)" % (p["customer_name"], p["case_id"]), "",
         "## Situation",
         "%s customer %s was disconnected on %s by work order %s, but billing was not stopped.%s" % (
             p["customer_class"].title() if p.get("customer_class") else "A", p["customer_name"], p["disconnect_date"], p["wo_id"], c(f.get("wo"), f.get("account"))),
         "", "## Impact"]
    if p["type"] == "ADVANCE":
        L.append("No wrong bill has been issued; the projected exposure this cycle was %s.%s" % (fmt_money(p["amount"]), c(f.get("projection"))))
    elif p["type"] == "PREVENT":
        L.append("Draft bill %s was held at the bill run; %s was prevented.%s" % (p["bills"][0]["bill_id"], fmt_money(p["amount"]), c(f.get("calc"))))
    else:
        L.append("Bill(s) %s were issued and posted; the customer is owed %s, posted as a credit to period %s.%s" % (
            _bills_line(p), fmt_money(p["amount"]), p.get("credit_period") or "-", c(f.get("calc"), f.get("period"))))
    L += ["Priority %s, triage %s, confidence %.2f, probable cause %s (%s).%s" % (p["priority"], p["triage"], p["confidence"], p["cause"], p["cause_label"],
                                                                                  c(*p.get("cause_fact_ids", []))),
          "", "## What we found"]
    for r in p["rules"]:
        L.append("- **%s %s:** %s" % (r["rule"], r["name"], r["text"]))
    L += ["", "## Next steps"]
    for s in p["s_codes"]:
        L.append("- %s: %s." % (s, p["s_code_names"][s]))
    L.append("- Engineering ticket %s tracks the systemic fix." % p["jira_id"])
    L.append("")
    return "\n".join(L)


def jira_title(p):
    if p["cause"] in ("C1", "C2"):
        return "Implement OMS disconnect event as billing suppression trigger in CC&B"
    return {"C3": "Validate OMS premise IDs against the crosswalk before disconnect events are sent",
            "C6": "Block bill runs until completed disconnects are processed in CC&B",
            "C4+C5": "Tie MDM estimation to premise connection status during non-pay windows"}.get(
        p["cause"], "Investigate billed-after-disconnection root cause")


def jira_ticket(p):
    f = p["facts"]
    L = ["# %s — %s" % (p["jira_id"], jira_title(p)), "",
         "**Priority:** %s · **Rule set:** %s · **Case:** %s" % (p["priority"], p["rule_set_version"], p["case_id"]), "",
         "## Links",
         "- Work order %s (%s, completed %s).%s" % (p["wo_id"], p["subtype"], p["disconnect_date"], c(f.get("wo")))]
    if p.get("msg_id"):
        L.append("- Integration message %s, status %s.%s" % (p["msg_id"], p["msg_status"], c(f.get("msg"))))
    for t in p.get("todos", []):
        L.append("- To Do %s (%s).%s" % (t["todo_id"], t["status"], c(t.get("fact_id"))))
    L += ["", "## Problem", "Probable cause %s: %s.%s" % (p["cause"], p["cause_label"], c(*p.get("cause_fact_ids", [])))]
    if p.get("cause_quote"):
        L.append("Integration error: \"%s\"%s" % (re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", p["cause_quote"]).strip(), c(f.get("msg"))))
    L += ["", "## Acceptance criteria"]
    ac = {"S1": "CC&B pre-bill check holds any draft covering days after a completed disconnect",
          "S2": "MDM stops estimating reads for premises whose status is disconnected",
          "S3": "Every completed OMS disconnect event reaches CC&B and stops the SA, with alerting on failure",
          "S4": "CRM complaint categories about disconnection set a billing hold automatically",
          "S5": "GL flags revenue on disputed bills as disputed until the dispute is resolved"}
    for s in p["s_codes"]:
        L.append("- [%s] %s." % (s, ac.get(s, p["s_code_names"].get(s, s))))
    L.append("")
    return "\n".join(L)


def regulator_response(p):
    f = p["facts"]
    reg = p.get("regulatory_case") or {}
    L = ["# Response to regulatory complaint %s" % reg.get("case_id", ""), "",
         "Customer %s, account %s.%s" % (p["customer_name"], p["account_id"], c(f.get("account"))), "",
         "## Chronology"]
    for ev in p["chronology"]:
        L.append("- %s: %s%s" % (ev["date"], ev["text"], c(*ev["fact_ids"])))
    L += ["", "## Findings"]
    for r in p["rules"]:
        L.append("- %s %s: %s" % (r["rule"], r["name"], r["text"]))
    L += ["", "## Remediation completed"]
    for line in p["remediation"]:
        L.append("- %s%s" % (line["text"], c(*line["fact_ids"])))
    L += ["", "## Systemic fixes"]
    for s in p["s_codes"]:
        L.append("- %s: %s." % (s, p["s_code_names"][s]))
    L += ["", "## Contact", "Billing Exceptions Lead, Utilico Energy (DRI for this case: %s)." % p["dri_name"], ""]
    return "\n".join(L)


def render_all(p):
    out = {"customer_summary": None, "executive_brief": None, "jira_ticket": None, "regulator_response": None}
    if p["type"] != "REGULATORY_RESPONSE":
        out["customer_summary"] = customer_summary(p)
        out["executive_brief"] = executive_brief(p)
        out["jira_ticket"] = jira_ticket(p)
    if p.get("regulatory_case") or p["type"] == "REGULATORY_RESPONSE":
        out["regulator_response"] = regulator_response(p)
    return out
