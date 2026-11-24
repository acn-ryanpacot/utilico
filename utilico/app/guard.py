"""Runtime offline guard (prompt §3): with OFFLINE_GUARD=1 (default) every non-localhost socket connection is
blocked and reported. Also sets the offline environment variables for ChromaDB / Hugging Face / ONNX tooling."""
import ipaddress
import os
import socket
import threading
from contextlib import contextmanager

OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "ANONYMIZED_TELEMETRY": "False",
               "CHROMA_TELEMETRY": "False", "POSTHOG_DISABLED": "1", "DO_NOT_TRACK": "1"}
blocked = []            # attempts seen since start: [(host, port)]
_listeners = []
_installed = False
_model_request = threading.local()


@contextmanager
def model_connection():
    previous = getattr(_model_request, "enabled", False)
    _model_request.enabled = True
    try:
        yield
    finally:
        _model_request.enabled = previous


def set_offline_env():
    for k, v in OFFLINE_ENV.items():
        os.environ.setdefault(k, v)


def _is_local(host):
    if host in (None, "", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return host.endswith(".localhost")


def _target(address):
    if isinstance(address, tuple) and address:
        return address[0], address[1] if len(address) > 1 else None
    return str(address), None       # AF_UNIX paths etc. are local


class OfflineGuardError(ConnectionRefusedError):
    pass


def _report(host, port):
    blocked.append((host, port))
    for fn in list(_listeners):
        try:
            fn(host, port)
        except Exception:
            pass


def install(listener=None):
    global _installed
    if listener:
        _listeners.append(listener)
    if _installed or os.environ.get("OFFLINE_GUARD", "1") != "1":
        return _installed
    orig_connect, orig_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        host, port = _target(address)
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(host) and not getattr(_model_request, "enabled", False):
            _report(host, port)
            raise OfflineGuardError("OFFLINE_GUARD blocked an outbound connection to %s:%s" % (host, port))
        return orig_connect(self, address)

    def connect_ex(self, address):
        host, port = _target(address)
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_local(host) and not getattr(_model_request, "enabled", False):
            _report(host, port)
            return 111
        return orig_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    _installed = True
    return True
