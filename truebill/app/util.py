"""Shared helpers: paths, Decimal money, dates, string similarity, canonical JSON, atomic file writes."""
import hashlib
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
CONFIG_DIR = os.path.join(ROOT, "config")
STATIC_DIR = os.path.join(ROOT, "static")
DOCS_DIR = os.path.join(ROOT, "docs")
MODELS_DIR = os.path.join(ROOT, "models")
STATE_DIR = os.path.join(ROOT, "state")
DEFAULT_DATA_DIR = r"C:\sources\Utilco\data"
RULE_SET_VERSION = "v1.0"
GENESIS_HASH = "0" * 64
TS_FMT = "%Y-%m-%d %H:%M"

CENT = Decimal("0.01")
TENTH = Decimal("0.1")


# ---- money (Decimal, ROUND_HALF_UP everywhere) ---------------------------------------------
def D(x):
    if isinstance(x, Decimal):
        return x
    if x in (None, ""):
        return Decimal("0")
    return Decimal(str(x))


def money(x):
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def kwh1(x):
    return D(x).quantize(TENTH, rounding=ROUND_HALF_UP)


def f2(x):
    """Decimal -> float for JSON (already rounded to cents)."""
    return float(money(x))


def fmt_money(x):
    return "${:,.2f}".format(money(x))


# ---- dates and scenario timestamps ("YYYY-MM-DD HH:MM", local) ------------------------------
def add_days(d, n):
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def days_between(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def day_range(a, b):
    out, d, e = [], date.fromisoformat(a), date.fromisoformat(b)
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def parse_ts(s):
    return datetime.strptime(s, TS_FMT)


def fmt_ts(dt):
    return dt.strftime(TS_FMT)


def ts_add(s, hours=0, minutes=0):
    return fmt_ts(parse_ts(s) + timedelta(hours=hours, minutes=minutes))


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def now_local():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def month_end(d):
    y, m = int(d[:4]), int(d[5:7])
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


# ---- similarity -------------------------------------------------------------------------------
def levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        for j in range(1, len(b) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if a[i - 1] == b[j - 1] else 1)))
        prev = cur
    return prev[len(b)]


def fold_lookalikes(s):
    """Treat letter O as zero and letter I as one."""
    return s.upper().replace("O", "0").replace("I", "1")


# ---- hashing / JSON ---------------------------------------------------------------------------
def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json_atomic(path, obj):
    """Temp file + replace. Windows can briefly refuse the replace while a scanner holds the file, so retry."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.dumps(obj, indent=1, ensure_ascii=False, default=str)
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02 * (attempt + 1))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.remove(tmp)
    except OSError:
        pass


def slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


class AppError(Exception):
    status = 400


class PermissionDenied(AppError):
    status = 403


class NotFound(AppError):
    status = 404
