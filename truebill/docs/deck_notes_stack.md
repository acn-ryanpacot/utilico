# Deck slide 5 - tech stack (speaker notes)

Versions actually installed by `scripts/setup.py` on Windows 11 (2026-11-24). Every pin is the newest release at least two weeks old at build time.

| Layer | Technology | Installed version |
|---|---|---|
| Language | Python | **3.14.4** (as in the deck - no deviation) |
| Backend | FastAPI on Uvicorn, Pydantic v2 models for every API body and agent output | fastapi 0.162.2 · starlette 1.10.0 · uvicorn 0.54.0 · pydantic 2.14.3 (pydantic-core 2.50.4) |
| Frontend | Plain HTML + Alpine.js, vendored in `static/vendor/` (no CDN, no build step); charts as inline SVG | Alpine.js **3.17.5** |
| Live updates | Server-Sent Events (FastAPI `StreamingResponse`, `text/event-stream`) | - |
| Vector store | ChromaDB with local MiniLM embeddings (all-MiniLM-L6-v2, ONNX) | chromadb **1.5.9** · onnxruntime 1.31.1 · tokenizers 0.23.3 · numpy 2.5.4 |
| Orchestrator / agents | Mock: deterministic rules + templated agent text behind a swappable `LLMClient` (`ClaudeLLMClient` stub) | - |
| Tests | pytest (+ httpx for FastAPI's TestClient) | pytest 9.2.0 · httpx 0.28.1 |

Notes for the speaker:

- Python 3.14 worked with every pin, including ChromaDB 1.5.9 (abi3 wheel) and ONNX Runtime 1.31.1 (cp314 wheel) - no fallback version was needed.
- The only network access is the one-time `scripts/setup.py` (pip packages, the Alpine.js file, the MiniLM model). At runtime an offline guard (`OFFLINE_GUARD=1`) blocks and logs any non-localhost connection; ChromaDB telemetry is disabled.
- The full resolved dependency list is in `requirements.lock.txt`.
