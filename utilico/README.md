# AI-TrueBILL — billed-after-disconnection agent workflow (Utilico Energy)

Agents (Orchestrator, Data Retrieval in five parallel lanes, Reconciliation, Recommendation) and one accountable human decision point (the DRI) — catching **customers billed for days after their service was physically disconnected**, and accounts still active after a disconnect. The process ends at the DRI's decision (approved or rejected), which seals the case; nothing is executed afterwards. All data is fictional. Runs fully offline; the LLM is a deterministic mock. Accenture colour theme (colours only, light and dark).

Rules: [`docs/workflow_rules.md`](docs/workflow_rules.md) (v3.2). Stack and versions: [`docs/deck_notes_stack.md`](docs/deck_notes_stack.md).

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

- `python run.py --reset` — clears cases, lookups and evidence (appends `DEMO_RESET`; the audit log is kept).
- `python run.py --reset --purge-log` — also deletes the audit log. Command line only; there is no edit or delete path in the app.

The source CSVs in `C:\sources\Utilco\data\` are read-only and never written (the Self-test checks them byte for byte).

## Using it

Sign in as Maya (CSR) or Daniel (DRI) — no passwords; "Switch user" in the header; pick a dataset. There is no timeline — cases come from two places:

- **Back-scan** (as of the extract end, 10 Nov 2026): the POC 1 detector lists every account billed after a disconnect; Maya clicks **Send to workflow** (per row or all eligible) and the agents reconcile each one and recommend.
- **Account lookup**: Maya enters an account number and an **as-of date**; the agents retrieve and reconcile the account as the data stood at 23:59 that day. A finding opens a case (LK-…); if nothing fires it shows "no issue found" and no case is opened.

Maya confirms triage (and any ID match); Daniel makes the final decision — approve (the recommendation or another option) or reject (release as-is, cancel as false positive). The decision seals the case.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

Plus the in-app Self-test screen (≈190 checks: POC 1 answer keys, back-scan → workflow for both datasets, dated CSR lookups, determinism, SoD/403s, agent guardrails, seals, unchanged CSVs, no outbound connections).

## Offline verification

`OFFLINE_GUARD=1` (default) patches sockets so any non-localhost connection is blocked and logged as `OFFLINE_GUARD_BLOCKED`. ChromaDB runs with `anonymized_telemetry=False` and the embedding function reads `models/` only. If ChromaDB cannot load, the app falls back to keyword similarity and shows "Knowledge store: fallback mode".

## Swapping in production components

- **Real LLM** — implement `ClaudeLLMClient.run_turn()` in `app/agents/llm.py` (Anthropic Messages API with tool use) and set `config/agent.json` `"mode": "claude"`. Tool execution, guardrails, point-in-time reads and validation stay server-side; nothing else changes.
- **Live data** — implement `ContextServiceDataSource` in `app/datasource/__init__.py` (a CDC-fed replicated read store behind one billing-context API, with per-connector lag for the freshness check) and select it in configuration.

## Layout

`run.py` · `scripts/setup.py` · `config/` (users & permissions, agent, sla, triage, freshness, rules/v1.0.json, workflow, seed case history, expected results) · `app/` (FastAPI `main.py`, `auth.py`, `datasource/`, `pit.py`, `rules.py`, `calculator.py`, `intake.py`, `agents/`, `workflow.py`, `evidence.py`, `audit.py`, `knowledge.py`, `outputs.py` (dormant output templates), `kpi.py`, `backscan.py`, `selftest.py`, `engine.py`, `guard.py`) · `static/` · `docs/` · `tests/` · `models/` and `state/` (created at run time).
