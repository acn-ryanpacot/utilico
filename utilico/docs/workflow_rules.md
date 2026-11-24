# AI-Utilico workflow rules v3.2

Readable rule book for AI-Utilico, also indexed into the knowledge store's `rules` collection. Rule set version: **v1.0** (`config/rules/v1.0.json`).

**v3.2 changes:** the timeline is removed - no replay scenarios, bill runs, disconnect follow-ups, dispute events, presets, autopilot, demo reset or stale-feed toggle. Cases come only from the back-scan and the CSR account lookup, each read as of its own date. **v3.1:** the process ends at the DRI's decision (no Execution agent, verifier, Finance or output step); Finance removed; CSR account lookup; Accenture colour theme. The four output templates are kept in the code but switched off.

## 1. Scope

Find and resolve one problem: a customer billed for days after their service was physically disconnected, or an account still active after a disconnect.

In scope: disconnect never reached billing (E1, E2, E7) · ID mismatch (E3) · timing race (E5) · non-pay window estimated (E4).
Out of scope - never creates a case: usage after a disconnect that was not billed (E6, revenue protection), field visits, legal review.

## 2. Personas and segregation of duties

| Persona | Rights |
|---|---|
| Maya Chen - CSR / Billing Operations (`u.csr`) | Sends back-scan findings into the workflow; looks up an account (with an as-of date) to have the agents check it; confirms triage and ID matches (evidence only). No decision or approval rights. |
| Daniel Okoro - DRI, Billing Exceptions Lead (`u.dri`) | The single and final decision point. His decision ends and seals the case. |

Non-human actors (never approve anything): agent.orchestrator, agent.retrieval, agent.reconciliation, agent.recommendation.

- **SoD-1** Agents retrieve, reconcile and recommend; only humans decide. Agents have no write tools - nothing is written to the source systems.
- **SoD-2** Only the DRI decides. The decision is final.
- **SoD-3** Only the CSR performs intake: back-scan sends, account lookups, triage, ID-match confirmation.
- **SoD-4** Every override, rejection and cancellation requires a written reason.

Every violation returns HTTP 403 and is logged as `PERMISSION_DENIED`.

## 3. Intake and point in time

There is no timeline. Every case has an **as-of time** and everything the agents, rules and calculator see is the point-in-time view at that time:

- **Back-scan** - as of the extract end, **2026-11-10 23:59**. The POC 1 back-scan lists findings and the unmatched disconnect; Maya sends them (one by one or all eligible) and each becomes a REMEDIATE case. REVIEW rows (E6) are disabled: "Out of scope – revenue protection".
- **Account lookup** - as of **23:59 on the date Maya picks** (2026-08-01 … 2026-11-10, default 2026-11-10). The Orchestrator finds the account's latest completed disconnect visible on that date (including an unmatched OMS premise one character from one of its premises). Existing case for that disconnect → that case is returned. Otherwise a lookup case `LK-nnnn` opens - REMEDIATE when issued bills carry post-disconnect charges, else ADVANCE - and the agents retrieve all five systems and reconcile: a rule fired → recommendation → triage → the DRI; nothing fired → **NO_ISSUE** (evidence kept and shown, no case, nothing to the DRI); no completed disconnect or an unknown account → a message only.

Point-in-time rules: OMS records visible once completed · integration messages once received (UNPROCESSED while processed_at > T) · SAs ACTIVE while the stop is processed after T · To Dos and CRM cases once created / opened · MDM reads dated before date(T) · GL periods OPEN while closed_at > T · GL lines posted on or before date(T). PB1 is evaluated on the premise as recorded; a bill is DISPUTED only once a BILLING_DISPUTE case exists.

## 4. Reconciliation rules

Disconnect window: the disconnect day is billable; post-disconnect days run from disconnect date + 1 until the day before the next COMPLETED RECONNECT (open-ended if none).

| Rule | Fires when |
|---|---|
| R1 Billed after disconnect (OMS ↔ CC&B) | An issued bill covers post-disconnect days (NONPAY: days with billed kWh) |
| R2 Estimated reads after disconnect (MDM ↔ OMS) | Post-disconnect days within an affected bill carry ESTIMATED reads |
| R3 Complaint without billing hold (CRM ↔ CC&B) | A BILLING_ENQUIRY, BILLING_DISPUTE or REGULATORY_COMPLAINT case opened on or after the disconnect date with billing_hold_set = N |
| R4 Disputed revenue posted (GL ↔ Customer Care) | An affected bill is DISPUTED (or has an open BILLING_DISPUTE case) and its GL journal is posted. High if the period is CLOSED at the as-of time, else Medium |
| PB1 | The SA is ACTIVE despite a STOP_SERVICE / MOVE_OUT disconnect |
| PB2 | The disconnect's integration message is ERROR, SKIPPED or UNPROCESSED |
| PB3 | An OMS premise has no crosswalk match but a crosswalk premise is within Levenshtein 1 (O/0, I/1 equal) |

Probable cause (first match wins): **C1** integration SKIPPED (S3) · **C3** ERROR mentioning the cross-reference (S3, S1) · **C2** ERROR with an open To Do (S3, S1) · **C6** UNPROCESSED, or processed after the affected bill run (S1) · **C4+C5** NONPAY with estimated post-disconnect days (S2). R3 adds **S4**, R4 adds **S5**.

## 5. Case model and lifecycle

Types: **REMEDIATE** (issued bills charged for post-disconnect days) and **ADVANCE** (account still active after a disconnect, nothing wrongly billed yet - from a lookup). One case per account and root-cause disconnect.

Lifecycle (`config/workflow.json`): NEW → RETRIEVING (five systems in parallel) → RECONCILING → RECOMMENDING → TRIAGE (CSR; a confirmed PB3 match re-runs the agents once) → AWAITING_DRI → **APPROVED** or **REJECTED** (DRI) - or NO_ISSUE for a lookup where nothing fired. The decision seals the case (SHA-256 over its audit events and evidence facts) and the process ends.

DRI options: approve the recommendation → APPROVED · choose another option (reason) → APPROVED · release as-is (override, reason) → REJECTED · cancel as a false positive (reason) → REJECTED. A **Flag** recommendation cannot be approved as an action.

Recommendation labels: **Prorate** (stop billing at the disconnect date / credit), **Suppress** (remove post-disconnect consumption; NONPAY keeps fixed charges), **Flag** (unconfirmed PB3 - hold and escalate). Carrying out the approved option in CC&B / MDM is outside this tool.

## 6. Amount, priority, SLA, triage

Amount at stake: REMEDIATE = credit · ADVANCE = projected exposure this cycle = round(avg of up to 30 most recent ACTUAL reads before the disconnect × remaining cycle days × rate, 2) + round(days × fixed daily charge, 2); Decimal, ROUND_HALF_UP.

Impact: High ≥ $1,000 or a dispute / regulatory flag; Medium $250–$999.99; Low < $250; +1 level with ≥ 3 bills (ADVANCE counts 0). Urgency: High with any dispute / regulatory flag; Medium otherwise. Priority: High×High P1, High×Medium P2, Medium×High P2, Medium×Medium P3, Low×High P3, Low×Medium P4.

SLA (clock hours from case open; resolve = the DRI's decision): P1 1 h / 24 h · P2 4 h / 48 h · P3 24 h / 120 h · P4 48 h / 240 h. At risk 75 %, breached 100 % - flags only.

Triage at AWAITING_DRI: **Red** amount ≥ $1,000, a flag, PB3 or a Flag recommendation; **Green** amount < $50, confidence ≥ 0.70, no flags, not Flag, no PB3; **Amber** otherwise.

Confidence: 0.60 + 0.10 per signal, cap 0.95 - integration failed / unprocessed / processed after the bill run · open To Do · ESTIMATED reads from the day after the disconnect (not for PB3 cases) · a CRM signal phrase · PB3 corroborated.

## 7. KPIs and the Day-30 gate

Money is counted when the DRI approves: **$ remediated** = approved credits · **$ exposure addressed** = approved ADVANCE projections. Rejected cases count nothing. Detection rate = expected back-scan cases detected (after findings are sent); false positive = a case on an account outside the in-scope sample cases. Also: packet assembly time, conflict detection time, citation coverage (agent statements citing existing facts), triage agreement, SLA at risk / breached, decisions, intake. Day-30 gate: detection ≥ 95 %, false positives < 5 %, citations 100 %, packet < 5 min.

## Appendix A - expected results

`config/expected_results.json`, checked by the Self-test:

- **Back-scan, Dataset A** (sent all eligible, then approved): E1 REMEDIATE $1,436.50 P1 Red 0.95 (R1–R4 High, PB1, PB2; C2; S3 S1 S4 S5) · E2 $488.87 P2 Amber 0.90 (3 bills) · E3 $877.50 P3 Red 0.80 (PB2, PB3 → R1, R2 after the match; Flag → Prorate) · E4 $52.92 P4 Amber 0.80 (Suppress) · E5 $14.30 P4 Green 0.80 (C6); $ remediated $2,870.09; E6 cannot be sent.
- **Back-scan, Dataset B:** E1, E3, E4; $2,366.92.
- **Lookups (Dataset A):** E1 as of 2026-11-03 → REMEDIATE (R1, R2, R3, PB1, PB2; Prorate) · E3 as of 2026-11-03 → REMEDIATE (PB2, PB3; Flag) · E2 as of 2026-08-10 → ADVANCE $145.35 P4 0.70 · E7 as of 2026-11-10 → ADVANCE $1,553.84 P2 0.80 · E6, N1 → NO_ISSUE · Alex Bennett, and E5 as of 2026-10-15 (before its disconnect) → no disconnect · unknown account, or a date outside the extract → error.
