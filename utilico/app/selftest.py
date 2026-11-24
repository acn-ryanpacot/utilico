"""Self-test. Every check runs in an isolated, in-memory engine (agent latency 0, own audit log); nothing touches
state/ or the source CSVs. Cases come from the back-scan and CSR lookups; the process ends at the DRI's decision."""
import asyncio
import copy
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
        self.cfg = Config(overrides={"agent.json": {"mode": "mock", "step_latency_ms": 0, "max_tool_calls": 12}})
        self.ex = self.cfg.expected()
        self.tol = self.ex["tolerance"]
        self.hashes_before = source_hashes_before
        self.U = self.cfg.user

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
            rows += await self.backscan(ds_id)
        rows += await self.lookups()
        rows += await self.determinism()
        rows += await self.sod()
        rows += await self.guardrails()
        rows += await self.seal_tamper()
        rows += self.integrity_sources()
        rows.append(R("Offline", "No outbound connection attempted during the self-test", 0, len(guard.blocked) - blocked_before,
                      len(guard.blocked) == blocked_before))
        return rows

    # ---- helpers ---------------------------------------------------------------------------------------
    async def drive(self, s):
        """Test driver (there is no autopilot in the app): CSR confirms matches and triage, the DRI approves the
        recommendation (a Flag is released as-is)."""
        for _ in range(500):
            await s.wait_idle()
            acted = False
            for c in sorted(s.state["cases"].values(), key=lambda c: c["case_id"]):
                if c["state"] == "TRIAGE":
                    if c["match"]["status"] == "PENDING":
                        s.act(self.U("u.csr"), c["case_id"], "CONFIRM_MATCH", {"note": "self-test: near-match evidence reviewed"})
                    else:
                        s.act(self.U("u.csr"), c["case_id"], "CONFIRM_TRIAGE", {})
                    acted = True
                elif c["state"] == "AWAITING_DRI":
                    if (c.get("recommendation") or {}).get("option") == "FLAG":
                        s.act(self.U("u.dri"), c["case_id"], "DECIDE", {"kind": "RELEASE_AS_IS", "reason": "self-test: match not confirmed"})
                    else:
                        s.act(self.U("u.dri"), c["case_id"], "DECIDE", {"kind": "APPROVE_RECOMMENDED"})
                    acted = True
            if not acted and s.idle():
                return

    async def run_backscan(self, ds_id):
        eng = self.engine()
        s = eng.session(ds_id)
        s.send_backscan(self.U("u.csr"))
        await self.drive(s)
        return eng, s

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

    # ---- back-scan ---------------------------------------------------------------------------------------
    def expected_backscan(self, ds_id):
        e = copy.deepcopy(self.ex["backscan"][ds_id])
        if "cases_from" in e:
            cases = copy.deepcopy(self.ex["backscan"][e["cases_from"]]["cases"])
            e["cases"] = [x for x in cases if x["case"] in e["only"]]
        return e

    async def backscan(self, ds_id):
        g = "Back-scan → workflow - %s" % ds_id.split("_")[1]
        e = self.expected_backscan(ds_id)
        eng, s = await self.run_backscan(ds_id)
        codes = s.ds.case_codes
        cases = sorted(s.state["cases"].values(), key=lambda c: c["case_id"])
        rows = [R(g, "Cases opened", ", ".join(sorted("%s %s" % (x["case"], x["type"]) for x in e["cases"])),
                  ", ".join(sorted("%s %s" % (codes.get(c["account_id"]), c["type"]) for c in cases)),
                  sorted((x["case"], x["type"]) for x in e["cases"]) == sorted((codes.get(c["account_id"]), c["type"]) for c in cases))]
        for x in e["cases"]:
            c = next((c for c in cases if codes.get(c["account_id"]) == x["case"]), None)
            if not c:
                rows.append(R(g, "%s %s" % (x["case"], x["type"]), "case", "missing", False))
                continue
            rows += self.case_rows(g, x, c, s)
        for code in self.ex["never_cases"]:
            bad = [c["case_id"] for c in cases if codes.get(c["account_id"]) == code]
            if bad:
                rows.append(R(g, "No case for %s" % code, "none", ", ".join(bad), False))
        k = kpis(s)
        rows.append(R(g, "$ remediated (approved credits)", "%.2f" % e["remediated"], "%.2f" % k["remediated"], money_ok(k["remediated"], e["remediated"], self.tol["money"])))
        rows += self.send_disabled(g, s, e["send_disabled"])
        rows.append(R(g, "Every case ends at the DRI's decision and is sealed", "all APPROVED, sealed",
                      ", ".join("%s %s" % (c["case_id"], c["state"]) for c in cases if c["state"] not in ("APPROVED", "REJECTED") or not c.get("seal")) or "all",
                      all(c["state"] in ("APPROVED", "REJECTED") and c.get("seal") for c in cases)))
        rows.append(R(g, "Citation coverage (agent statements)", "100%", "-" if k["citation_coverage"] is None else "%.1f%%" % (k["citation_coverage"] * 100),
                      k["citation_coverage"] == 1.0))
        rows.append(R(g, "Day-30 gate", "all four pass", ", ".join("%s %s" % (r["name"], r["display"]) for r in k["gate"]["rows"]), k["gate"]["pass"]))
        vi = eng.verify_integrity()
        rows.append(R(g, "Verify integrity (chain + %d seal(s))" % len(vi["seals"]), "no breaks", "ok" if vi["ok"] else vi, vi["ok"]))
        rows.append(R(g, "Human steps by the allowed persona / SoD", "all", self.sod_audit(eng), self.sod_audit(eng) == "all"))
        rows.append(R(g, "No cited record dated after its as-of time", "none", self.pit_violations(s) or "none", not self.pit_violations(s)))
        rows.append(R(g, "No agent errors", "none", "; ".join(eng.errors + [c["agent_error"] for c in cases if c.get("agent_error")]) or "none",
                      not eng.errors and not any(c.get("agent_error") for c in cases)))
        return rows

    def case_rows(self, g, x, c, s):
        rows = []
        name = "%s %s %s" % (x["case"], x["type"], c["case_id"])
        rec = c.get("recommendation") or {}
        tolm = self.tol["projection"] if c["type"] == "ADVANCE" else self.tol["money"]

        def chk(label, exp, got, ok=None):
            rows.append(R(g, "%s: %s" % (name, label), exp, got, (exp == got) if ok is None else ok))
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
        if "amount" in x:
            chk("amount", "%.2f" % x["amount"], "%.2f" % c["amount"], money_ok(c["amount"], x["amount"], tolm))
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
        cited = self.cited_records(s, c)
        if x.get("must_cite"):
            miss = [r for r in x["must_cite"] if r not in cited]
            chk("cites", ", ".join(x["must_cite"]), "missing " + ", ".join(miss) if miss else "all cited", not miss)
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
        persona_of_action = {"TRIAGE_CONFIRMED": "CSR", "MATCH_CONFIRMED": "CSR", "MATCH_REJECTED": "CSR", "BACKSCAN_SENT_TO_WORKFLOW": "CSR",
                             "DECISION_RECORDED": "DRI", "OVERRIDE_RECORDED": "DRI", "CASE_SEALED": "DRI", "CASE_CANCELLED": "DRI"}
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
                s.send_backscan(self.U("u.csr"), [r["wo_id"]] if r else ["?"])
                rows.append(R(g, "Sending %s is refused" % code, "refused", "accepted", False))
            except AppError as e:
                rows.append(R(g, "Sending %s is refused" % code, "refused", str(e), True))
        return rows

    # ---- CSR account lookup ---------------------------------------------------------------------------
    async def lookups(self):
        g = "CSR account lookup (as-of date)"
        ex = self.ex["lookups"]
        eng = self.engine()
        s = eng.session(ex["dataset"])
        rows = []
        for x in ex["rows"]:
            try:
                r = s.lookup(self.U("u.csr"), x["account_id"], x["as_of"])
            except AppError as e:
                r = {"status": "ERROR", "message": str(e)}
            await s.wait_idle()
            c = s.state["cases"].get(r.get("case_id")) if r.get("case_id") else None
            rec = (c or {}).get("recommendation") or {}
            got = {"status": r["status"], "state": c and c["state"], "type": c and c["type"], "rules": ",".join((c or {}).get("rules_fired") or []) or None,
                   "option": rec.get("option"), "priority": c and c["priority"], "confidence": rec.get("confidence"), "as_of": c and c.get("as_of")}
            want = {"status": x["status"], "state": x.get("state"), "type": x.get("type"), "rules": ",".join(x.get("rules") or []) or None,
                    "option": x.get("option"), "priority": x.get("priority"), "confidence": x.get("confidence"),
                    "as_of": (x["as_of"] + " 23:59") if c else None}
            ok = all(want[k] is None or want[k] == got[k] for k in want)
            if x.get("amount") is not None:
                ok = ok and c is not None and money_ok(c["amount"], x["amount"], self.tol["projection"])
            label = "%s %s as of %s" % (x.get("case", ""), x["account_id"], x["as_of"])
            rows.append(R(g, label, " / ".join(str(v) for v in want.values() if v is not None) + (" / %.2f" % x["amount"] if x.get("amount") else ""),
                          " / ".join(str(v) for v in got.values() if v is not None) + (" / %.2f" % c["amount"] if c and x.get("amount") else ""), ok))
        no_issue = [c for c in s.state["cases"].values() if c["state"] == "NO_ISSUE"]
        k = kpis(s)
        rows.append(R(g, "No-issue lookups are not cases (KPIs, My Work)", "excluded", "%d case(s) counted" % k["cases"],
                      k["cases"] == sum(1 for c in s.state["cases"].values() if c["state"] != "NO_ISSUE") and no_issue))
        rows.append(R(g, "No-issue lookups still show the agents' findings", "facts + reconciliation",
                      ", ".join("%s %d facts" % (c["case_id"], len(s.evidence.for_case(c["case_id"]))) for c in no_issue),
                      all(s.evidence.for_case(c["case_id"]) and c.get("reconciliation") for c in no_issue)))
        rows.append(R(g, "Agents only see records up to the lookup's as-of date", "none later", self.pit_violations(s) or "none", not self.pit_violations(s)))
        hit = s.lookup(self.U("u.csr"), "8800010101", "2026-11-10")
        rows.append(R(g, "Looking up an account with an existing case returns that case", "EXISTING", "%s %s" % (hit["status"], hit.get("case_id")), hit["status"] == "EXISTING"))
        try:
            s.lookup(self.U("u.dri"), "8800010214", "2026-11-10")
            got = "allowed"
        except PermissionDenied:
            got = "403"
        rows.append(R(g, "The DRI cannot use the lookup (CSR intake)", "403", got, got == "403"))
        await self.drive(s)
        open_ = [c for c in s.state["cases"].values() if c["state"] not in ("APPROVED", "REJECTED", "NO_ISSUE")]
        rows.append(R(g, "Lookup cases go through triage to the DRI's decision", "all decided, sealed", ", ".join(c["case_id"] + " " + c["state"] for c in open_) or "all decided",
                      not open_ and all(c.get("seal") for c in s.state["cases"].values() if c["state"] in ("APPROVED", "REJECTED"))))
        ea = kpis(s)["exposure_addressed"]
        rows.append(R(g, "$ exposure addressed (approved ADVANCE lookups E2 + E7)", "%.2f" % (145.35 + 1553.84), "%.2f" % ea, money_ok(ea, 145.35 + 1553.84, 0.02)))
        return rows

    # ---- determinism -----------------------------------------------------------------------------------
    async def determinism(self):
        g = "Determinism"
        snaps = []
        for _ in range(2):
            eng, s = await self.run_backscan("dataset_A_with_errors")
            snaps.append([(c["case_id"], c["amount"], dict(c.get("recommendation") or {}), c.get("reconciliation"), c["state"])
                          for c in sorted(s.state["cases"].values(), key=lambda c: c["case_id"])])
        same = snaps[0] == snaps[1]
        return [R(g, "Same back-scan twice: identical agent outputs, decisions and amounts", "identical", "identical" if same else "different", same)]

    # ---- SoD / permissions ---------------------------------------------------------------------------------
    async def sod(self):
        g = "Permissions & segregation of duties (server-enforced)"
        rows = []
        eng = self.engine()
        s = eng.session("dataset_A_with_errors")
        U = self.U
        try:
            s.send_backscan(U("u.dri"))
            got = "allowed"
        except PermissionDenied:
            got = "403"
        rows.append(R(g, "The DRI cannot send back-scan findings (CSR intake)", "403", got, got == "403"))
        s.send_backscan(U("u.csr"), ["WO-104242", "WO-104256", "WO-104277"])
        await s.wait_idle()
        by = {s.ds.case_codes.get(c["account_id"]): c for c in s.state["cases"].values()}
        case = by["E1"]

        def attempt(label, user, action, payload=None, expect="403", c=case):
            before = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED")
            try:
                s.act(U(user), c["case_id"], action, payload or {})
                res = "allowed"
            except PermissionDenied:
                res = "403"
            except AppError as e:
                res = "refused: %s" % str(e)[:80]
            logged = sum(1 for e in eng.audit.events if e["action"] == "PERMISSION_DENIED") > before
            ok = (res == "403" and logged) if expect == "403" else res.startswith(expect)
            rows.append(R(g, label, expect + (" + PERMISSION_DENIED logged" if expect == "403" else ""), res + (" + logged" if logged else ""), ok))
        attempt("DRI cannot confirm triage (CSR step)", "u.dri", "CONFIRM_TRIAGE")
        attempt("CSR confirms triage", "u.csr", "CONFIRM_TRIAGE", expect="allowed")
        attempt("CSR cannot decide (SoD-2)", "u.csr", "DECIDE", {"kind": "APPROVE_RECOMMENDED"})
        attempt("Override without a reason is refused (SoD-4)", "u.dri", "DECIDE", {"kind": "RELEASE_AS_IS"}, expect="refused")
        attempt("Another option without a reason is refused (SoD-4)", "u.dri", "DECIDE", {"kind": "CHOOSE_OTHER", "option": "SUPPRESS"}, expect="refused")
        attempt("DRI approves Prorate - final", "u.dri", "DECIDE", {"kind": "APPROVE_RECOMMENDED"}, expect="allowed")
        rows.append(R(g, "The decision ends and seals the case", "APPROVED, sealed", "%s%s" % (case["state"], ", sealed" if case.get("seal") else ""),
                      case["state"] == "APPROVED" and bool(case.get("seal"))))
        attempt("A decided case is read-only", "u.dri", "DECIDE", {"kind": "CANCEL_FALSE_POSITIVE", "reason": "x"}, expect="refused")
        rows.append(R(g, "Two personas only (no Finance)", "u.csr, u.dri", ", ".join(sorted(u["user_id"] for u in self.cfg.users())),
                      sorted(u["user_id"] for u in self.cfg.users()) == ["u.csr", "u.dri"]))
        c_flag, c_other = by["E3"], by["E5"]
        s.act(U("u.csr"), c_flag["case_id"], "REJECT_MATCH", {"reason": "self-test"})
        s.act(U("u.csr"), c_flag["case_id"], "CONFIRM_TRIAGE", {})
        attempt("A Flag recommendation cannot be approved as an action", "u.dri", "DECIDE", {"kind": "APPROVE_RECOMMENDED"}, expect="refused: A Flag", c=c_flag)
        s.act(U("u.dri"), c_flag["case_id"], "DECIDE", {"kind": "RELEASE_AS_IS", "reason": "self-test: match rejected"})
        rows.append(R(g, "Release as-is ends the case as REJECTED", "REJECTED, sealed", "%s%s" % (c_flag["state"], ", sealed" if c_flag.get("seal") else ""),
                      c_flag["state"] == "REJECTED" and bool(c_flag.get("seal"))))
        s.act(U("u.csr"), c_other["case_id"], "CONFIRM_TRIAGE", {})
        s.act(U("u.dri"), c_other["case_id"], "DECIDE", {"kind": "CANCEL_FALSE_POSITIVE", "reason": "self-test"})
        rows.append(R(g, "Cancel as false positive ends the case as REJECTED", "REJECTED", c_other["state"], c_other["state"] == "REJECTED"))
        k = kpis(s)
        rows.append(R(g, "Only approved cases count money", "%.2f (E1 only)" % 1436.5, "%.2f" % k["remediated"], money_ok(k["remediated"], 1436.5, 0.01)))
        return rows

    # ---- guardrails ------------------------------------------------------------------------------------------
    async def guardrails(self):
        g = "Agent guardrails"
        rows = []
        eng = self.engine()
        s = eng.session("dataset_A_with_errors")
        s.send_backscan(self.U("u.csr"), ["WO-104277"])
        await s.wait_idle()
        case = next(iter(s.state["cases"].values()))
        POLICIES["agent.selftest_loop"] = lambda m, t, b: {"tool_calls": [call("get_crm_cases", len(m), account_id=case["account_id"])]}
        POLICIES["agent.selftest_cite"] = lambda m, t, b: {"final": {"lane": "X", "fact_ids": ["F-9999"], "records": 1}}
        POLICIES["agent.selftest_write"] = lambda m, t, b: {"tool_calls": [call("STOP_SA", 1, sa_id=case["sa_id"], stop_date="2026-01-01")]}
        for label, agent, tools in (("More than 12 tool calls is stopped", "agent.selftest_loop", ["get_crm_cases"]),
                                    ("A citation of an unknown fact is rejected", "agent.selftest_cite", []),
                                    ("No agent can write to the source systems (no write tools)", "agent.selftest_write", ["STOP_SA"])):
            try:
                with s.at(case["as_of"]):
                    await run_agent(s, case, agent, {}, tools, RetrievalOut)
                got = "allowed"
            except GuardrailError as e:
                got = "stopped: %s" % str(e)[:70]
            rows.append(R(g, label, "stopped", got, got.startswith("stopped")))
        n = sum(1 for e in eng.audit.events if e["action"] == "AGENT_TOOL_CALL")
        rows.append(R(g, "Every tool call is logged as AGENT_TOOL_CALL", "> 0", n, n > 0))
        rows.append(R(g, "Similar past cases are context only", "3 results", len((case.get("recommendation") or {}).get("similar_cases", [])),
                      len((case.get("recommendation") or {}).get("similar_cases", [])) == 3))
        return rows

    async def seal_tamper(self):
        g = "Audit chain and case seals"
        eng, s = await self.run_backscan("dataset_A_with_errors")
        ok1 = eng.verify_integrity()["ok"]
        case = next(c for c in s.state["cases"].values() if c.get("seal"))
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
