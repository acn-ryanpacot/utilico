"""
Utilico Energy - mock system extracts for POCs.
Generates Dataset A (with errors) and Dataset B (E2 and E5 corrected).
Deterministic: same output every run.
"""
import csv, os, random, shutil
from datetime import date, datetime, timedelta

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
READ_START, READ_END = date(2026, 8, 1), date(2026, 11, 1)
CYCLES = [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30)),
          (date(2026, 10, 1), date(2026, 10, 31))]
PERIOD_CLOSE = {"2026-08": "2026-09-05 18:00", "2026-09": "2026-10-06 18:00",
                "2026-10": "2026-11-06 18:00"}

TARIFFS = {
    "COM-GS2": dict(desc="Commercial general service - large", rate=0.16, fixed=4.50),
    "COM-GS1": dict(desc="Commercial general service - small", rate=0.17, fixed=2.10),
    "RES-1": dict(desc="Residential standard", rate=0.21, fixed=0.85),
}

def d(s): return datetime.strptime(s, "%Y-%m-%d").date()
def ts(s): return s  # timestamps kept as 'YYYY-MM-DD HH:MM' strings
def days(a, b):
    x = a
    while x <= b:
        yield x
        x += timedelta(days=1)
def money(x): return f"{x:.2f}"

FIRST = ["Alex", "Morgan", "Jamie", "Taylor", "Jordan", "Casey", "Riley", "Avery", "Quinn", "Harper",
         "Rowan", "Elliot", "Sasha", "Noor", "Mateo", "Ingrid", "Kofi", "Mei", "Tomas", "Leila",
         "Owen", "Freya", "Ravi", "Chloe", "Hugo"]
LAST = ["Bennett", "Clarke", "Dunmore", "Ellison", "Fairweather", "Grant", "Hollis", "Iverson", "Jansen",
        "Kowalski", "Lindqvist", "Marsh", "Nakamura", "Oduya", "Pemberton", "Quintero", "Rasmussen",
        "Sutherland", "Thorne", "Underwood", "Vance", "Whitlock", "Yates", "Zubiri", "Ashworth"]
BIZ = ["Fernhill Joinery", "Bluegate Pharmacy", "Ridgeway Auto Care", "Saltmarsh Cafe", "Oakline Fitness",
       "Peregrine Tailors", "Granite Row Books", "Lantern Bay Hotel", "Sable & Finch Design", "Thistle Vet Clinic"]
STREETS = ["Harbour Rd", "Mill Lane", "Station St", "Elm Grove", "Quarry Way", "Kings Rd", "Wharf St",
           "Beacon Ave", "Orchard Close", "Riverside Dr"]


def build(dataset):
    rng = random.Random(20261102)
    A = dataset == "A"
    T = {k: [] for k in ["accounts", "sas", "bills", "todos", "meters", "reads", "wos", "crm",
                         "gl", "intlog", "xwalk"]}
    counters = dict(wo=104200, msg=550000, todo=7700, case=300100, je=900000, bill=6100000, read=1)
    ACCTS = []

    def nid(k, step=1):
        counters[k] += step
        return counters[k]

    # ---------- account definitions ----------
    def acct(idx, name, cls, tariff, case=None, **kw):
        a = dict(idx=idx, name=name, cls=cls, tariff=tariff, case=case,
                 account_id=f"88000{10000 + idx}", sa_id=f"SA-{4400000 + idx}",
                 sp_id=f"SP-{710000 + idx}", premise=f"PRM-{idx:05d}", meter=f"MTR-{930000 + idx}",
                 address=f"{rng.randint(2, 240)} {rng.choice(STREETS)}, Utilico City",
                 status="ACTIVE", stop_date="", stop_processed="", mdm_state="CONNECTED")
        a.update(kw)
        ACCTS.append(a)
        return a

    # Background accounts
    bi = 0
    for i in range(25):
        acct(1 + i, f"{FIRST[i]} {LAST[(i * 7) % 25]}", "RES", "RES-1")
    for i in range(10):
        acct(40 + i, BIZ[i], "COM", "COM-GS1" if i % 2 else "COM-GS2")

    # Error & control cases
    E1 = acct(101, "Harbourline Logistics Ltd", "COM", "COM-GS2", "E1")
    E2 = acct(214, "Dana Whitfield", "RES", "RES-1", "E2")
    E3 = acct(417, "Copperleaf Studio", "COM", "COM-GS1", "E3")
    E4 = acct(322, "Luis Ferreira", "RES", "RES-1", "E4")
    E5 = acct(388, "Priya Natarajan", "RES", "RES-1", "E5")
    E6 = acct(455, "Brightwater Bakery", "COM", "COM-GS1", "E6")
    E7 = acct(512, "Northgate Dental", "COM", "COM-GS1", "E7")
    N1 = acct(601, "Eastside Florist", "COM", "COM-GS1", "N1")
    N2 = acct(602, "Gareth Owens", "RES", "RES-1", "N2")
    N3 = acct(603, "Rosa Delgado", "RES", "RES-1", "N3")
    N4 = acct(604, "Kestrel Print Co", "COM", "COM-GS1", "N4")
    N5 = acct(605, "Amara Osei", "RES", "RES-1", "N5")

    # ---------- daily reads ----------
    # profile: function(day) -> (kwh, quality, vee_rule) or None for random-normal
    def normal(a, day, est_prob=0.03):
        lo, hi = {"RES": (12, 35), "COM": (80, 600)}[a["cls"]]
        if a["tariff"] == "COM-GS2":
            lo, hi = 380, 600
        kwh = round(rng.uniform(lo, hi), 1)
        if rng.random() < est_prob:
            return (kwh, "ESTIMATED", "EST-HIST-AVG")
        return (kwh, "ACTUAL", "")

    special = {}  # (acct idx, date) -> tuple

    def setr(a, start, end, kwh, q, rule=""):
        for x in days(d(start), d(end)):
            special[(a["idx"], x)] = (kwh, q, rule)

    # E1: estimated 500/day Oct 15-31 and Nov 1
    setr(E1, "2026-10-15", "2026-11-01", 500.0, "ESTIMATED", "EST-HIST-AVG")
    # E2
    if A:
        setr(E2, "2026-08-10", "2026-11-01", 24.0, "ESTIMATED", "EST-HIST-AVG")
    else:
        setr(E2, "2026-08-10", "2026-11-01", 0.0, "ACTUAL")
    # E3
    setr(E3, "2026-09-23", "2026-11-01", 120.0, "ESTIMATED", "EST-HIST-AVG")
    # E4 window
    setr(E4, "2026-09-06", "2026-09-19", 18.0, "ESTIMATED", "EST-HIST-AVG")
    # E5
    if A:
        setr(E5, "2026-10-30", "2026-10-31", 30.0, "ESTIMATED", "EST-HIST-AVG")
    else:
        setr(E5, "2026-10-30", "2026-10-31", 0.0, "ACTUAL")
    setr(E5, "2026-11-01", "2026-11-01", 0.0, "ACTUAL")
    # E6 consumption after disconnect (actual)
    setr(E6, "2026-09-19", "2026-11-01", 15.0, "ACTUAL")
    # E7 disconnect day 1 Nov partial
    setr(E7, "2026-11-01", "2026-11-01", 41.2, "ACTUAL")
    # N1 zero after Sep 10
    setr(N1, "2026-09-11", "2026-11-01", 0.0, "ACTUAL")
    # N3 comms failure
    setr(N3, "2026-09-12", "2026-09-17", 21.0, "ESTIMATED", "EST-HIST-AVG")
    # N4 zero Nov 1
    setr(N4, "2026-11-01", "2026-11-01", 0.0, "ACTUAL")

    no_random_est = {E1["idx"], E2["idx"], E3["idx"], E4["idx"], E5["idx"], E6["idx"], E7["idx"],
                     N1["idx"], N2["idx"], N4["idx"], N5["idx"]}
    readmap = {}
    for a in ACCTS:
        for x in days(READ_START, READ_END):
            r = special.get((a["idx"], x))
            if r is None:
                r = normal(a, x, 0 if a["idx"] in no_random_est or a["case"] == "N3" else 0.03)
            readmap[(a["idx"], x)] = r
            T["reads"].append(dict(read_id=f"RD-{counters['read']:07d}", mdm_meter_id=a["meter"],
                                   read_date=x.isoformat(), kwh=f"{r[0]:.1f}", read_quality=r[1],
                                   vee_rule=r[2]))
            counters["read"] += 1

    # ---------- per-case status / work orders / integration ----------
    def wo(a, wtype, sub, status, completed, notes, premise=None, int_status="SUCCESS",
           processed=None, retries=0, err="", tech=None):
        wid = f"WO-{nid('wo', 7)}"
        T["wos"].append(dict(wo_id=wid, oms_premise_id=premise or a["premise"], wo_type=wtype,
                             wo_subtype=sub, status=status, created_at=ts(completed[:10] + " 07:30") if status != "OPEN" else completed,
                             completed_at=completed if status == "COMPLETED" else "",
                             technician_id=tech or f"TECH-{rng.randint(201, 248)}", notes=notes))
        if status == "COMPLETED":
            T["intlog"].append(dict(msg_id=f"MSG-{nid('msg', 3)}", source_system="OMS", target_system="CCB",
                                    message_type="FIELD_ACTIVITY_COMPLETE", wo_id=wid,
                                    oms_premise_id=premise or a["premise"], received_at=completed,
                                    processed_at=processed or "", status=int_status,
                                    retry_count=retries, error_message=err))
        return wid

    def plus(t, minutes):
        return (datetime.strptime(t, "%Y-%m-%d %H:%M") + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")

    # Background routine work orders
    for a in ACCTS[:35]:
        if rng.random() < 0.35:
            dd = READ_START + timedelta(days=rng.randint(3, 85))
            t = f"{dd.isoformat()} {rng.randint(8, 16):02d}:{rng.choice(['05', '20', '35', '50'])}"
            wo(a, rng.choice(["METER_INSPECTION", "METER_INSPECTION", "SITE_SAFETY_CHECK"]), "ROUTINE",
               "COMPLETED", t, "Routine visit. No issues found.", processed=plus(t, rng.randint(2, 15)))

    # E1
    t = "2026-10-14 10:42"
    E1_wo = wo(E1, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t,
               "Customer relocating operations. Supply isolated at meter, meter locked, seal S-88231 applied. "
               "Disconnect confirmed with site manager.", int_status="ERROR", processed=plus(t, 95), retries=3,
               err="CC&B rejected Stop SA: SA has bill segment in pending state; retry limit reached",
               tech="TECH-217")
    T["todos"].append(dict(todo_id=f"TD-{nid('todo')}", created_at=plus(t, 96), todo_type="FA_UPLOAD_ERROR",
                           related_entity=E1_wo, sa_id=E1["sa_id"],
                           message="Field activity completion upload failed - Stop SA not processed",
                           priority="NORMAL", status="OPEN", assigned_to=""))

    # E2
    t = "2026-08-09 16:05"
    if A:
        wo(E2, "DISCONNECT", "MOVE_OUT", "COMPLETED", t,
           "Tenant move-out. Supply isolated, meter locked.", int_status="SKIPPED", processed=plus(t, 4),
           err="No CC&B mapping configured for wo_subtype MOVE_OUT; message discarded")
    else:
        wo(E2, "DISCONNECT", "MOVE_OUT", "COMPLETED", t,
           "Tenant move-out. Supply isolated, meter locked.", processed=plus(t, 15))
        E2.update(status="STOPPED", stop_date="2026-08-09", stop_processed=plus(t, 15), mdm_state="DISCONNECTED")

    # E3 (premise typo in OMS)
    t = "2026-09-22 13:10"
    wo(E3, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t,
       "Studio closing. Supply isolated, meter locked.", premise="PRM-0O417", int_status="ERROR",
       processed=plus(t, 2), retries=0, err="Premise PRM-0O417 not found in ID cross-reference")

    # E4 non-pay + reconnect
    t = "2026-09-05 09:30"
    wo(E4, "DISCONNECT", "NONPAY", "COMPLETED", t, "Non-payment disconnect per collections order.",
       processed=plus(t, 12))
    t2 = "2026-09-20 08:15"
    wo(E4, "RECONNECT", "PAYMENT_RECEIVED", "COMPLETED", t2, "Payment confirmed; supply restored.",
       processed=plus(t2, 9))

    # E5 timing race
    t = "2026-10-29 18:40"
    if A:
        wo(E5, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Customer request - property sold. Supply isolated.",
           int_status="SUCCESS", processed="2026-11-03 06:00",
           err="Delivered from retry queue after CC&B maintenance outage")
        E5.update(status="STOPPED", stop_date="2026-10-29", stop_processed="2026-11-03 06:00", mdm_state="DISCONNECTED")
    else:
        wo(E5, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Customer request - property sold. Supply isolated.",
           processed="2026-10-30 03:00")
        E5.update(status="STOPPED", stop_date="2026-10-29", stop_processed="2026-10-30 03:00", mdm_state="DISCONNECTED")

    # E6 tamper
    t = "2026-09-18 11:00"
    wo(E6, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Business closed. Supply isolated, meter locked.",
       processed=plus(t, 10))
    E6.update(status="STOPPED", stop_date="2026-09-18", stop_processed=plus(t, 10), mdm_state="DISCONNECTED")

    # E7 recent
    t = "2026-11-01 14:20"
    E7_wo = wo(E7, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Practice relocated. Supply isolated, meter locked.",
               int_status="ERROR", processed=plus(t, 30), retries=3, err="Timeout calling CC&B inbound service; retry limit reached")
    T["todos"].append(dict(todo_id=f"TD-{nid('todo')}", created_at=plus(t, 31), todo_type="FA_UPLOAD_ERROR",
                           related_entity=E7_wo, sa_id=E7["sa_id"],
                           message="Field activity completion upload failed - Stop SA not processed",
                           priority="NORMAL", status="OPEN", assigned_to=""))

    # Controls
    t = "2026-09-10 12:00"
    wo(N1, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Shop closed. Supply isolated.", processed=plus(t, 8))
    N1.update(status="STOPPED", stop_date="2026-09-10", stop_processed=plus(t, 8), mdm_state="DISCONNECTED")
    T["wos"].append(dict(wo_id=f"WO-{nid('wo', 7)}", oms_premise_id=N2["premise"], wo_type="DISCONNECT",
                         wo_subtype="STOP_SERVICE", status="CANCELLED", created_at="2026-09-02 09:10",
                         completed_at="", technician_id="", notes="Cancelled - customer decided to stay."))
    t = "2026-10-31 23:10"
    wo(N4, "DISCONNECT", "STOP_SERVICE", "COMPLETED", t, "Lease ended. Supply isolated.", processed=plus(t, 45))
    N4.update(status="STOPPED", stop_date="2026-10-31", stop_processed=plus(t, 45), mdm_state="DISCONNECTED")
    t = "2026-09-24 10:15"
    wo(N5, "METER_EXCHANGE", "AGED_METER", "COMPLETED", t, "Aged meter replaced; reads continuous.", processed=plus(t, 6))

    # ---------- CC&B tables ----------
    for a in ACCTS:
        T["accounts"].append(dict(account_id=a["account_id"], customer_name=a["name"], customer_class=a["cls"],
                                  mailing_address=a["address"], account_status="ACTIVE",
                                  created_date=f"20{rng.randint(15, 24)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"))
        T["sas"].append(dict(sa_id=a["sa_id"], account_id=a["account_id"], ccb_sp_id=a["sp_id"],
                             premise_address=a["address"], tariff_code=a["tariff"], sa_status=a["status"],
                             start_date="2024-01-01" if a["idx"] < 100 else "2023-06-01",
                             stop_date=a["stop_date"], stop_processed_at=a["stop_processed"]))
        T["meters"].append(dict(mdm_meter_id=a["meter"], mdm_sp_id=f"MSP-{a['sp_id'][3:]}",
                                meter_type="AMI" if a["cls"] == "COM" or a["idx"] % 3 else "AMR",
                                connection_state=a["mdm_state"],
                                connection_state_updated_at=a["stop_processed"] or "2024-01-01 00:00"))
        T["xwalk"].append(dict(oms_premise_id=a["premise"], ccb_sp_id=a["sp_id"], mdm_meter_id=a["meter"]))

    # Bills
    def bill(a, start, end, run_at, status="FINAL", btype="REGULAR"):
        tf = TARIFFS[a["tariff"]]
        rs = [readmap[(a["idx"], x)] for x in days(start, end)]
        kwh = round(sum(r[0] for r in rs), 1)
        q = {r[1] for r in rs}
        qual = "ACTUAL" if q == {"ACTUAL"} else ("ESTIMATED" if q == {"ESTIMATED"} else "MIXED")
        nd = (end - start).days + 1
        cons = round(kwh * tf["rate"], 2)
        fixed = round(nd * tf["fixed"], 2)
        bid = f"BL-{a['sa_id'][3:]}-{start.strftime('%Y%m')}"
        T["bills"].append(dict(bill_id=bid, sa_id=a["sa_id"], bill_type=btype, period_start=start.isoformat(),
                               period_end=end.isoformat(), bill_run_at=run_at, days_billed=nd,
                               kwh_billed=f"{kwh:.1f}", usage_quality=qual, rate_per_kwh=f"{tf['rate']:.2f}",
                               consumption_amount=money(cons), fixed_daily_charge=f"{tf['fixed']:.2f}",
                               fixed_amount=money(fixed), total_amount=money(cons + fixed), bill_status=status))
        # GL posting
        period = end.strftime("%Y-%m")
        je = f"JE-{a['sa_id'][3:]}-{start.strftime('%Y%m')}"
        post = run_at
        for ln, (acc, name, dr, cr) in enumerate([
                ("1200", "Accounts receivable - customers", cons + fixed, 0),
                ("4010", "Energy revenue - consumption", 0, cons),
                ("4020", "Energy revenue - fixed charges", 0, fixed)], 1):
            T["gl"].append(dict(je_id=je, je_line=ln, posting_date=post[:10], accounting_period=period,
                                gl_account=acc, gl_account_name=name, debit=money(dr), credit=money(cr),
                                source_system="CCB", source_ref=bid, sa_id=a["sa_id"],
                                description=f"Billing {start.isoformat()} to {end.isoformat()}"))
        return bid

    runs = ["2026-09-02 01:00", "2026-10-02 01:00", "2026-11-02 01:00"]
    E1_bill = None
    for a in ACCTS:
        stop = d(a["stop_date"]) if a["stop_date"] else None
        for (cs, ce), run in zip(CYCLES, runs):
            if a is E5 and not A:
                if cs.month == 10:
                    bill(a, cs, d("2026-10-29"), run, btype="FINAL")
                    continue
            if a is E5 and A:
                bill(a, cs, ce, run)  # bill ran before stop processed
                continue
            if stop and stop < cs:
                continue
            if stop and stop <= ce:
                bill(a, cs, stop, run, btype="FINAL")
                continue
            st = "DISPUTED" if (a is E1 and cs.month == 10) else "FINAL"
            b = bill(a, cs, ce, run, status=st)
            if a is E1 and cs.month == 10:
                E1_bill = b

    # CC&B background To Dos (completed)
    for _ in range(6):
        a = rng.choice(ACCTS[:35])
        T["todos"].append(dict(todo_id=f"TD-{nid('todo')}", created_at=rng.choice(runs),
                               todo_type="BSEG_HIGH_USAGE", related_entity=a["sa_id"], sa_id=a["sa_id"],
                               message="Bill segment usage exceeds high-tolerance threshold",
                               priority="LOW", status="COMPLETE", assigned_to=f"BILLOPS-{rng.randint(1, 6)}"))

    # ---------- CRM ----------
    def case(a, opened, channel, cat, subj, status, closed, notes, linked=""):
        cid = f"CASE-{nid('case', 11)}"
        T["crm"].append(dict(case_id=cid, account_id=a["account_id"], customer_name=a["name"], opened_at=opened,
                             channel=channel, category=cat, subject=subj, status=status, closed_at=closed,
                             billing_hold_set="N", linked_case_id=linked, notes=notes))
        return cid

    for _ in range(14):
        a = rng.choice(ACCTS[:35])
        cat, subj, note = rng.choice([
            ("ACCOUNT_UPDATE", "Change of mailing address", "Address updated per customer request."),
            ("PAYMENT", "Payment arrangement request", "Set up 3-month payment arrangement."),
            ("BILLING_ENQUIRY", "High bill enquiry", "Explained seasonal usage; customer satisfied."),
            ("GENERAL_ENQUIRY", "Tariff information", "Sent tariff comparison by email.")])
        dd = READ_START + timedelta(days=rng.randint(2, 95))
        case(a, f"{dd.isoformat()} {rng.randint(9, 16):02d}:{rng.randint(0, 59):02d}",
             rng.choice(["PHONE", "EMAIL", "WEB"]), cat, subj, "CLOSED", f"{dd.isoformat()} 17:00", note)

    c1 = case(E1, "2026-10-16 11:05", "PHONE", "BILLING_ENQUIRY",
              "Confirm no billing after disconnect",
              "CLOSED", "2026-10-16 11:20",
              "Facilities manager called: supply was disconnected on 14 Oct as they have relocated. Asked us to "
              "make sure no further charges are billed after that date. Advised CSR would note the account. "
              "Note added. No further action.")
    c2 = case(E1, "2026-11-04 09:40", "EMAIL", "BILLING_DISPUTE",
              "Billed for full October after disconnection",
              "REOPENED", "",
              "04-Nov: Customer disputes October bill (full cycle). CSR checked CC&B: SA active, bill calculated "
              "correctly per tariff. Case closed 04-Nov 10:15 as bill valid. 05-Nov: Customer re-escalated to "
              "account manager, attached technician disconnect confirmation and photo of locked meter. Case reopened.",
              linked=c1)
    case(E1, "2026-11-09 15:30", "REGULATOR", "REGULATORY_COMPLAINT",
         "Formal complaint: billed for consumption after physical disconnection", "OPEN", "",
         "Regulator notified Utilico of formal complaint. Customer states: billing after disconnection is evidence "
         "that our systems do not communicate. Response due within regulatory deadline.", linked=c2)
    if A:
        case(E2, "2026-09-15 10:10", "MAIL", "ACCOUNT_UPDATE", "Returned mail - addressee moved", "CLOSED",
             "2026-09-15 10:30", "August bill returned by post marked 'moved away'. Note added to account.")
    else:
        nid('case', 11)  # keep later case IDs identical across datasets
    case(E4, "2026-09-02 14:00", "PHONE", "COLLECTIONS", "Final notice before disconnection", "CLOSED",
         "2026-09-02 14:10", "Customer advised of arrears and pending non-pay disconnection.")
    case(E4, "2026-09-19 16:45", "WEB", "PAYMENT", "Arrears paid in full", "CLOSED", "2026-09-19 17:00",
         "Payment received. Reconnection order raised.")
    case(E6, "2026-10-20 08:50", "PHONE", "GENERAL_ENQUIRY", "Neighbour report - lights on at closed premises",
         "CLOSED", "2026-10-20 09:05", "Caller reports lights on at former bakery premises. Logged for info only.")

    # ---------- GL periods ----------
    gl_periods = [dict(accounting_period=p, status="CLOSED", closed_at=c) for p, c in PERIOD_CLOSE.items()]
    gl_periods.append(dict(accounting_period="2026-11", status="OPEN", closed_at=""))

    return T, gl_periods


SCHEMAS = {
    "ccb_accounts.csv": ("accounts", ["account_id", "customer_name", "customer_class", "mailing_address", "account_status", "created_date"]),
    "ccb_service_agreements.csv": ("sas", ["sa_id", "account_id", "ccb_sp_id", "premise_address", "tariff_code", "sa_status", "start_date", "stop_date", "stop_processed_at"]),
    "ccb_bill_segments.csv": ("bills", ["bill_id", "sa_id", "bill_type", "period_start", "period_end", "bill_run_at", "days_billed", "kwh_billed", "usage_quality", "rate_per_kwh", "consumption_amount", "fixed_daily_charge", "fixed_amount", "total_amount", "bill_status"]),
    "ccb_todo_entries.csv": ("todos", ["todo_id", "created_at", "todo_type", "related_entity", "sa_id", "message", "priority", "status", "assigned_to"]),
    "mdm_meters.csv": ("meters", ["mdm_meter_id", "mdm_sp_id", "meter_type", "connection_state", "connection_state_updated_at"]),
    "mdm_daily_reads.csv": ("reads", ["read_id", "mdm_meter_id", "read_date", "kwh", "read_quality", "vee_rule"]),
    "oms_work_orders.csv": ("wos", ["wo_id", "oms_premise_id", "wo_type", "wo_subtype", "status", "created_at", "completed_at", "technician_id", "notes"]),
    "crm_cases.csv": ("crm", ["case_id", "account_id", "customer_name", "opened_at", "channel", "category", "subject", "status", "closed_at", "billing_hold_set", "linked_case_id", "notes"]),
    "gl_journal_lines.csv": ("gl", ["je_id", "je_line", "posting_date", "accounting_period", "gl_account", "gl_account_name", "debit", "credit", "source_system", "source_ref", "sa_id", "description"]),
    "integration_message_log.csv": ("intlog", ["msg_id", "source_system", "target_system", "message_type", "wo_id", "oms_premise_id", "received_at", "processed_at", "status", "retry_count", "error_message"]),
    "ref_id_crosswalk.csv": ("xwalk", ["oms_premise_id", "ccb_sp_id", "mdm_meter_id"]),
}

ANSWER = {
    "A": [
        ("E1", "Harbourline Logistics Ltd", "FINDING", "HIGH", "8500.0", "1360.00", "76.50", "1436.50", "C1/C2 - disconnect never reached CC&B (integration ERROR, To Do unworked)", "HOLD"),
        ("E2", "Dana Whitfield", "FINDING", "HIGH", "1992.0", "418.32", "70.55", "488.87", "C1 - MOVE_OUT subtype not mapped; message SKIPPED; 3 bills affected", "HOLD"),
        ("E3", "Copperleaf Studio", "UNMATCHED", "", "4680.0", "795.60", "81.90", "877.50", "C3 - OMS premise PRM-0O417 (letter O) does not match PRM-00417; amounts = potential exposure if linked", "UNMATCHED"),
        ("E4", "Luis Ferreira", "FINDING", "MEDIUM", "252.0", "52.92", "0.00", "52.92", "C4 + C5 - non-pay disconnect keeps SA active by design; MDM estimated while disconnected; fixed $11.90 retained per tariff", "NONE"),
        ("E5", "Priya Natarajan", "FINDING", "LOW", "60.0", "12.60", "1.70", "14.30", "C6 - bill run 2026-11-02 01:00 before stop processed 2026-11-03 06:00", "HOLD"),
        ("E6", "Brightwater Bakery", "FINDING", "REVIEW", "0.0", "0.00", "0.00", "0.00", "Consumption after disconnect: 645 kWh ACTUAL Sep 19-Oct 31 (660 kWh to Nov 1), unbilled - possible failed disconnect or tamper", "NONE"),
        ("E7", "Northgate Dental", "DAILY_CHECK_ONLY", "", "0.0", "0.00", "0.00", "0.00", "Disconnect 2026-11-01 14:20, integration ERROR, SA still ACTIVE - hold before next bill run", "HOLD"),
    ],
}
ANSWER["B"] = [r for r in ANSWER["A"] if r[0] not in ("E2", "E5")]
CONTROLS = [("N1", "Eastside Florist", "Proper stop; prorated final bill; zeros after"),
            ("N2", "Gareth Owens", "Disconnect work order CANCELLED"),
            ("N3", "Rosa Delgado", "6 estimated days Sep 12-17 on an active premise (comms failure)"),
            ("N4", "Kestrel Print Co", "Disconnect 2026-10-31 23:10; final bill through Oct 31 (disconnect day billable)"),
            ("N5", "Amara Osei", "METER_EXCHANGE work order, not a disconnect")]


def write(dataset, folder):
    T, periods = build(dataset)
    os.makedirs(folder, exist_ok=True)
    for fn, (key, cols) in SCHEMAS.items():
        with open(os.path.join(folder, fn), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in T[key]:
                w.writerow({c: r.get(c, "") for c in cols})
    with open(os.path.join(folder, "ccb_tariffs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tariff_code", "description", "rate_per_kwh", "fixed_daily_charge",
                    "fixed_stops_on_stop_service", "fixed_continues_on_nonpay"])
        for k, v in TARIFFS.items():
            w.writerow([k, v["desc"], f"{v['rate']:.2f}", f"{v['fixed']:.2f}", "Y", "Y"])
    with open(os.path.join(folder, "gl_periods.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["accounting_period", "status", "closed_at"])
        w.writeheader(); w.writerows(periods)
    with open(os.path.join(folder, "answer_key.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "customer_name", "back_scan_result", "severity", "phantom_kwh_billed",
                    "credit_consumption", "credit_fixed", "credit_total", "cause_and_notes",
                    "daily_check_2026_11_02"])
        for r in ANSWER[dataset]:
            w.writerow(r)
        for c in CONTROLS:
            w.writerow([c[0], c[1], "NO_FLAG", "", "0.0", "0.00", "0.00", "0.00", c[2], "NONE"])
    return T


if __name__ == "__main__":
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    TA = write("A", os.path.join(OUT, "dataset_A_with_errors"))
    TB = write("B", os.path.join(OUT, "dataset_B_two_errors_corrected"))
    print("done")
