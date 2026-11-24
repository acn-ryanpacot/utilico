# AI-TrueBILL workflow rules v3.0

Generated from the AI-TrueBILL build prompt (§2, §8–§13, §16, §19). **The prompt remains the source of truth**; this file is the readable rule book and is also indexed into the knowledge store's `rules` collection. Rule set version: **v1.0** (`config/rules/v1.0.json`).

## 1. Scope

Prevent and resolve one problem: a customer billed, or about to be billed, for days after their service was physically disconnected.

In scope: disconnect never reached billing (E1, E2, E7) · ID mismatch (E3) · timing race (E5) · non-pay window estimated (E4).
Out of scope - never creates a case: usage after a disconnect that was not billed (E6, revenue protection), field visits, legal review.

## 2. Personas and segregation of duties

| Persona | Rights |
|---|---|
| Maya Chen - CSR / Billing Operations (`u.csr`) | Receives disputes and new cases; confirms triage and ID matches (evidence only); releases held bills; sends approved customer summaries; sends back-scan findings into the workflow. No decision or approval rights. |
| Daniel Okoro - DRI, Billing Exceptions Lead (`u.dri`) | The single decision point: approves or overrides recommendations, approves execution plans, approves all outputs including regulatory content, closes and seals cases. |
| Aisha Rahman - Finance Controller (`u.finance`) | Approves reversals of revenue already posted to the GL (REMEDIATE credits). Nothing else. |

Non-human actors (never approve anything): agent.orchestrator, agent.retrieval, agent.reconciliation, agent.recommendation, agent.execution, agent.communications, sys.prebill, sys.verifier.

- **SoD-1** Agents and system actors propose and execute; only humans approve. No agent can approve, change priority, or run a step that is not in an approved plan.
- **SoD-2** Only the DRI decides, approves plans, approves outputs, and closes and seals.
- **SoD-3** Only Finance approves posted-revenue reversals. The DRI cannot.
- **SoD-4** Only the CSR releases held bills and sends customer communications.
- **SoD-5** Every override, rejection, cancellation and output edit requires a written reason.

Every violation returns HTTP 403 and is logged as `PERMISSION_DENIED`.

## 3. Reconciliation rules and pre-bill checks

Disconnect window: the disconnect day is billable; post-disconnect days run from disconnect date + 1 until the day before the next COMPLETED RECONNECT on the same premise (open-ended if none).

| Rule | Fires when |
|---|---|
| R1 Billed after disconnect (OMS ↔ CC&B) | A draft or issued bill covers post-disconnect days (NONPAY: days with billed kWh) |
| R2 Estimated reads after disconnect (MDM ↔ OMS) | Post-disconnect days within an affected bill carry ESTIMATED reads |
| R3 Complaint without billing hold (CRM ↔ CC&B) | A BILLING_ENQUIRY, BILLING_DISPUTE or REGULATORY_COMPLAINT case opened on or after the disconnect date with billing_hold_set = N |
| R4 Disputed revenue posted (GL ↔ Customer Care) | An affected bill is DISPUTED (or has an open BILLING_DISPUTE case) and its GL journal is posted. High if the period is CLOSED at scenario time, else Medium |
| PB1 | The SA is ACTIVE at scenario time despite a STOP_SERVICE / MOVE_OUT disconnect |
| PB2 | The disconnect's integration message is ERROR, SKIPPED or UNPROCESSED at scenario time |
| PB3 | An OMS premise has no crosswalk match but a crosswalk premise is within Levenshtein 1 (O/0, I/1 equal) |

Blocking at a bill run: R1, R2, PB3 → HELD (PREVENT case). Only PB1/PB2 → PASSED + ADVANCE case.

Probable cause (first match wins): **C1** integration SKIPPED - no mapping (S3) · **C3** ERROR mentioning the cross-reference - ID mismatch (S3, S1) · **C2** ERROR with an open To Do - integration failure (S3, S1) · **C6** UNPROCESSED at scenario time or processed after the affected bill run - bill ran before stop processed (S1) · **C4+C5** NONPAY with estimated post-disconnect days (S2). R3 adds **S4**, R4 adds **S5**.

System fixes: S1 CC&B pre-bill check · S2 MDM read types and estimates tied to premise status · S3 OMS disconnect events · S4 CRM complaint categories that trigger holds · S5 GL disputed-revenue status.

### Point-in-time view

OMS records visible once completed (open orders by created_at) · integration messages once received, UNPROCESSED while processed_at > T · SAs ACTIVE while the stop is processed after T · To Dos and CRM cases once created / opened · MDM reads dated before date(T) · GL periods OPEN while closed_at > T · GL lines posted on or before date(T). Sandbox overlays are layered on top.

Interpretations made for this build: PB1 is evaluated on the disconnect's premise as recorded (so an unmatched premise shows PB3, not PB1); a bill is DISPUTED only once a BILLING_DISPUTE case exists at scenario time; drafts held at a bill run have no posted GL journal; for a REGULATORY_RESPONSE case the rules are re-run on the original (unsandboxed) records so the findings describe what happened.

### Freshness

Each connector reports its lag (0 for CSV). Maximum allowed lag 15 minutes (`config/freshness.json`). With "Simulate stale MDM feed (6 h)" every BILL_READY is blocked: banner, `BILL_RUN_BLOCKED_STALE_DATA`, no drafts evaluated. Turning the toggle off and stepping the timeline processes the run.

## 4. Case model and lifecycle

Types: **ADVANCE** (no wrong bill yet - fix the account before the next bill), **PREVENT** (draft HELD at a bill run), **REMEDIATE** (issued bills, posted revenue - from a dispute or the back-scan), **REGULATORY_RESPONSE** (regulatory complaint after the related case was sealed). One case per account and root-cause disconnect; new bills and events attach to an open case.

Lifecycle (`config/workflow.json`; any other transition is rejected):
NEW (Orchestrator) → RETRIEVING (Data Retrieval, five systems in parallel) → RECONCILING → RECOMMENDING → TRIAGE (CSR; a confirmed PB3 match re-runs retrieval-recommendation once) → AWAITING_DRI (DRI) → PENDING_FINANCE (REMEDIATE, Finance) → PLAN_APPROVAL (DRI) → EXECUTING (approved steps only) → VERIFYING (verifier) → DRAFTING_OUTPUTS (Communications) → OUTPUT_APPROVAL (DRI) → READY_FOR_CSR (CSR releases the held bill and/or sends the summary) → RESOLVED → SEALED (DRI). Release as-is and regulatory responses go AWAITING_DRI → DRAFTING_OUTPUTS. Any failure or rejection returns the case to AWAITING_DRI with `returned=true`; the autopilot leaves returned cases to a person.

DRI options: approve the recommendation · choose another option (reason) · Release as-is (override, reason) · cancel as false positive (reason). A **Flag** recommendation cannot be approved as an action.

Recommendation labels: **Prorate** (stop the account at the disconnect date), **Suppress** (remove post-disconnect consumption; cancel the draft if no bill is due), **Flag** (unconfirmed PB3 - hold and escalate).

## 5. Amount, priority, SLA, triage

Amount at stake: PREVENT = draft reduction · REMEDIATE = credit · ADVANCE = projected exposure this cycle · REGULATORY_RESPONSE = the linked case's amount.
Projected exposure: avg of up to 30 most recent ACTUAL reads before the disconnect × remaining days in the cycle after the disconnect day (next cycle's days for a non-blocking bill-run result) × rate, rounded, + days × fixed daily charge, rounded. Decimal, ROUND_HALF_UP.

Impact: High ≥ $1,000 or a dispute / regulatory flag; Medium $250–$999.99; Low < $250; +1 level with ≥ 3 bills (ADVANCE counts 0). Urgency: High for PREVENT, any flag, REGULATORY_RESPONSE; Medium otherwise. Priority: High×High P1, High×Medium P2, Medium×High P2, Medium×Medium P3, Low×High P3, Low×Medium P4.

SLA (clock hours from case open, real time): P1 1 h / 24 h · P2 4 h / 48 h · P3 24 h / 120 h · P4 48 h / 240 h. At risk at 75 %, breached at 100 % - display and KPI flags only.

Triage at AWAITING_DRI (measured, not acting): **Red** amount ≥ $1,000, a flag, PB3 fired or a Flag recommendation; **Green** amount < $50, confidence ≥ 0.70, no flags, not Flag, no PB3; **Amber** otherwise. Green cases record `would_auto_approve` and whether the DRI approved unchanged (triage agreement KPI).

Confidence: 0.60 + 0.10 per signal, cap 0.95 - integration failed / unprocessed / processed after the bill run · open To Do · ESTIMATED reads from the day after the disconnect (not for PB3 cases) · a CRM signal phrase ("disconnect", "no further charges", "moved", "returned", "dispute") · PB3 corroborated (near-match meter ESTIMATED within 2 days of the unmatched disconnect).

## 6. Playbooks and verifier

| Option / context | Steps |
|---|---|
| Prorate, ADVANCE | [CORRECT_PREMISE_REFERENCE if PB3 confirmed] → [REPROCESS_INTEGRATION_MESSAGE if not SUCCESS] → STOP_SA (disconnect date) → ZERO_POST_DISCONNECT_READS (disconnect+1, open) → [CLOSE_TODO] → ADD_CRM_NOTE |
| Prorate, PREVENT | ADVANCE steps → REGENERATE_DRAFT_BILL (or CANCEL_DRAFT_BILL if the stop is before the period) |
| Prorate, REMEDIATE | ADVANCE steps if the SA is still active → CREATE_CREDIT_MEMO → ADD_CRM_NOTE |
| Suppress, NONPAY | ZERO_POST_DISCONNECT_READS (window to the day before the reconnect) → REGENERATE_DRAFT_BILL keeping fixed charges (REMEDIATE: credit memo + CRM note) |
| Flag / Release as-is | No steps |

Each step has a check (crosswalk resolves · status SUCCESS · SA STOPPED with the date · read rule active and reads 0 CORRECTED · total = original − reduction · draft cancelled · To Do COMPLETE · memo = calculated credit in an OPEN period · note exists). The Execution agent may only run the next approved step; the first failed check stops it (`STEP_FAILED`). The verifier re-runs R1, R2 and PB1–PB3 on the sandboxed state (PREVENT: on the regenerated draft; REMEDIATE: issued bills are remediated by a credit memo equal to `calculate`).

## 7. Outputs

From one approved packet: customer summary · executive brief · Jira ticket (BILL-NEW-1147 upwards per scenario, in case order; C1/C2 title "Implement OMS disconnect event as billing suppression trigger in CC&B") · regulator response (only with a REGULATORY_COMPLAINT at scenario time, or for REGULATORY_RESPONSE). Every factual sentence ends with fact-ID citations. The consistency check confirms amounts, dates, bill ids and account ids are identical across outputs and match the packet; a failure blocks OUTPUT_APPROVAL.

## 8. KPIs and the Day-30 gate

Packet assembly time (NEW → recommendation) · conflict detection time (event → reconciliation) · detection rate · false-positive rate · citation coverage · output consistency · triage agreement · SLA at risk / breached. Day-30 gate: detection ≥ 95 %, false positives < 5 %, citations 100 %, packet < 5 min.

## Appendix A - expected results

See `config/expected_results.json` (prompt §19), checked by the Self-test:

- PREVENTION_REPLAY, Dataset A, follow-up ON: E2 ADVANCE $145.35 P4 Amber 0.70 · E3 ADVANCE $472.70 P3 Red 0.70 (Flag → Prorate) · E4 PREVENT $161.60 → $108.68 P3 Amber 0.80 · E1 ADVANCE $1,406.36 P2 Red 0.80 · E5 ADVANCE $12.32 P4 Green 0.70 · E7 ADVANCE $1,553.84 P2 Red 0.80; $ prevented $2,870.09; 1 bill held; Jira BILL-NEW-1147…1152.
- Follow-up OFF: six cases as PREVENT (E7 ADVANCE), $2,870.09, 5 bills held; E1 P1 Red 0.95 citing CASE-300265.
- Dataset B: E3, E4, E1, E7; $2,366.92; 1 / 3 held.
- DISPUTE_REPLAY: E1 REMEDIATE 4 Nov 09:40, R1–R4 (Medium), PB1, PB2, C2, S3 S1 S4 S5, credit $1,436.50 to 2026-11, P1 Red 0.95, Finance, BILL-NEW-1147, no regulator response; then REGULATORY_RESPONSE 9 Nov 15:30 linked to the sealed case, R4 High, regulator response only.
- BACKSCAN_2026-11-10: E1 P1 Red 0.95 · E2 P2 Amber 0.90 · E3 P3 Red 0.80 · E4 P4 Amber 0.80 · E5 P4 Green 0.80; $ remediated $2,870.09 (Dataset B $2,366.92); E6 cannot be sent.
