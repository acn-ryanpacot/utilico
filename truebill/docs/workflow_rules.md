# AI-TrueBILL workflow rules v3.1

Readable rule book for AI-TrueBILL, also indexed into the knowledge store's `rules` collection. Rule set version: **v1.0** (`config/rules/v1.0.json`).

**v3.1 changes (vs v3.0):** the process ends at the DRI's decision - there is no Execution agent, verifier, Finance step or customer-facing output step; Finance (Aisha Rahman) is removed; the CSR can look up an account number to have the agents check it; Accenture colour theme. The four output templates (customer summary, executive brief, Jira ticket, regulator response) are kept in the code but switched off.

## 1. Scope

Prevent and resolve one problem: a customer billed, or about to be billed, for days after their service was physically disconnected.

In scope: disconnect never reached billing (E1, E2, E7) · ID mismatch (E3) · timing race (E5) · non-pay window estimated (E4).
Out of scope - never creates a case: usage after a disconnect that was not billed (E6, revenue protection), field visits, legal review.

## 2. Personas and segregation of duties

| Persona | Rights |
|---|---|
| Maya Chen - CSR / Billing Operations (`u.csr`) | Receives disputes and new cases; looks up an account number to have the agents check it; confirms triage and ID matches (evidence only); sends back-scan findings into the workflow. No decision or approval rights. |
| Daniel Okoro - DRI, Billing Exceptions Lead (`u.dri`) | The single and final decision point. His decision ends and seals the case. |

Non-human actors (never approve anything): agent.orchestrator, agent.retrieval, agent.reconciliation, agent.recommendation, sys.prebill. (agent.communications is dormant.)

- **SoD-1** Agents and system actors retrieve, reconcile and recommend; only humans decide. Agents have no write tools - nothing is written to the source systems or a sandbox.
- **SoD-2** Only the DRI decides. The decision is final.
- **SoD-3** Only the CSR performs intake: account lookups, triage, ID-match confirmation, back-scan sends.
- **SoD-4** Every override, rejection and cancellation requires a written reason.

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

Blocking at a bill run: R1, R2, PB3 → HELD (PREVENT case). Only PB1/PB2 → PASSED + ADVANCE case. Because nothing is corrected after a decision, a later draft for a disconnect that already has a case (open or decided) is held again and attached to that case - no second case is opened.

Probable cause (first match wins): **C1** integration SKIPPED - no mapping (S3) · **C3** ERROR mentioning the cross-reference - ID mismatch (S3, S1) · **C2** ERROR with an open To Do - integration failure (S3, S1) · **C6** UNPROCESSED at scenario time or processed after the affected bill run (S1) · **C4+C5** NONPAY with estimated post-disconnect days (S2). R3 adds **S4**, R4 adds **S5**.

Point-in-time view: OMS records visible once completed · integration messages once received (UNPROCESSED while processed_at > T) · SAs ACTIVE while the stop is processed after T · To Dos and CRM cases once created / opened · MDM reads dated before date(T) · GL periods OPEN while closed_at > T · GL lines posted on or before date(T). PB1 is evaluated on the premise as recorded; a bill is DISPUTED only once a BILLING_DISPUTE case exists; held drafts have no posted journal; a REGULATORY_RESPONSE case re-reads the original records.

Freshness: maximum connector lag 15 minutes. With "Simulate stale MDM feed (6 h)" every BILL_READY is blocked (banner, `BILL_RUN_BLOCKED_STALE_DATA`, no drafts evaluated) until the toggle is off and the timeline is stepped.

## 4. Case model and lifecycle

Types: **ADVANCE** (no wrong bill yet), **PREVENT** (draft HELD at a bill run), **REMEDIATE** (issued bills - from a dispute, the back-scan or a CSR lookup), **REGULATORY_RESPONSE** (regulatory complaint after the related case was decided).

Lifecycle (`config/workflow.json`; any other transition is rejected):

NEW (Orchestrator) → RETRIEVING (Data Retrieval, five systems in parallel) → RECONCILING → RECOMMENDING → TRIAGE (CSR; a confirmed PB3 match re-runs retrieval → recommendation once) → AWAITING_DRI → **APPROVED** or **REJECTED** (DRI). The decision seals the case (SHA-256 over its audit events and evidence facts) and the process ends.

DRI options: approve the recommendation → APPROVED · choose another option (reason) → APPROVED · release as-is (override, reason) → REJECTED · cancel as a false positive (reason) → REJECTED. A **Flag** recommendation cannot be approved as an action.

Recommendation labels: **Prorate** (stop billing at the disconnect date), **Suppress** (remove post-disconnect consumption; cancel the draft if no bill is due), **Flag** (unconfirmed PB3 - hold and escalate). The approved option is the instruction; carrying it out in CC&B / MDM is outside this tool.

### CSR account lookup

Maya enters an account number on **Account lookup**. At the current scenario time the Orchestrator finds the account's latest completed disconnect (including an unmatched OMS premise one character away from one of its premises). If that disconnect already has a case, the lookup returns it. Otherwise a lookup case `LK-nnnn` is opened (REMEDIATE when issued bills carry post-disconnect charges, else ADVANCE) and the agents retrieve all five systems and reconcile:

- a rule fired → recommendation → Maya's triage → the DRI's final decision;
- nothing fired → **NO_ISSUE**: the agents' evidence and rule results are kept and shown, but no case is opened, nothing goes to the DRI, and it is not counted in case KPIs;
- no completed disconnect, or an unknown account → a message only.

## 5. Amount, priority, SLA, triage

Amount at stake: PREVENT = draft reduction · REMEDIATE = credit · ADVANCE = projected exposure this cycle · REGULATORY_RESPONSE = the linked case's amount. Projected exposure = round(avg of up to 30 most recent ACTUAL reads before the disconnect × remaining cycle days × rate, 2) + round(days × fixed daily charge, 2); Decimal, ROUND_HALF_UP.

Impact: High ≥ $1,000 or a dispute / regulatory flag; Medium $250–$999.99; Low < $250; +1 level with ≥ 3 bills (ADVANCE counts 0). Urgency: High for PREVENT, any flag, REGULATORY_RESPONSE; Medium otherwise. Priority: High×High P1, High×Medium P2, Medium×High P2, Medium×Medium P3, Low×High P3, Low×Medium P4.

SLA (clock hours from case open): P1 1 h / 24 h · P2 4 h / 48 h · P3 24 h / 120 h · P4 48 h / 240 h; resolve = the DRI's decision. At risk at 75 %, breached at 100 % - flags only.

Triage at AWAITING_DRI: **Red** amount ≥ $1,000, a flag, PB3 or a Flag recommendation; **Green** amount < $50, confidence ≥ 0.70, no flags, not Flag, no PB3; **Amber** otherwise.

Confidence: 0.60 + 0.10 per signal, cap 0.95 - integration failed / unprocessed / processed after the bill run · open To Do · ESTIMATED reads from the day after the disconnect (not for PB3 cases) · a CRM signal phrase ("disconnect", "no further charges", "moved", "returned", "dispute") · PB3 corroborated.

## 6. KPIs and the Day-30 gate

Money is counted when the DRI approves: **$ prevented** = approved PREVENT reductions · **$ remediated** = approved REMEDIATE credits · **$ exposure addressed** = approved ADVANCE projections. Rejected cases count nothing.

Packet assembly time (NEW → recommendation) · conflict detection time (event → reconciliation) · detection rate · false-positive rate · citation coverage (agent statements - rule explanations and recommendations - citing existing facts) · triage agreement · SLA at risk / breached · approved / rejected. Day-30 gate: detection ≥ 95 %, false positives < 5 %, citations 100 %, packet < 5 min.

## Appendix A - expected results

`config/expected_results.json`, checked by the Self-test (all cases end APPROVED and sealed with the autopilot):

- PREVENTION_REPLAY, Dataset A, follow-up ON: E2 ADVANCE $145.35 P4 Amber 0.70 · E3 ADVANCE $472.70 P3 Red 0.70 (Flag → Prorate after the match) · E4 PREVENT $161.60 → $108.68 P3 Amber 0.80 · E1 ADVANCE $1,406.36 P2 Red 0.80 · E5 ADVANCE $12.32 P4 Green 0.70 · E7 ADVANCE $1,553.84 P2 Red 0.80; $ prevented $52.92; $ exposure addressed $3,590.57; 8 bills held (7 on an existing case).
- Follow-up OFF: E2, E3, E4, E1, E5 PREVENT + E7 ADVANCE; $ prevented $1,813.30; exposure $1,553.84; 8 held (3 on an existing case); E1 P1 Red 0.95 citing CASE-300265.
- Dataset B: E3, E4, E1, E7 - ON: $52.92 prevented, $3,432.90 exposure, 4 held; OFF: $1,669.42 prevented, $1,553.84 exposure, 4 held.
- DISPUTE_REPLAY (A and B): E1 REMEDIATE 4 Nov 09:40, R1–R4 (Medium), PB1, PB2, C2, S3 S1 S4 S5, $1,436.50, P1 Red 0.95; then REGULATORY_RESPONSE 9 Nov 15:30 linked to the decided case, R4 High; $ remediated $1,436.50.
- BACKSCAN_2026-11-10: E1 P1 Red 0.95 · E2 P2 Amber 0.90 · E3 P3 Red 0.80 · E4 P4 Amber 0.80 · E5 P4 Green 0.80; $ remediated $2,870.09 (Dataset B $2,366.92); E6 cannot be sent.
- CSR lookups (Dataset A, DISPUTE_REPLAY at 3 Nov 12:00): E1 → REMEDIATE case (R1, R2, R3, PB1, PB2; Prorate) · E3 → REMEDIATE case (PB2, PB3; Flag) · E6 and N1 → NO_ISSUE · an account with no disconnect → message · unknown account → error.
