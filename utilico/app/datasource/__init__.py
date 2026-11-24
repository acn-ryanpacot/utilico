"""Data access abstraction (prompt §6).

DataSource is the interface every reader uses; one Connector per source system reports its rows and its lag.
CsvDataSource reads the read-only CSV extracts. ContextServiceDataSource is the production design (stub).
"""
import csv
import os
import re

from ..util import NotFound, sha256_file

F_ACC, F_SA, F_BILL, F_TAR, F_TODO = ("ccb_accounts.csv", "ccb_service_agreements.csv", "ccb_bill_segments.csv",
                                      "ccb_tariffs.csv", "ccb_todo_entries.csv")
F_MTR, F_READ, F_WO, F_CRM = "mdm_meters.csv", "mdm_daily_reads.csv", "oms_work_orders.csv", "crm_cases.csv"
F_GL, F_PER, F_MSG, F_XW, F_KEY = ("gl_journal_lines.csv", "gl_periods.csv", "integration_message_log.csv",
                                   "ref_id_crosswalk.csv", "answer_key.csv")
F_MEMO, F_NOTE = "sandbox_credit_memos", "sandbox_crm_notes"     # virtual tables that exist only in the sandbox
SOURCE_FILES = (F_ACC, F_SA, F_BILL, F_TAR, F_TODO, F_MTR, F_READ, F_WO, F_CRM, F_GL, F_PER, F_MSG, F_XW, F_KEY)
KEY_FIELD = {F_ACC: "account_id", F_SA: "sa_id", F_BILL: "bill_id", F_TAR: "tariff_code", F_TODO: "todo_id",
             F_MTR: "mdm_meter_id", F_READ: "read_id", F_WO: "wo_id", F_CRM: "case_id", F_GL: None,
             F_PER: "accounting_period", F_MSG: "msg_id", F_XW: "oms_premise_id", F_KEY: "case",
             F_MEMO: "memo_id", F_NOTE: "note_id"}
# connector -> files it owns
CONNECTORS = {
    "CCB": (F_ACC, F_SA, F_BILL, F_TAR, F_TODO),
    "MDM": (F_MTR, F_READ),
    "OMS": (F_WO,),
    "CRM": (F_CRM,),
    "GL": (F_GL, F_PER),
    "INTEGRATION": (F_MSG,),
    "CROSSWALK": (F_XW,),
}
SYSTEM_LABEL = {"CCB": "CC&B", "MDM": "MDM", "OMS": "OMS", "CRM": "CRM", "GL": "GL", "INTEGRATION": "Integration",
                "CROSSWALK": "Master data", "ORACLE": "Test oracle", "SANDBOX": "Sandbox"}
SYSTEM_OF_FILE = {f: c for c, fs in CONNECTORS.items() for f in fs}
SYSTEM_OF_FILE[F_KEY] = "ORACLE"
SYSTEM_OF_FILE[F_MEMO] = SYSTEM_OF_FILE[F_NOTE] = "SANDBOX"
DISPLAY_NAMES = {"dataset_A_with_errors": "Dataset A — with errors",
                 "dataset_B_two_errors_corrected": "Dataset B — 2 errors corrected"}


def read_csv(path):
    """RFC-4180 CSV (quoted commas, quotes and line breaks). Opened read-only."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not r or (len(r) == 1 and r[0] == ""):
            continue
        out.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(headers)})
    return headers, out


class Connector:
    """One source system. lag_minutes() is how far behind the replica is (always 0 for CSV extracts)."""

    def __init__(self, name, tables, headers):
        self.name, self.tables, self.headers = name, tables, headers
        self.simulated_lag = 0

    def lag_minutes(self):
        return self.simulated_lag

    def rows(self, file):
        return self.tables.get(file, [])


class DataSource:
    """Interface: a dataset made of per-system connectors."""
    id = name = folder = None

    def connector(self, name):
        raise NotImplementedError

    def table(self, file):
        raise NotImplementedError

    def headers(self, file):
        raise NotImplementedError

    def freshness(self):
        """{connector: lag minutes}"""
        raise NotImplementedError


class CsvDataSource(DataSource):
    def __init__(self, folder, ds_id=None):
        self.folder = folder
        base = os.path.basename(folder.rstrip("\\/"))
        self.id = ds_id or re.sub(r"[^A-Za-z0-9_.-]", "_", base)
        self.name = DISPLAY_NAMES.get(base, base)
        self._tables, self._headers = {}, {}
        for f in SOURCE_FILES:
            p = os.path.join(folder, f)
            if os.path.isfile(p):
                self._headers[f], self._tables[f] = read_csv(p)
        self.files_present = [f for f in SOURCE_FILES if f in self._tables]
        self.files_missing = [f for f in SOURCE_FILES if f not in self._tables]
        self.connectors = {c: Connector(c, {f: self._tables.get(f, []) for f in fs}, {f: self._headers.get(f, []) for f in fs})
                           for c, fs in CONNECTORS.items()}
        # sample case codes (E1.. N5) by account, matched on customer_name from answer_key.csv (presenter labels / tests)
        by_name = {}
        for a in self._tables.get(F_ACC, []):
            by_name.setdefault(a["customer_name"].strip().lower(), []).append(a["account_id"])
        self.case_codes, self.accounts_by_code = {}, {}
        for k in self._tables.get(F_KEY, []):
            ids = by_name.get(k["customer_name"].strip().lower(), [])
            self.accounts_by_code[k["case"]] = ids
            for i in ids:
                self.case_codes[i] = k["case"]

    def connector(self, name):
        return self.connectors[name]

    def table(self, file):
        return self._tables.get(file, [])

    def headers(self, file):
        return self._headers.get(file, [])

    def freshness(self):
        return {c: k.lag_minutes() for c, k in self.connectors.items()}

    def source_hashes(self):
        return {f: sha256_file(os.path.join(self.folder, f)) for f in self.files_present}


class ContextServiceDataSource(DataSource):
    """Production design (not implemented in the POC).

    A replicated read store fed by change-data capture (CDC) from CC&B, MDM, OMS, CRM, GL, the integration hub and
    the master-data crosswalk, exposed through ONE billing-context API ("give me everything about account X / premise
    Y / meter Z as of time T"). Each connector reports its replication lag so the freshness check can block a bill
    run on stale data. Swapping it in means implementing connector()/table()/headers()/freshness() against that API
    and naming it in configuration - no agent, rule or workflow code changes.
    """

    def __init__(self, base_url=None, **kwargs):
        raise NotImplementedError("ContextServiceDataSource is the production design; the POC uses CsvDataSource")


def discover(data_dir):
    """Every sub-folder of data_dir (two levels deep) that contains ccb_bill_segments.csv."""
    out = []
    root = os.path.abspath(data_dir)
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "__")))
        if depth >= 2:
            dirnames[:] = []
        if depth and F_BILL in filenames:
            out.append(dirpath)
    return sorted(out, key=str.lower)


class Registry:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._cache = {}

    def list(self):
        out = []
        for folder in discover(self.data_dir):
            ds = self.get_by_folder(folder)
            out.append({"id": ds.id, "name": ds.name, "folder": folder, "files_present": ds.files_present,
                        "files_missing": ds.files_missing})
        return out

    def get_by_folder(self, folder):
        if folder not in self._cache:
            self._cache[folder] = CsvDataSource(folder)
        return self._cache[folder]

    def get(self, ds_id):
        for folder in discover(self.data_dir):
            ds = self.get_by_folder(folder)
            if ds.id == ds_id:
                return ds
        raise NotFound("Unknown dataset: %s" % ds_id)
