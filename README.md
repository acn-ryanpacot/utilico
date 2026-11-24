# Utilico Energy — POC 1: Disconnect-vs-Billing Detector + Pre-bill Guard

Finds customers billed for days after their supply was physically disconnected (OMS × CC&B × MDM),
holds risky draft bills before a bill run, and routes each finding as a BEX ticket
(PREVENT / ADVANCE / REMEDIATE) through a three-persona workflow — Billing Ops, DRI, Finance — with
priority, SLAs and escalation, delegation of authority, server-enforced segregation of duties,
scripted (mocked) investigation and execution agents, a sandbox for corrections and a hash-chained
audit log. Business rules: `poc1/config/workflow_rules.md` (v2.0).

All data is fictional. Runs fully offline: Python 3.9+ standard library, no installs, no API keys.

```
cd poc1
py server.py          # opens http://127.0.0.1:8501/
```

| Path | Contents |
|---|---|
| `poc1/server.py` | Local server: data layer, pre-bill gate, ticket workflow, SLAs, agents (mock LLM), audit log, self-test |
| `poc1/index.html` | The whole UI (single file, offline) |
| `poc1/config/` | Workflow rules, users, permissions, workflow state machine, SLAs, playbooks, expected results |
| `poc1/README.txt` | How to start/stop, change the data folder, and a full demo walkthrough |
| `data/` | The two mock datasets (Dataset A with errors, Dataset B with two errors corrected) |

The server looks for data in `C:\sources\Utilco\data` by default; pass `--data "<path>"` or set
`UTILICO_DATA_DIR` when the repository lives elsewhere.
