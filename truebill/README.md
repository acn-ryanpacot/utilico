# AI-TrueBILL — billed-after-disconnection agent workflow (Utilico Energy)

Five agents (Orchestrator, Data Retrieval, Reconciliation, Recommendation, Communications) plus an Execution agent, one accountable human decision point (the DRI), and governed execution in a sandbox — preventing and resolving **customers billed for days after their service was physically disconnected**. All data is fictional. Runs fully offline; the LLM is a deterministic mock.

Rules: [`docs/workflow_rules.md`](docs/workflow_rules.md) (v3.0). Stack and versions: [`docs/deck_notes_stack.md`](docs/deck_notes_stack.md).

## Setup (once — the only step that uses the network)

```powershell
cd C:\sources\Utilco\truebill
py -3.14 scripts\setup.py
```

Creates `.venv` (Python 3.14), installs the exact pins in `requirements.txt`, downloads Alpine.js 3.17.5 to `static/vendor/` and the all-MiniLM-L6-v2 ONNX model to `models/`, and writes `requirements.lock.txt`.

Installed versions: Python 3.14.4 · fastapi 0.162.2 · starlette 1.10.0 · uvicorn 0.54.0 · pydantic 2.14.3 · chromadb 1.5.9 · onnxruntime 1.31.1 · tokenizers 0.23.3 · numpy 2.5.4 · pytest 9.2.0 · httpx 0.28.1 · Alpine.js 3.17.5.

## Run

```powershell
.venv\Scripts\python run.py
```

Starts Uvicorn on `http://127.0.0.1:8501/` and opens the browser. Options: `--port 8600`, `--no-browser`, `--data "D:\extracts"` (or `UTILICO_DATA_DIR`; default `C:\sources\Utilco\data`).

- `python run.py --reset` — clears case state, sandbox, evidence and outputs (appends `DEMO_RESET`; the audit log is kept).
- `python run.py --reset --purge-log` — also deletes the audit log. Command line only; there is no edit or delete path in the app.

The source CSVs in `C:\sources\Utilco\data\` are read-only and never written (the Self-test checks them byte for byte).

## Using it

Sign in as Maya (CSR), Daniel (DRI) or Aisha (Finance) — no passwords; "Switch user" in the header. Pick a dataset and scenario:

| Scenario | What happens |
|---|---|
| `PREVENTION_REPLAY` (1 Aug → 2 Nov) | Pre-bill checks on each bill run; disconnect follow-up (toggle, default on) opens ADVANCE cases a day after each disconnect |
| `DISPUTE_REPLAY` (2 → 10 Nov) | As it actually happened: only disputes arrive (Harbourline 4 Nov, regulatory complaint 9 Nov) |
| `BACKSCAN_2026-11-10` | POC 1 back-scan; the CSR sends findings into the workflow as REMEDIATE cases |

Timeline: Play / Pause / Step, speed (default one scenario-day per second), the scrubber, presets, and "Pause for humans" (default on). Demo tools (Timeline & demo): reset, inject failure on next step, simulate stale MDM feed, **Autopilot** (each persona's next recommended action, logged with `autopilot=true`) and the **Guided demo** (~3 min on DISPUTE_REPLAY, Dataset A).

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Plus the in-app Self-test screen (≈510 checks: POC 1 answer keys, every §19 expected result for both datasets and modes, determinism, SoD/403s, agent guardrails, injected failure, stale feed, seals, unchanged CSVs, no outbound connections).

## Offline verification

`OFFLINE_GUARD=1` (default) patches sockets so any non-localhost connection is blocked and logged as `OFFLINE_GUARD_BLOCKED`. ChromaDB runs with `anonymized_telemetry=False` and the embedding function reads `models/` only. If ChromaDB cannot load, the app falls back to keyword similarity and shows "Knowledge store: fallback mode".

## Swapping in production components

- **Real LLM** — implement `ClaudeLLMClient.run_turn()` in `app/agents/llm.py` (Anthropic Messages API with tool use) and set `config/agent.json` `"mode": "claude"`. Tool execution, guardrails, point-in-time reads and validation stay server-side; nothing else changes.
- **Live data** — implement `ContextServiceDataSource` in `app/datasource/__init__.py` (a CDC-fed replicated read store behind one billing-context API, with per-connector lag for the freshness check) and select it in configuration.

## Layout

`run.py` · `scripts/setup.py` · `config/` (users & permissions, agent, sla, triage, freshness, rules/v1.0.json, workflow, seed case history, expected results) · `app/` (FastAPI `main.py`, `auth.py`, `datasource/`, `pit.py`, `events.py`, `rules.py`, `calculator.py`, `prebill.py`, `sandbox.py`, `agents/`, `workflow.py`, `evidence.py`, `audit.py`, `knowledge.py`, `outputs.py`, `kpi.py`, `backscan.py`, `selftest.py`, `engine.py`, `guard.py`) · `static/` · `docs/` · `tests/` · `models/` and `state/` (created at run time).
