POC 1 - Disconnect-vs-Billing Detector + Pre-bill Guard (Utilico Energy)
========================================================================

What it does
------------
1. POC 1 detector (Back-scan / Daily check): finds every account billed for days after a
   confirmed disconnect and quantifies the credit owed.
2. Pre-bill gate: replays historical bill runs and HOLDS risky draft bills before they are issued
   (rules G1-G6).
3. Case workflow for eight roles (Billing Ops, DRI, Field Ops, Master Data, Integration, Finance,
   Regulatory, Auditor). Each user sees only their screens; the server enforces the same permissions.
4. Investigation and execution agents: a real tool-calling loop over the data, driven by a
   deterministic MOCKED LLM (no network, no API key).
5. Sandbox: approved corrections are JSON overlays - the source CSVs are never changed.
6. Immutable audit log (who / what / when) with a SHA-256 hash chain.

All detection, gate rules and money calculations are deterministic code. The app makes no outbound
network requests and needs no API key. The source CSV files are strictly read-only.

Contents
--------
  server.py     Local server (Python 3.9+, standard library only - nothing to install)
  index.html    The whole UI (works offline; no external libraries, fonts or CDNs)
  config\       users.json, permissions.json, agent.json, playbooks.json, app.json,
                expected_gate_results.json   (all editable)
  state\        created at run time: audit_log.jsonl and state\<dataset>\<scenario>\ (cases + sandbox)
  README.txt    This file


START
-----
  cd C:\sources\Utilco\poc1
  py server.py                  (or: python server.py)

The console prints the data folder and the URL, then opens http://127.0.0.1:8501/ .
Sign in by clicking one of the eight user cards (simulated sign-in, no passwords).
Use "Switch user" in the header to change role.

Options:
  py server.py --port 8600          another port
  py server.py --no-browser         don't open the browser
  py server.py --reset              clear all case state and sandboxes (appends DEMO_RESET), then start
  py server.py --reset --purge-log  ALSO delete the audit log - the only way to clear it
  py server.py --help

STOP
----
Press Ctrl+C in the server window (or close it). Case state and the audit log persist in state\.


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
To use new data, replace the CSVs and click Reload / restart the server. No code changes are needed.


SCENARIOS (header selector)
---------------------------
  RUN_2026-09-02, RUN_2026-10-02, RUN_2026-11-02   bill-run replays. Draft bills = rows whose
        bill_run_at is that run (01:00); earlier runs count as issued.
  BACKSCAN_2026-11-10                              remediation of POC 1 back-scan findings.
Point-in-time rule: the gate and the agents only see records that existed at the scenario time
(work orders, integration messages, stop processing, To Dos, CRM cases, reads dated before the day).
State is kept per dataset + scenario; there is one global audit log.


A FULL DEMO (RUN_2026-11-02, Dataset A)
---------------------------------------
  1. Maya Chen (Billing Ops)     Bill Run > Run gate. 4 bills HELD, 1 NON-BLOCKING, 40 PASSED.
  2. Agents investigate automatically - open a case to watch "Agent activity" stream (400 ms per call).
  3. Elena Petrova (Master Data) Master data tasks > Confirm match (PRM-0O417 -> PRM-00417).
  4. Daniel Okoro (DRI)          each case > Record decision (the recommended option is pre-selected).
     Sam Rivera (Field Ops)      Field tasks > record the outcome for the revenue-protection case,
                                 then the DRI decides again.
  5. Aisha Rahman (Finance)      Finance approvals > approve E1 ($1,436.50) and the REMEDIATE credits.
  6. Daniel Okoro (DRI)          each case > Approve plan. The execution agent runs each approved step
                                 and its check in the sandbox; the verifier re-runs the gate.
  7. Maya Chen (Billing Ops)     Bill Run > Release corrected bills and "Release all passed".
  8. Linda Park (Auditor)        Audit log > Verify integrity / Export CSV.
  BACKSCAN_2026-11-10: Billing Ops > Back-scan > "Send findings to workflow"; the Harbourline credit
  routes to Jonas Weber (Regulatory) because of the open regulatory complaint.

Demo tools (Self-test & demo screen): reset the current scenario or all scenarios, and
"Inject failure on next step" (the case returns to the DRI with STEP_FAILED).


AGENTS (MOCKED LLM)
-------------------
server.py defines LLMClient.run_turn(system_prompt, messages, tools) -> {"tool_calls": [...]} or
{"final": {...}}. MockLLMClient (deterministic, scripted from real tool results) is the only one used.
ClaudeLLMClient is a stub. To use a real model later, implement that class and change
config\agent.json "mode" - nothing else changes.
  config\agent.json: {"mode": "mock", "step_latency_ms": 400, "show_agent_activity": true}
Guardrails: max 12 tool calls per run; tools respect the point-in-time rule; the execution agent may
only call the next approved step; final output is schema-checked and every cited record must exist;
money only comes from calculate_correction; every tool call is logged as AGENT_TOOL_CALL.


SELF-TEST
---------
Self-test & demo screen (or GET /api/selftest): runs every scenario of both datasets in an isolated
in-memory copy (agent latency 0) and checks gate outcomes, corrections, remediation credits,
investigation outputs, the end-to-end role workflow, determinism, guardrails, the POC 1 answer keys
and that the source CSVs are byte-for-byte unchanged. Expectations: config\expected_gate_results.json
and each dataset's answer_key.csv.


WITHOUT THE SERVER
------------------
Opening index.html directly from disk offers "Choose data folder", which runs only the POC 1
detector in the browser (the workflow, agents and audit log need server.py).


TROUBLESHOOTING
---------------
  "ERROR: the data folder does not exist"  -> check the path, or pass --data "<path>"
  "ERROR: no datasets found"               -> dataset folders must contain ccb_bill_segments.csv
  "cannot listen on 127.0.0.1:8501"        -> already running, or port in use: --port 8600
  "configuration problem"                  -> a file in config\ is missing or not valid JSON
  'py' is not recognized                   -> use "python server.py"
