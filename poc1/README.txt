POC 1 - Disconnect-vs-Billing Detector + Pre-bill Guard (Utilico Energy) - workflow rules v2.0
==============================================================================================

What it does
------------
1. POC 1 detector (Back-scan / Daily check): finds every account billed for days after a
   confirmed disconnect and quantifies the credit owed.
2. Pre-bill gate: replays historical bill runs and HOLDS draft bills that would charge for days
   after a disconnection (rules G1-G5, config\workflow_rules.md section 3).
3. BEX tickets (PREVENT / ADVANCE / REMEDIATE) for three personas:
     Maya Chen      BILLING_OPS  runs the gate, triages, confirms ID matches, decides L0, releases bills
     Daniel Okoro   DRI          owns every ticket, decides L1/L2, approves plans and communications,
                                 reviews the L0 digest, exports the root-cause register
     Aisha Rahman   FINANCE      approves pre-issue reductions >= $1,000 and every REMEDIATE credit
   The lifecycle is a data-driven state machine (config\workflow.json). Priority (P1-P4), SLAs with
   escalation (config\sla.json), delegation of authority (L0/L1/L2) and segregation of duties
   (SoD-1..6) are all enforced by the server - a forbidden call returns 403 and is logged.
4. Investigation and execution agents: a real tool-calling loop over the data, driven by a
   deterministic MOCKED LLM (no network, no API key).
5. Sandbox: approved corrections are JSON overlays - the source CSVs are never changed.
6. Append-only audit log (who / what / when / before / after / why) with a SHA-256 hash chain.

Business rules live in config\workflow_rules.md (v2.0). All detection, gate rules and money
calculations are deterministic code. The app makes no outbound network requests and needs no API
key. The source CSV files are strictly read-only (checked byte-for-byte by the self-test).

Contents
--------
  server.py     Local server (Python 3.9+, standard library only - nothing to install)
  index.html    The whole UI (works offline; no external libraries, fonts or CDNs)
  config\       workflow_rules.md (business rules - required), users.json, permissions.json,
                workflow.json, sla.json, playbooks.json, app.json, agent.json, expected_results.json
  state\        created at run time: audit_log.jsonl, clock.json, state\<dataset>\<scenario>\
  state_archive\  pre-v2.0 state moved here by the one-time migration (never deleted)
  README.txt    This file


START
-----
  cd C:\sources\Utilco\poc1
  py server.py                  (or: python server.py)

The console prints the data folder and the URL, then opens http://127.0.0.1:8501/ .
Sign in by clicking one of the three user cards (simulated sign-in, no passwords).
Use "Switch user" in the header to change persona.

Options:
  py server.py --port 8600          another port
  py server.py --no-browser         don't open the browser
  py server.py --reset              clear tickets, sandboxes and the demo clock (appends DEMO_RESET)
  py server.py --reset --purge-log  ALSO delete the audit log - the only way to clear it
  py server.py --help

Upgrading from the 8-persona version: on first start the old state\ folder is moved to
state_archive\<timestamp>\ and the new audit log starts with a MIGRATION event.

STOP
----
Press Ctrl+C in the server window (or close it). Tickets and the audit log persist in state\.
Stop any older server.py before starting this one - only one process can use port 8501.


POINT THE APP AT A DIFFERENT DATA FOLDER
----------------------------------------
First match wins:
  1. py server.py --data "D:\extracts\utilico"
  2. Environment variable UTILICO_DATA_DIR
       PowerShell:      $env:UTILICO_DATA_DIR = "D:\extracts\utilico"; py server.py
       Command Prompt:  set UTILICO_DATA_DIR=D:\extracts\utilico  (then)  py server.py
  3. Default: C:\sources\Utilco\data

Any folder inside the data folder (up to 2 levels deep) containing ccb_bill_segments.csv is a dataset:
  dataset_A_with_errors           -> "Dataset A - with errors"
  dataset_B_two_errors_corrected  -> "Dataset B - 2 errors corrected"


SCENARIOS (header selector)
---------------------------
  RUN_2026-09-02, RUN_2026-10-02, RUN_2026-11-02   bill-run replays. Draft bills = rows whose
        bill_run_at is that run (01:00); earlier runs count as issued.
  BACKSCAN_2026-11-10                              remediation of POC 1 back-scan findings.
Point-in-time rule: the gate and the agents only see records that existed at the scenario time.
SLA clocks start when the ticket is created (business hours Mon-Fri 08:00-17:00).


A FULL DEMO (RUN_2026-11-02, Dataset A)
---------------------------------------
  1. Maya (Billing Ops)   Bill Run > Run gate: 4 HELD, 1 NON-BLOCKING, 40 PASSED. Agents investigate
                          automatically; open a ticket to watch "Agent activity".
  2. Maya                 BEX-1105 (Copperleaf, G5): confirm the ID match PRM-0O417 -> PRM-00417 with
                          an evidence note, then Confirm triage. Triage the other tickets too;
                          for the L0 tickets (Priya Natarajan, Northgate Dental) keep "Decide now".
  3. Daniel (DRI)         each AWAITING_DECISION ticket > Record decision (recommended pre-selected).
  4. Aisha (Finance)      Finance approvals > approve E1 ($1,436.50) and the REMEDIATE credits.
  5. Whoever decided      Approve plan. The execution agent runs each step + check in the sandbox;
                          the verifier re-runs the gate.
  6. Maya                 Bill Run > "Release / confirm" each ready bill and "Release all passed".
  7. Daniel               DRI digest > review the L0 decisions. Root-cause register > Export.
  8. Anyone               Audit log > Verify integrity / Export CSV.
  Expected: $2,330.89 prevented, $486.28 remediated (Dataset B: $2,134.00 / $180.00).
  BACKSCAN_2026-11-10: Maya > Back-scan > "Send to workflow" per row or "Send all eligible"
  (REVIEW rows are out of scope - revenue protection). Harbourline's credit has an open regulatory
  complaint, so Daniel approves the letter and regulator response on Communications approval.

Demo tools (Self-test & demo screen): reset the current or all scenarios; "Inject failure on next
step"; "Advance demo clock" by N hours (SLA warnings at 75%, escalation at 100%, banner in the
header); "Run end-to-end (autopilot)" plays every persona's recommended actions, each event flagged
"performed by demo autopilot".


AGENTS (MOCKED LLM)
-------------------
server.py defines LLMClient.run_turn(system_prompt, messages, tools) -> {"tool_calls": [...]} or
{"final": {...}}. MockLLMClient (deterministic, scripted from real tool results) is the only one used.
ClaudeLLMClient is a stub. To use a real model later, implement that class and change
config\agent.json "mode" - nothing else changes.
Guardrails: max 12 tool calls per run; tools respect the point-in-time rule; the execution agent may
only call the next approved step; final output is schema-checked and every cited record must exist;
money only comes from calculate_correction; every tool call is logged as AGENT_TOOL_CALL.


SELF-TEST
---------
Self-test & demo screen (or GET /api/selftest): runs every scenario of both datasets in an isolated
in-memory copy (fixed clock, agent latency 0) and checks gate outcomes, routing, priority, DoA,
investigation outputs, the end-to-end autopilot, SoD/permissions, guardrails, SLA escalation,
auto-close, the POC 1 answer keys and that the source CSVs are byte-for-byte unchanged.
Expectations: config\expected_results.json and each dataset's answer_key.csv.


WITHOUT THE SERVER
------------------
Opening index.html directly from disk offers "Choose data folder", which runs only the POC 1
detector in the browser (the workflow, agents and audit log need server.py).


TROUBLESHOOTING
---------------
  "ERROR: the data folder does not exist"  -> check the path, or pass --data "<path>"
  "ERROR: no datasets found"               -> dataset folders must contain ccb_bill_segments.csv
  "workflow_rules.md is missing"           -> put the v2.0 rules file in config\
  "cannot listen on 127.0.0.1:8501"        -> already running, or port in use: --port 8600
  Sign-in fails with KeyError 'role'       -> an older server.py is still running on that port
  "configuration problem"                  -> a file in config\ is missing or not valid JSON
  'py' is not recognized                   -> use "python server.py"
