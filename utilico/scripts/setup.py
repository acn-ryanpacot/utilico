"""One-time setup for AI-Utilico - the ONLY step that uses the network.

  1. creates .venv with Python 3.14 and installs requirements.txt (exact pins)
  2. downloads the pinned Alpine.js build into static/vendor/
  3. downloads the all-MiniLM-L6-v2 ONNX model files into models/ (via ChromaDB's own downloader,
     which verifies the archive's SHA-256)
  4. writes requirements.lock.txt (pip freeze) and prints the installed versions

Usage (from the utilico folder):   py -3.14 scripts\\setup.py
After this, `python run.py` runs fully offline.
"""
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv")
VPY = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")
ALPINE_VERSION = "3.17.5"
ALPINE_URL = "https://cdn.jsdelivr.net/npm/alpinejs@%s/dist/cdn.min.js" % ALPINE_VERSION
ALPINE_FILE = os.path.join(ROOT, "static", "vendor", "alpine-%s.min.js" % ALPINE_VERSION)
MODEL_DIR = os.path.join(ROOT, "models", "all-MiniLM-L6-v2")
REQUIRED_PY = (3, 14)


def step(msg):
    print("\n== " + msg, flush=True)


def run(cmd):
    print("   $ " + " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT)


def main():
    if sys.version_info[:2] != REQUIRED_PY:
        print("This project is pinned to Python %d.%d; you ran %s.\nRun:  py -%d.%d scripts\\setup.py"
              % (REQUIRED_PY + (sys.version.split()[0],) + REQUIRED_PY))
        return 2
    step("1/4 virtual environment (.venv, Python %s)" % sys.version.split()[0])
    if not os.path.exists(VPY):
        run([sys.executable, "-m", "venv", VENV])
    run([VPY, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", os.path.join(ROOT, "requirements.txt")])

    step("2/4 Alpine.js %s -> static/vendor/" % ALPINE_VERSION)
    os.makedirs(os.path.dirname(ALPINE_FILE), exist_ok=True)
    if not os.path.exists(ALPINE_FILE):
        with urllib.request.urlopen(ALPINE_URL, timeout=60) as r:
            data = r.read()
        if b"Alpine" not in data[:200000]:
            raise SystemExit("Downloaded Alpine.js does not look right")
        with open(ALPINE_FILE, "wb") as fh:
            fh.write(data)
    print("   %s (%d bytes)" % (os.path.relpath(ALPINE_FILE, ROOT), os.path.getsize(ALPINE_FILE)))

    step("3/4 MiniLM embedding model -> models/")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.knowledge import local_embedding_function\n"
        "ef = local_embedding_function(allow_download=True)\n"
        "v = ef(['billed after disconnection'])\n"
        "print('   model ok, embedding dim', len(v[0]))\n" % ROOT
    )
    run([VPY, "-c", code])

    step("4/4 lock file and versions")
    out = subprocess.check_output([VPY, "-m", "pip", "freeze", "--disable-pip-version-check"], cwd=ROOT, text=True)
    with open(os.path.join(ROOT, "requirements.lock.txt"), "w", encoding="utf-8") as fh:
        fh.write("# pip freeze after scripts/setup.py (Python %s)\n" % sys.version.split()[0] + out)
    wanted = {"fastapi", "uvicorn", "pydantic", "pydantic_core", "starlette", "chromadb", "onnxruntime", "tokenizers", "numpy", "pytest", "httpx"}
    for line in out.splitlines():
        if line.split("==")[0].lower().replace("-", "_") in wanted:
            print("   " + line)
    print("   alpinejs==%s" % ALPINE_VERSION)
    print("\nSetup complete. Start the app with:  .venv\\Scripts\\python run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
