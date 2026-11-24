# Utilico Energy – mock system extracts for POCs

All data is fictional. Two datasets with identical structure and IDs:

| Folder | Purpose |
|---|---|
| `dataset_A_with_errors/` | Full scenario: 7 problem cases (E1–E7), 5 controls (N1–N5), 35 normal accounts |
| `dataset_B_two_errors_corrected/` | Same customers and IDs, but E2 and E5 are handled correctly |

Extract assumptions: MDM daily reads cover 2026-08-01 to 2026-11-01 (the investigation window).
All other systems are extracted as of 2026-11-10. Billing cycles are calendar months (Aug, Sep, Oct 2026),
billed on the 2nd of the following month at 01:00. Daily-check scenarios use as-of 2026-11-02.

## Files by system

| System | File | What it holds |
|---|---|---|
| Oracle CC&B | `ccb_accounts.csv` | Customer accounts (47) |
| | `ccb_service_agreements.csv` | SA status, stop date, and when the stop was actually processed |
| | `ccb_bill_segments.csv` | Bills: period, run time, kWh, usage quality (ACTUAL/ESTIMATED/MIXED), amounts, status |
| | `ccb_tariffs.csv` | Rates, fixed daily charges, fixed-charge rules for stop-service vs non-pay |
| | `ccb_todo_entries.csv` | CC&B To Do queue (exceptions), including unworked upload failures |
| MDM | `mdm_meters.csv` | Meter, MDM service point, connection state |
| | `mdm_daily_reads.csv` | One read per meter per day, with read quality and estimation rule |
| OMS / Field service | `oms_work_orders.csv` | Disconnects, reconnects, meter exchanges, inspections |
| CRM | `crm_cases.csv` | Cases with free-text notes; `billing_hold_set` is always N (no integration exists) |
| GL / Financials | `gl_journal_lines.csv` | Revenue postings per bill (AR / consumption revenue / fixed revenue) |
| | `gl_periods.csv` | Accounting periods; Aug–Oct closed, Nov open |
| Integration middleware | `integration_message_log.csv` | OMS → CC&B field-activity messages: SUCCESS / ERROR / SKIPPED |
| Master data | `ref_id_crosswalk.csv` | OMS premise ↔ CC&B service point ↔ MDM meter |
| (test oracle) | `answer_key.csv` | Expected results for this dataset |

Key joins: `oms_work_orders.oms_premise_id` → `ref_id_crosswalk` → `ccb_sp_id` (service agreements) and
`mdm_meter_id` (meters, reads). `ccb_bill_segments.bill_id` = `gl_journal_lines.source_ref`.
`integration_message_log.wo_id` = `oms_work_orders.wo_id`.

## Where the problem data lives (Dataset A)

| Case | Customer / account | What's wrong | Where to see it |
|---|---|---|---|
| E1 – headline case | Harbourline Logistics Ltd, 8800010101, SA-4400101 | Disconnected 14 Oct; billed full October on estimated reads | OMS `WO-104242`; integration `MSG-550018` ERROR; CC&B To Do `TD-7701` OPEN unassigned; SA ACTIVE; MDM `MTR-930101` CONNECTED, ESTIMATED 500 kWh/day Oct 15–Nov 1; bill `BL-4400101-202610` DISPUTED; CRM `CASE-300265` (prior warning), `CASE-300276` (dispute, closed as "valid" then reopened), `CASE-300287` (regulator); GL `JE-4400101-202610` posted to period 2026-10, closed 6 Nov |
| E2 – silent integration gap | Dana Whitfield, 8800010214, SA-4400214 | Move-out 9 Aug never reached CC&B; billed 3 cycles on estimates | OMS `WO-104249` subtype MOVE_OUT; integration `MSG-550021` SKIPPED (no mapping); SA ACTIVE; MDM ESTIMATED 24 kWh/day from Aug 10; bills `BL-4400214-202608/09/10`; CRM `CASE-300298` returned mail |
| E3 – ID mismatch | Copperleaf Studio, 8800010417, SA-4400417 | OMS used premise `PRM-0O417` (letter O); crosswalk has `PRM-00417` | OMS `WO-104256`; integration `MSG-550024` ERROR "premise not found"; SA ACTIVE; MDM ESTIMATED 120 kWh/day from Sep 23; bills `BL-4400417-202609/10` |
| E4 – non-pay window | Luis Ferreira, 8800010322, SA-4400322 | Non-pay disconnect 5 Sep, reconnect 20 Sep; MDM estimated while off | OMS `WO-104263` (NONPAY), `WO-104270` (RECONNECT); MDM ESTIMATED 18 kWh/day Sep 6–19; bill `BL-4400322-202609`; CRM `CASE-300309`, `CASE-300320` |
| E5 – timing race | Priya Natarajan, 8800010388, SA-4400388 | Disconnect 29 Oct; stop processed 3 Nov, after the 2 Nov bill run | OMS `WO-104277`; integration `MSG-550033` processed 2026-11-03 06:00; SA `stop_processed_at` 2026-11-03; MDM ESTIMATED Oct 30–31; bill `BL-4400388-202610` |
| E6 – failed disconnect / tamper | Brightwater Bakery, 8800010455, SA-4400455 | Stopped and final-billed correctly, but meter keeps recording real usage | OMS `WO-104284`; SA STOPPED; bill `BL-4400455-202609` (Sep 1–18); MDM ACTUAL 15 kWh/day Sep 19–Nov 1 (unbilled); CRM `CASE-300331` "lights on" |
| E7 – recent, not yet billed | Northgate Dental, 8800010512, SA-4400512 | Disconnect 1 Nov failed to reach CC&B; next bill would be wrong | OMS `WO-104291`; integration `MSG-550039` ERROR; To Do `TD-7702` OPEN; SA ACTIVE |

Controls that must **not** be flagged: N1 Eastside Florist (proper stop), N2 Gareth Owens (cancelled disconnect
`WO-104305`), N3 Rosa Delgado (estimated Sep 12–17 on an active premise), N4 Kestrel Print Co (disconnect
31 Oct 23:10, billed through 31 Oct), N5 Amara Osei (meter exchange).

## Dataset B changes (E2 and E5 corrected)

- E2: integration SUCCESS; SA STOPPED 2026-08-09; final bill Aug 1–9 only; MDM zero ACTUAL from Aug 10; meter DISCONNECTED; no Sep/Oct bills or GL lines; no returned-mail case.
- E5: stop processed 2026-10-30 03:00; final bill Oct 1–29; MDM zero ACTUAL Oct 30–31.
- Everything else, including all IDs, is identical.

## Expected results (verified against the data)

| | Dataset A | Dataset B |
|---|---|---|
| Back-scan findings | E1 $1,436.50 · E2 $488.87 · E4 $52.92 · E5 $14.30 · E6 REVIEW (645 kWh unbilled) | E1 $1,436.50 · E4 $52.92 · E6 REVIEW |
| Total credit owed | $1,992.59 | $1,489.42 |
| Unmatched disconnects | E3 (potential $877.50) | E3 (potential $877.50) |
| Daily check, as of 2026-11-02 | E1, E2, E5, E7 | E1, E7 |

Rules used: the disconnect day is billable; post-disconnect days start the next day and end the day before a
reconnect. Consumption on ESTIMATED post-disconnect days is credited at the tariff rate. Fixed charges for those
days are credited for stop-service disconnects and retained for non-pay disconnects.

`generate.py` rebuilds both datasets deterministically (Python 3, no dependencies) into a `generated/` folder, if you want to change volumes or cases.
