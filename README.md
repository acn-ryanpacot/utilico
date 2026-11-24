# Utilico Energy — POC 1: Disconnect-vs-Billing Detector + Pre-bill Guard

Finds customers billed for days after their supply was physically disconnected (OMS × CC&B × MDM),
holds risky draft bills before a bill run, and routes each case through an eight-role workflow with
scripted (mocked) investigation and execution agents, a sandbox for corrections and a hash-chained audit log.

All data is fictional. Runs fully offline: Python 3.9+ standard library, no installs, no API keys.

```
cd poc1
py server.py          # opens http://127.0.0.1:8501/
```

| Path | Contents |
|---|---|
| `poc1/server.py` | Local server: data layer, pre-bill gate, workflow, agents (mock LLM), audit log, self-test |
| `poc1/index.html` | The whole UI (single file, offline) |
| `poc1/config/` | Users, permissions, agent mode, playbooks, expected results |
| `poc1/README.txt` | How to start/stop, change the data folder, and a full demo walkthrough |
| `data/` | The two mock datasets (Dataset A with errors, Dataset B with two errors corrected) |

The server looks for data in `C:\sources\Utilco\data` by default; pass `--data "<path>"` or set
`UTILICO_DATA_DIR` when the repository lives elsewhere.
