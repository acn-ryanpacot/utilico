"""Self-test. Every scenario runs in an isolated, in-memory engine (agent latency 0, own audit log); nothing touches
state/ or the source CSVs. The process ends at the DRI's decision, which seals the case."""
import asyncio
import copy
import re
import time
from decimal import Decimal

from . import backscan as BS
from . import guard
from .agents.llm import POLICIES, call
from .agents.models import RetrievalOut
from .agents.runner import run_agent
from .agents.tools import GuardrailError
from .audit import AuditLog
from .config import Config
from .engine import Engine
from .kpi import kpis
from .util import AppError, PermissionDenied

DATE_FIELDS = ("completed_at", "received_at", "opened_at", "created_at", "posting_date", "bill_run_at")


def R(group, check, expected, actual, ok):
    return {"group": group, "check": check, "expected": str(expected), "actual": str(actual), "pass": bool(ok)}


def money_ok(a, b, tol):
    return a is not None and b is not None and abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(tol)) + Decimal("0.000001")


class SelfTest:
    def __init__(self, registry, kb, source_hashes_before=None):
        self.registry, self.kb = registry, kb
        self.cfg = Config(overrides={"agent.json": {"step_latency_ms": 0}})
        self.ex = self.cfg.expected()
        self.tol = self.ex["tolerance"]
        self.hashes_before = source_hashes_before

    def engine(self):
        return Engine(self.registry, self.kb, self.cfg, state_root=None, audit=AuditLog(None), fast=True)

    def run(self):
        t0 = time.time()
        rows = asyncio.run(self._run())
        groups = {}
        for r in rows:
            groups.setdefault(r["group"], []).append(r)
        failed = sum(1 for r in rows if not r["pass"])
        return {"pass": failed == 0, "checks": len(rows), "failed": failed, "elapsed_ms": int((time.time() - t0) * 1000),
                "knowledge_mode": self.kb.mode, "groups": [{"name": g, "pass": all(r["pass"] for r in rs), "rows": rs} for g, rs in groups.items()]}

    async def _run(self):
        rows = []
        blocked_before = len(guard.blocked)
        for d in self.registry.list():
            rows += self.poc1(self.registry.get(d["id"]))
        for ds_id in ("dataset_A_with_errors", "dataset_B_two_errors_corrected"):
            for sc, mode in (("PREVENTION_REPLAY", "followup_on"), ("PREVENTION_REPLAY", "followup_off"), ("DISPUTE_REPLAY", "default"),
                             ("BACKSCAN_2026-11-10", "default")):
                rows += await self.scenario(ds_id, sc, mode)
        rows += await self.lookups()
        rows += await self.determinism()
        rows += await self.sod()
        rows += await self.guardrails()
        rows += await self.stale_feed()
        rows += await self.seal_tamper()
        rows += self.integrity_sources()
        rows.append(R("Offline", "No outbound connection attempted during the self-test", 0, len(guard.blocked) - blocked_before,
                      len(guard.blocked) == blocked_before))
        return rows

    # ---- POC 1 ----------------------------------------------------------------------------------------
    def poc1(self, ds):
        rows = BS.answer_key_checks(ds)
        for r in rows:
            r["expected"], r["actual"] = str(r["expected"]), str(r["actual"])
        ex = self.ex["backscan_totals"].get(ds.id)
        tot = next((r for r in rows if r["check"].startswith("Back-scan total")), None)
        if ex and tot:
            tot["expected"] = "%.2f + unmatched %.2f" % (ex["total"], ex["unmatched"])
            tot["pass"] = money_ok(tot["total"], ex["total"], 0.01) and money_ok(tot["unmatched"], ex["unmatched"], 0.01)
        e6 = next((r for r in rows if r["check"].startswith("E6")), None)
        if e6 and ex:
            e6["pass"] = e6["pass"] and ("%s.0 kWh" % ex["E6_review_kwh"]) in e6["actual"]
            e6["expected"] += " (%s kWh)" % ex["E6_review_kwh"]
        return [{k: v for k, v in r.items() if k in ("group", "check", "expected", "actual", "pass")} for r in rows]

    # ---- scenarios -----------------------------------------------------------------------------------
    def expected_for(self, ds_id, sc, mode):
        e = copy.deepcopy(self.ex["scenarios"][ds_id][sc][mode])
        if "cases_from" in e:
            a, b, c = e["cases_from"]
            cases = copy.deepcopy(self.ex["scenarios"][a][b][c])["cases"]
            if "only" in e:
                cases = [x for x in cases if x["case"] in e["only"]]
            e["cases"] = cases
            e.pop("jira_order", None)
        return e

    async def play(self, ds_id, sc, followup=True, autopilot=True):
        eng = self.engine()
        s = eng.session(ds_id, sc)
        s.state["toggles"]["followup"] = followup
        if sc == "BACKSCAN_2026-11-10":
            s.state["toggles"]["autopilot"] = autopilot
            s.send_backscan(self.cfg.user("u.csr"))
        await s.run_to_end(autopilot=autopilot)
        return eng, s

    async def scenario(self, ds_id, sc, mode):
        g = "%s - %s%s" % (ds_id.split("_")[1], sc, "" if mode == "default" else " (follow-up %s)" % ("ON" if mode == "followup_on" else "OFF"))
        e = self.expected_for(ds_id, sc, mode)
        eng, s = await self.play(ds_id, sc, followup=(mode != "followup_off"))
        codes = s.ds.case_codes
        cases = sorted(s.state["cases"].values(), key=lambda c: c["case_id"])
        rows = []
        exp = e["cases"]
        rows.append(R(g, "Cases opened", ", ".join("%s %s" % (x["case"], x["type"]) for x in exp),
                      ", ".join("%s %s" % (codes.get(c["account_id"]), c["type"]) for c in cases),
                      sorted((x["case"], x["type"]) for x in exp) == sorted((codes.get(c["account_id"]), c["type"]) for c in cases)))
        used = set()
        for x in exp:
            c = next((c for c in cases if codes.get(c["account_id"]) == x["case"] and c["type"] == x["type"] and c["case_id"] not in used), None)
            if not c:
                rows.append(R(g, "%s %s" % (x["case"], x["type"]), "case", "missing", False))
                continue
            used.add(c["case_id"])
            rows += self.case_rows(g, x, c, s)
        for code in self.ex["never_cases"]:
            bad = [c["case_id"] for c in cases if codes.get(c["account_id"]) == code]
            if bad:
                rows.append(R(g, "No case for %s" % code, "none", ", ".join(bad), False))
        rows.append(R(g, "No cases outside the expected list", "none", ", ".join(c["case_id"] for c in cases if codes.get(c["account_id"]) not in {x["case"] for x in exp}) or "none",
                      all(codes.get(c["account_id"]) in {x["case"] for x in exp} for c in cases)))
        k = kpis(s)
        for key, label in (("prevented", "$ prevented (approved PREVENT reductions)"), ("remediated", "$ remediated (approved credits)"),
                           ("exposure_addressed", "$ exposure addressed (approved ADVANCE)")):
            if key in e:
                rows.append(R(g, label, "%.2f" % e[key], "%.2f" % k[key], money_ok(k[key], e[key], self.tol["money"])))
        if "held" in e:
            rows.append(R(g, "Bills held", e["held"], k["bills"]["held"], k["bills"]["held"] == e["held"]))
        if "held_on_existing_case" in e:
            rows.append(R(g, "Held bills attached to an existing case (no second case)", e["held_on_existing_case"], k["bills"]["held_on_existing_case"],
                          k["bills"]["held_on_existing_case"] == e["held_on_existing_case"]))
        if "jira_order" in e:
            got = [codes.get(c["account_id"]) for c in cases]
            jiras = [c["jira_id"] for c in cases]
            rows.append(R(g, "Jira numbering BILL-NEW-1147.. in case order", e["jira_order"], "%s = %s" % (got, ", ".join(jiras)),
                          got == e["jira_order"] and jiras == ["BILL-NEW-%d" % (1147 + i) for i in range(len(jiras))]))
        if "send_disabled" in e:
            rows += self.send_disabled(g, s, e["send_disabled"])
        rows.append(R(g, "Every case ends at the DRI's decision (APPROVED) and is sealed", "all",
                      ", ".join("%s %s" % (c["case_id"], c["state"]) for c in cases if c["state"] != "APPROVED" or not c.get("seal")) or "all",
                      all(c["state"] == "APPROVED" and c.get("seal") for c in cases)))
        rows.append(R(g, "No execution, Finance or output steps", "none",
                      ", ".join(sorted({e2["action"] for e2 in eng.audit.events if e2["action"] in ("PLAN_PROPOSED", "STEP_EXECUTED", "FINANCE_APPROVED", "OUTPUTS_DRAFTED")})) or "none",
                      not any(e2["action"] in ("PLAN_PROPOSED", "STEP_EXECUTED", "FINANCE_APPROVED", "OUTPUTS_DRAFTED") for e2 in eng.audit.events)))
        rows.append(R(g, "Citation coverage (agent statements)", "100%", "-" if k["citation_coverage"] is None else "%.1f%%" % (k["citation_coverage"] * 100),
                      k["citation_coverage"] == 1.0))
        rows.append(R(g, "Day-30 gate", "all four pass", ", ".join("%s %s" % (r["name"], r["display"]) for r in k["gate"]["rows"]), k["gate"]["pass"]))
        vi = eng.verify_integrity()
        rows.append(R(g, "Verify integrity (chain + %d seal(s))" % len(vi["seals"]), "no breaks", "ok" if vi["ok"] else vi, vi["ok"]))
        rows.append(R(g, "Human steps by the allowed persona / SoD", "all", self.sod_audit(eng), self.sod_audit(eng) == "all"))
        rows.append(R(g, "No cited record dated after its scenario time", "none", self.pit_violations(s) or "none", not self.pit_violations(s)))
        rows.append(R(g, "No agent errors", "none", "; ".join(eng.errors + [c["agent_error"] for c in cases if c.get("agent_error")]) or "none",
                      not eng.errors and not any(c.get("agent_error") for c in cases)))
        return rows

    def case_rows(self, g, x, c, s):
        rows = []
        name = "%s %s %s" % (x["case"], x["type"], c["case_id"])
        rec = c.get("recommendation") or {}
        tolm = self.tol["projection"] if x.get("amount_kind") == "projected_exposure" else self.tol["money"]

        def chk(label, exp, got, ok=None):
            rows.append(R(g, "%s: %s" % (name, label), exp, got, (exp == got) if ok is None else ok))
        if "opened" in x:
            chk("opened", x["opened"], c["opened_at"])
        if "rules" in x:
            chk("rules", ",".join(x["rules"]), ",".join(c.get("rules_initial") or []))
        if "rules_final" in x:
            chk("rules after ID match confirmed", ",".join(x["rules_final"]), ",".join(c.get("rules_fired") or []))
        if "r4" in x:
            chk("R4 severity", x["r4"], (c.get("reconciliation") or {}).get("severity"))
        if "cause" in x:
            chk("cause", x["cause"], rec.get("probable_cause"))
        if "s_codes" in x:
            chk("S-codes", ",".join(x["s_codes"]), ",".join(rec.get("s_codes") or []))
        if "option_initial" in x:
            chk("first recommendation", x["option_initial"], c.get("option_initial"))
        if "option" in x:
            chk("option", x["option"], rec.get("option"))
        if "label_contains" in x:
            chk("label", "…%s…" % x["label_contains"], rec.get("label"), x["label_contains"] in (rec.get("label") or ""))
        if "amount" in x:
            chk("amount", "%.2f" % x["amount"], "%.2f" % c["amount"], money_ok(c["amount"], x["amount"], tolm) and money_ok(rec.get("amount"), x["amount"], tolm))
        if "before" in x:
            calc = rec.get("calculation") or {}
            chk("draft before → after", "%.2f → %.2f" % (x["before"], x["after"]), "%s → %s" % (calc.get("before"), calc.get("after")),
                money_ok(calc.get("before"), x["before"], 0.01) and money_ok(calc.get("after"), x["after"], 0.01))
        if "priority" in x:
            chk("priority", x["priority"], c["priority"])
        if "triage" in x:
            chk("triage", x["triage"], (c.get("triage") or {}).get("colour"))
        if "confidence" in x:
            chk("confidence", "%.2f" % x["confidence"], "%.2f" % rec.get("confidence", -1), abs(rec.get("confidence", -1) - x["confidence"]) < 1e-9)
        if "state" in x:
            chk("final state", x["state"], c["state"])
        if "bills" in x:
            chk("bills affected", x["bills"], len(c["bill_ids"]))
        if x.get("linked"):
            link = s.state["cases"].get(c.get("linked_case_id")) or {}
            chk("linked to the decided, sealed case", "APPROVED (sealed)", "%s%s" % (link.get("state"), " (sealed)" if link.get("seal") else ""))
        cited = self.cited_records(s, c)
        if x.get("must_cite"):
            miss = [r for r in x["must_cite"] if r not in cited]
            chk("cites", ", ".join(x["must_cite"]), "missing " + ", ".join(miss) if miss else "all cited", not miss)
        if x.get("must_not_cite"):
            bad = [r for r in x["must_not_cite"] if r in cited]
            chk("does not cite", ", ".join(x["must_not_cite"]), ", ".join(bad) or "not cited", not bad)
        for mid, st in (x.get("msg_status") or {}).items():
            f = next((f for f in s.evidence.for_case(c["case_id"]) if f["record_id"] == mid and f.get("kind", "record") == "record"), None)
            chk("%s status at scenario time" % mid, st, (f or {}).get("fields", {}).get("status"))
        if x.get("no_pb3_corroboration"):
            chk("no PB3 corroboration claimed", "none", "; ".join(s2 for s2 in rec.get("signals", []) if "Near-match" in s2) or "none")
        return rows

    def cited_records(self, s, c):
        ids = set()
        for e in (c.get("reconciliation") or {}).get("explanations", []):
            ids.update(e["fact_ids"])
        ids.update((c.get("recommendation") or {}).get("fact_ids", []))
        out = set()
        for fid in ids:
            f = s.evidence.by_id.get(fid)
            if f:
                out.add(f["record_id"])
                if f["file"] == "gl_journal_lines.csv":
                    out.add(f["fields"].get("source_ref"))
                for v in (f.get("fields") or {}).values():
                    if isinstance(v, str) and v.startswith(("JE-", "BL-")):
                        out.add(v)
        return out

    def pit_violations(self, s):
        bad = []
        for f in s.evidence.facts:
            if f.get("kind", "record") != "record":
                continue
            for k in DATE_FIELDS:
                v = (f.get("fields") or {}).get(k)
                if v and v[:16] > f["as_of"]:
                    bad.append("%s %s %s=%s > %s" % (f["fact_id"], f["record_id"], k, v, f["as_of"]))
        return "; ".join(bad[:3])

    def sod_audit(self, eng):
        persona_of_action = {"TRIAGE_CONFIRMED": "CSR", "MATCH_CONFIRMED": "CSR", "MATCH_REJECTED": "CSR", "DECISION_RECORDED": "DRI",
                             "OVERRIDE_RECORDED": "DRI", "CASE_SEALED": "DRI", "CASE_CANCELLED": "DRI"}
        bad = [e for e in eng.audit.events if e["action"] in persona_of_action and (e.get("actor_persona") != persona_of_action[e["action"]] or e["actor_type"] != "USER")]
        return "; ".join("%s by %s" % (e["action"], e["actor_id"]) for e in bad[:3]) if bad else "all"

    def send_disabled(self, g, s, codes):
        rows = []
        cand = s.backscan_candidates()
        for code in codes:
            accs = s.ds.accounts_by_code.get(code, [])
            r = next((r for r in cand if r["account_id"] in accs), None)
            rows.append(R(g, "%s cannot be sent to the workflow" % code, "REVIEW, disabled: Out of scope – revenue protection",
                          "%s, %s: %s" % (r and r["kind"], "disabled" if r and not r["eligible"] else "eligible", r and r["reason"]),
                          bool(r) and not r["eligible"] and r["reason"] == "Out of scope – revenue protection"))
            try:
                s.send_backscan(self.cfg.user("u.csr"), [r["wo_id"]] if r else ["?"])
                rows.append(R(g, "Sending %s is refused" % code, "refused", "accepted", False))
            except AppError as e:
                rows.append(R(g, "Sending %s is refused" % code, "refused", str(e), True))
        return rows

    # ---- CSR account lookup ---------------------------------------------------------------------------
    async def lookups(self):
        g = "CSR account lookup"
        ex = self.ex["lookups"]
        eng = self.engine()
        s = eng.session(ex["dataset"], ex["scenario"])
        s.clock = ex["clock"]
        U = self.cfg.user
        rows = []
        for x in ex["rows"]:
            try:
                r = s.lookup(U("u.csr"), x["account_id"])
            except AppError as e:
                r = {"status": "ERROR", "message": str(e)}
            await s.wait_idle()
            c = s.state["cases"].get(r.get("case_id")) if r.get("case_id") else None
            got = [r["status"], c and c["state"], c and c["type"], ",".join((c or {}).get("rules_fired") or []), ((c or {}).get("recommendation") or {}).get("option")]
            want = [x["status"], x.get("state"), x.get("type"), ",".join(x.get("rules") or []), x.get("option")]
            ok = got[0] == want[0] and all(w is None or w == "" or w == gv for w, gv in zip(want[1:], got[1:]))
            rows.append(R(g, "%s %s" % (x.get("case", ""), x["account_id"]), " / ".join(str(w) for w in want if w), " / ".join(str(v) for v in got if v), ok))
        no_issue = [c for c in s.state["cases"].values() if c["state"] == "NO_ISSUE"]
        rows.append(R(g, "No-issue lookups are not cases (not in KPIs or My Work)", "excluded", "%d case(s) counted" % kpis(s)["cases"],
                      kpis(s)["cases"] == sum(1 for c in s.state["cases"].values() if c["state"] != "NO_ISSUE") and no_issue))
        rows.append(R(g, "No-issue lookups still show the agents' findings", "facts + reconciliation",
                      ", ".join("%s %d facts" % (c["case_id"], len(s.evidence.for_case(c["case_id"]))) for c in no_issue),
                      all(s.evidence.for_case(c["case_id"]) and c.get("reconciliation") for c in no_issue)))
        hit = s.lookup(U("u.csr"), "8800010101")
        rows.append(R(g, "Looking up an account with an existing case returns that case", "EXISTING", "%s %s" % (hit["status"], hit.get("case_id")), hit["status"] == "EXISTING"))
        try:
            s.lookup(U("u.dri"), "8800010214")
            got = "allowed"
        except PermissionDenied:
            got = "403"
        rows.append(R(g, "The DRI cannot use the lookup (CSR intake)", "403", got, got == "403"))
        c = next(c for c in s.state["cases"].values() if c["state"] == "TRIAGE" and c["match"]["status"] == "NONE")
        s.act(U("u.csr"), c["case_id"], "CONFIRM_TRIAGE", {})
        s.act(U("u.dri"), c["case_id"], "DECIDE", {"kind": "APPROVE_RECOMMENDED"})
        rows.append(R(g, "A lookup case goes through triage to the DRI's decision", "APPROVED, sealed", "%s%s" % (c["state"], ", sealed" if c.get("seal") else ""),
                      c["state"] == "APPROVED" and c.get("seal")))
        return rows

    # ---- determinism -----------------------------------------------------------------------------------
    async def determinism(self):
        g = "Determinism"
        snaps = []
        for _ in range(2):
            eng, s = await self.play("dataset_A_with_errors", "PREVENTION_REPLAY")
            snaps.append([(c["case_id"], c["amount"], dict(c.get("recommendation") or {}), c.get("reconciliation"), c["state"])
                          for c in sorted(s.state["cases"].values(), key=lambda c: c["case_id"])])
        same = snaps[0] == snaps[1]
        return [R(g, "Same scenario twice: identical agent outputs, decisions and amounts", "identical", "identical" if same else "different", same)]

    # ---- SoD / permissions ---------------------------------------------------------------------------------
    async def sod(self):
        g = "Permissions & segregation of duties (server-enforced)"
        rows = []
        eng = self.engine()
        s = eng.session("dataset_A_with_errors", "DISPUTE_REPLAY")
        s.play("u.csr", target="2026-11-04 09:40")
        while s.state["timeline"]["playing"] or not s.idle():
            await asyncio.sleep(0)
        await s.wait_idle()
        case = next(iter(s.state["cases"].values()))
        U = self.cfg.user

        def attempt(label, user, action, payload=None, expect="403"):
            before = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
            try:
                s.act(U(user), case["case_id"], action, payload or {})
                got = "allowed"
            except PermissionDenied:
                got = "403"
            except AppError as e:
                got = "refused: %s" % str(e)[:80]
            logged = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED") > before
            ok = (got == "403" and logged) if expect == "403" else got.startswith(expect)
            rows.append(R(g, label, expect + (" + PERMISSION_DENIED logged" if expect == "403" else ""), got + (" + logged" if logged else ""), ok))
        attempt("DRI cannot confirm triage (CSR step)", "u.dri", "CONFIRM_TRIAGE")
        attempt("CSR confirms triage", "u.csr", "CONFIRM_TRIAGE", expect="allowed")
        attempt("CSR cannot decide (SoD-2)", "u.csr", "DECIDE", {"kind": "APPROVE_RECOMMENDED"})
        attempt("Override without a reason is refused (SoD-4)", "u.dri", "DECIDE", {"kind": "RELEASE_AS_IS"}, expect="refused")
        attempt("Another option without a reason is refused (SoD-4)", "u.dri", "DECIDE", {"kind": "CHOOSE_OTHER", "option": "SUPPRESS"}, expect="refused")
        attempt("DRI approves Prorate - final", "u.dri", "DECIDE", {"kind": "APPROVE_RECOMMENDED"}, expect="allowed")
        rows.append(R(g, "The decision ends and seals the case", "APPROVED, sealed", "%s%s" % (case["state"], ", sealed" if case.get("seal") else ""),
                      case["state"] == "APPROVED" and bool(case.get("seal"))))
        attempt("A decided case is read-only", "u.dri", "DECIDE", {"kind": "CANCEL_FALSE_POSITIVE", "reason": "x"}, expect="refused")
        rows.append(R(g, "There is no Finance persona", "none", ", ".join(u["user_id"] for u in self.cfg.users() if u["persona"] == "FINANCE") or "none",
                      not any(u["persona"] == "FINANCE" for u in self.cfg.users())))
        # rejection paths and Flag
        eng2 = self.engine()
        s2 = eng2.session("dataset_A_with_errors", "BACKSCAN_2026-11-10")
        s2.send_backscan(U("u.csr"), ["WO-104256", "WO-104277"])
        await s2.wait_idle()
        c_flag = next(c for c in s2.state["cases"].values() if c["match"]["status"] == "PENDING")
        c_other = next(c for c in s2.state["cases"].values() if c is not c_flag)
        s2.act(U("u.csr"), c_flag["case_id"], "REJECT_MATCH", {"reason": "self-test"})
        s2.act(U("u.csr"), c_flag["case_id"], "CONFIRM_TRIAGE", {})
        try:
            s2.act(U("u.dri"), c_flag["case_id"], "DECIDE", {"kind": "APPROVE_RECOMMENDED"})
            got = "allowed"
        except AppError as e:
            got = "refused: %s" % e
        rows.append(R(g, "A Flag recommendation cannot be approved as an action", "refused", got, got.startswith("refused") and "Flag" in got))
        s2.act(U("u.dri"), c_flag["case_id"], "DECIDE", {"kind": "RELEASE_AS_IS", "reason": "self-test: match rejected"})
        rows.append(R(g, "Release as-is ends the case as REJECTED", "REJECTED, sealed", "%s%s" % (c_flag["state"], ", sealed" if c_flag.get("seal") else ""),
                      c_flag["state"] == "REJECTED" and bool(c_flag.get("seal"))))
        s2.act(U("u.csr"), c_other["case_id"], "CONFIRM_TRIAGE", {})
        s2.act(U("u.dri"), c_other["case_id"], "DECIDE", {"kind": "CANCEL_FALSE_POSITIVE", "reason": "self-test"})
        rows.append(R(g, "Cancel as false positive ends the case as REJECTED", "REJECTED", c_other["state"], c_other["state"] == "REJECTED"))
        k = kpis(s2)
        rows.append(R(g, "Rejected cases count no money", "$0.00 remediated", "%.2f" % k["remediated"], k["remediated"] == 0))
        return rows

    # ---- guardrails ------------------------------------------------------------------------------------------
    async def guardrails(self):
        g = "Agent guardrails"
        rows = []
        eng = self.engine()
        s = eng.session("dataset_A_with_errors", "BACKSCAN_2026-11-10")
        s.send_backscan(self.cfg.user("u.csr"), ["WO-104277"])
        await s.wait_idle()
        case = next(iter(s.state["cases"].values()))
        POLICIES["agent.selftest_loop"] = lambda m, t, b: {"tool_calls": [call("get_crm_cases", len(m), account_id=case["account_id"])]}
        POLICIES["agent.selftest_cite"] = lambda m, t, b: {"final": {"lane": "X", "fact_ids": ["F-9999"], "records": 1}}
        POLICIES["agent.selftest_write"] = lambda m, t, b: {"tool_calls": [call("STOP_SA", 1, sa_id=case["sa_id"], stop_date="2026-01-01")]}
        for label, agent, tools in (("More than 12 tool calls is stopped", "agent.selftest_loop", ["get_crm_cases"]),
                                    ("A citation of an unknown fact is rejected", "agent.selftest_cite", []),
                                    ("No agent can write to the source systems or the sandbox (no write tools)", "agent.selftest_write", ["STOP_SA"])):
            try:
                await run_agent(s, case, agent, {}, tools, RetrievalOut)
                got = "allowed"
            except GuardrailError as e:
                got = "stopped: %s" % str(e)[:70]
            rows.append(R(g, label, "stopped", got, got.startswith("stopped")))
        n = sum(1 for e in eng.audit.events if e["action"] == "AGENT_TOOL_CALL")
        rows.append(R(g, "Every tool call is logged as AGENT_TOOL_CALL", "> 0", n, n > 0))
        rows.append(R(g, "Sandbox stays empty (nothing is executed)", "0 overlays", "%d overlays" % len(s.sandbox.overlays), not s.sandbox.overlays))
        rows.append(R(g, "Similar past cases are context only", "3 results", len((case.get("recommendation") or {}).get("similar_cases", [])),
                      len((case.get("recommendation") or {}).get("similar_cases", [])) == 3))
        return rows

    async def stale_feed(self):
        g = "Freshness: stale MDM feed"
        eng = self.engine()
        s = eng.session("dataset_A_with_errors", "PREVENTION_REPLAY")
        s.set_toggle("u.csr", "stale_mdm", True)
        await s.run_to_end()
        blocked = [e for e in eng.audit.events if e["action"] == "BILL_RUN_BLOCKED_STALE_DATA"]
        ev = next(e for e in s.state["events"] if e["type"] == "BILL_READY")
        rows = [R(g, "BILL_READY is blocked and logged", "BILL_RUN_BLOCKED_STALE_DATA", "%s (%s)" % (blocked[0]["object_id"], ev["status"]) if blocked else "not blocked", bool(blocked) and ev["status"] == "BLOCKED"),
                R(g, "No drafts were evaluated", "0 BILL_PASSED / BILL_HELD", sum(1 for e in eng.audit.events if e["action"] in ("BILL_PASSED", "BILL_HELD")),
                  not any(e["action"] in ("BILL_PASSED", "BILL_HELD") for e in eng.audit.events)),
                R(g, "Run-level banner shown", "banner", (s.state.get("banner") or {}).get("text", "-")[:60], (s.state.get("banner") or {}).get("kind") == "stale")]
        s.set_toggle("u.csr", "stale_mdm", False)
        await s.step("u.csr")
        done = [e for e in eng.audit.events if e["action"] == "BILL_RUN_COMPLETED"]
        rows.append(R(g, "Toggle off + step processes the run", "BILL_RUN_COMPLETED", done[0]["object_id"] if done else "not processed", bool(done)))
        return rows

    async def seal_tamper(self):
        g = "Audit chain and case seals"
        eng, s = await self.play("dataset_A_with_errors", "DISPUTE_REPLAY")
        ok1 = eng.verify_integrity()["ok"]
        case = next(c for c in s.state["cases"].values() if c["type"] == "REMEDIATE")
        f = s.evidence.for_case(case["case_id"])[0]
        old = f["hash"]
        f["hash"] = "0" * 64
        vi = eng.verify_integrity()
        f["hash"] = old
        ev = eng.audit.events[5]
        oldr = ev.get("reason")
        ev["reason"] = "tampered"
        ch = eng.audit.verify_chain()
        ev["reason"] = oldr
        return [R(g, "Verify integrity on an untouched run", "ok", ok1, ok1),
                R(g, "A changed fact breaks the case seal", "broken seal reported", (vi.get("first_broken_seal") or {}).get("case_id"), not vi["ok"] and vi["first_broken_seal"]),
                R(g, "A changed event breaks the hash chain at that event", "line 6", (ch.get("first_broken") or {}).get("line"), not ch["ok"] and ch["first_broken"]["line"] == 6)]

    def integrity_sources(self):
        g = "Source CSVs unchanged"
        rows = []
        for d in self.registry.list():
            ds = self.registry.get(d["id"])
            now = ds.source_hashes()
            before = (self.hashes_before or {}).get(ds.id)
            ok = before is None or before == now
            rows.append(R(g, "%s: %d files byte-for-byte unchanged (SHA-256)" % (ds.name, len(now)), "unchanged since start", "unchanged" if ok else "CHANGED", ok))
        return rows
