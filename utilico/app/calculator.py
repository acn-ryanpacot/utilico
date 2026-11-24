"""Money (prompt §9-§12). Decimal with ROUND_HALF_UP throughout. Agents never calculate money - they call these."""
import calendar
from decimal import Decimal

from .pit import STOP_SUBTYPES
from .util import D, add_days, day_range, days_between, f2, kwh1, money


def fixed_creditable(subtype, tariff):
    """Fixed charges are credited for stop-type disconnects when the tariff stops them; NONPAY keeps them when the
    tariff says fixed charges continue on non-pay."""
    if subtype in STOP_SUBTYPES:
        return tariff is None or tariff.get("fixed_stops_on_stop_service") == "Y"
    if subtype == "NONPAY":
        return tariff is not None and tariff.get("fixed_continues_on_nonpay") != "Y"
    return tariff is None or tariff.get("fixed_stops_on_stop_service") == "Y"


def tariff_of(view, sa, bill=None):
    t = view.tariffs.get(sa["tariff_code"])
    rate = D(t["rate_per_kwh"]) if t else D(bill["rate_per_kwh"] if bill else 0)
    fd = D(t["fixed_daily_charge"]) if t else D(bill["fixed_daily_charge"] if bill else 0)
    return t, rate, fd


def bill_line(view, subtype, sa, bill, win, meter_id, last_day=None):
    """Credit for one bill: phantom (ESTIMATED) kWh x rate + fixed charges for post-disconnect billed days."""
    start = max(win["start"], bill["period_start"])
    end = bill["period_end"]
    if win.get("end") and win["end"] < end:
        end = win["end"]
    if last_day and last_day < end:
        end = last_day
    if start > end:
        return None
    reads = view.reads_by_meter.get(meter_id, {})
    phantom = actual = Decimal(0)
    est = []
    for d in day_range(start, end):
        r = reads.get(d)
        if r and D(r["kwh"]) > 0:
            if r["read_quality"] == "ESTIMATED":
                phantom += D(r["kwh"])
                est.append(r["read_id"])
            else:
                actual += D(r["kwh"])
    tariff, rate, fd = tariff_of(view, sa, bill)
    days = days_between(start, end) + 1
    phantom = kwh1(phantom)
    cons = money(phantom * rate)
    fixed_amt = money(days * fd)
    cf = fixed_creditable(subtype, tariff)
    fixed_credit = fixed_amt if cf else Decimal("0.00")
    return {"bill_id": bill["bill_id"], "sa_id": sa["sa_id"], "from": start, "to": end, "overlap_days": days,
            "phantom_kwh": float(phantom), "actual_kwh": float(kwh1(actual)), "rate": float(rate), "fixed_daily": float(fd),
            "consumption_credit": f2(cons), "fixed_amount": f2(fixed_amt), "fixed_credit": f2(fixed_credit),
            "fixed_retained": f2(Decimal(0) if cf else fixed_amt), "credit": f2(cons + fixed_credit), "estimated_read_ids": est}


def draft_correction(view, subtype, sa, bill, win, meter_id, stop_date=None):
    """Correction of a draft bill.
    SUPPRESS (cancel) when the SA stops before period_start (every billed day is post-disconnect for a stop-type
    disconnect); otherwise REGENERATE: original total - calculated reduction (final bill when the stop is inside the
    period; NONPAY keeps fixed charges)."""
    total = money(bill["total_amount"])
    line = bill_line(view, subtype, sa, bill, win, meter_id)
    if subtype in STOP_SUBTYPES and (stop_date or win["disconnect_date"]) < bill["period_start"]:
        return {"bill_id": bill["bill_id"], "action": "CANCEL", "before": f2(total), "after": 0.0, "reduction": f2(total),
                "fixed_retained": 0.0, "line": line, "period_end_after": None}
    red = D(line["credit"]) if line else Decimal(0)
    final_end = (stop_date or win["disconnect_date"]) if subtype in STOP_SUBTYPES and line else None
    return {"bill_id": bill["bill_id"], "action": "REGENERATE", "before": f2(total), "after": f2(total - red), "reduction": f2(red),
            "fixed_retained": line["fixed_retained"] if line else 0.0, "line": line, "period_end_after": final_end}


def issued_credit(view, subtype, sp_sas, win, meter_id, before_run=None):
    """Credits on bills already issued (FINAL / DISPUTED) that cover post-disconnect days."""
    lines = []
    for sa in sp_sas:
        for b in sorted(view.bills_by_sa.get(sa["sa_id"], []), key=lambda b: b["period_start"]):
            if b["_state"] != "ISSUED" or b["bill_status"] not in ("FINAL", "DISPUTED"):
                continue
            if before_run and b["bill_run_at"] >= before_run:
                continue
            ln = bill_line(view, subtype, sa, b, win, meter_id)
            if ln and ln["credit"] > 0:
                lines.append(ln)
    return {"lines": lines, "credit": f2(sum((D(l["credit"]) for l in lines), Decimal(0))), "bill_ids": [l["bill_id"] for l in lines]}


def billing_cycle(day):
    """Monthly billing cycle (1st .. last day of the month) containing `day`."""
    y, m = int(day[:4]), int(day[5:7])
    return "%04d-%02d-01" % (y, m), "%04d-%02d-%02d" % (y, m, calendar.monthrange(y, m)[1])


def project_exposure(view, sa, meter_id, disconnect_date, after_period_end=None):
    """ADVANCE amount at stake: projected exposure this cycle (prompt §9).
    avg_kwh = mean of the up-to-30 most recent ACTUAL reads dated before the disconnect date (visible at T);
    days = remaining days in the current cycle after the disconnect day - or, for a non-blocking result found at a
    bill run (after_period_end = the draft's period end), the next cycle's days after the disconnect day;
    exposure = round(avg x days x rate, 2) + round(days x fixed, 2)."""
    reads = view.reads_by_meter.get(meter_id, {})
    act = sorted((r for d, r in reads.items() if d < disconnect_date and r["read_quality"] == "ACTUAL"),
                 key=lambda r: r["read_date"])[-30:]
    avg = (sum((D(r["kwh"]) for r in act), Decimal(0)) / len(act)) if act else Decimal(0)
    cyc_start, cyc_end = billing_cycle(add_days(after_period_end, 1) if after_period_end else disconnect_date)
    first = max(cyc_start, add_days(disconnect_date, 1))
    days = max(0, days_between(first, cyc_end) + 1)
    _, rate, fd = tariff_of(view, sa)
    cons = money(avg * days * rate)
    fixed = money(days * fd)
    return {"avg_kwh": float(avg.quantize(Decimal("0.0001"))), "reads_used": len(act), "first_read": act[0]["read_id"] if act else None,
            "last_read": act[-1]["read_id"] if act else None, "read_ids": [r["read_id"] for r in act], "days": days,
            "cycle": [cyc_start, cyc_end], "rate": float(rate), "fixed_daily": float(fd), "consumption": f2(cons), "fixed": f2(fixed),
            "exposure": f2(cons + fixed)}
