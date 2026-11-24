"""AI-TrueBILL - start the local app.

  python run.py                  start Uvicorn on 127.0.0.1:8501 and open the browser
  python run.py --reset          clear case state, sandbox and outputs (appends DEMO_RESET), then start
  python run.py --reset --purge-log   ALSO delete the audit log (the only way to clear it)
  python run.py --no-browser | --port 8600 | --data "D:\\extracts"
"""
import argparse
import os
import shutil
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app import guard  # noqa: E402

guard.set_offline_env()            # before chromadb / onnx are imported


def main():
    ap = argparse.ArgumentParser(description="AI-TrueBILL (local, offline, mocked agents)")
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--data", default=None, help="data folder (default C:\\sources\\Utilco\\data or UTILICO_DATA_DIR)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--purge-log", action="store_true")
    a = ap.parse_args()
    if a.purge_log and not a.reset:
        ap.error("--purge-log must be used together with --reset")
    from app.util import STATE_DIR, DEFAULT_DATA_DIR
    from app.audit import AuditLog
    data_dir = a.data or os.environ.get("UTILICO_DATA_DIR") or DEFAULT_DATA_DIR
    if not os.path.isdir(data_dir):
        print("ERROR: the data folder does not exist: %s" % data_dir)
        return 2
    if a.reset:
        for d in os.listdir(STATE_DIR) if os.path.isdir(STATE_DIR) else []:
            p = os.path.join(STATE_DIR, d)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
        log = AuditLog(os.path.join(STATE_DIR, "audit_log.jsonl"))
        if a.purge_log:
            log.purge()
            log = AuditLog(os.path.join(STATE_DIR, "audit_log.jsonl"))
        log.append(actor_type="SYSTEM", actor_id="sys.cli", actor_name="System (command line)", action="DEMO_RESET", object_type="state",
                   object_id="all", reason="python run.py --reset%s: case state, sandbox and outputs cleared" % (" --purge-log (audit log purged)" if a.purge_log else "; audit log kept"))
        print("State reset%s." % (" and audit log purged" if a.purge_log else " (audit log kept)"))
    guard.install()
    import uvicorn
    from app.main import create_app
    print("AI-TrueBILL - billed-after-disconnection agent workflow (Utilico Energy, fictional data)")
    print("  Data folder : %s (read-only)" % data_dir)
    print("  Offline     : OFFLINE_GUARD=%s - non-localhost connections are blocked and logged" % os.environ.get("OFFLINE_GUARD", "1"))
    app = create_app(data_dir)
    kb = app.state.tb.kb
    print("  Knowledge   : %s%s" % (kb.mode, " (%s)" % kb.error if kb.error else ""))
    print("  Agent       : mock (scripted), no API key")
    url = "http://127.0.0.1:%d/" % a.port
    print("  URL         : %s   (Ctrl+C to stop)" % url)
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
