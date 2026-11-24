"""Sandbox overlays (prompt §10): JSON under state/<dataset>/<scenario>/sandbox/, layered on the point-in-time view.

Row overlays replace (or add) one row of one table: SA stop changes, premise-reference corrections, reprocessed
messages, closed To Dos, regenerated / cancelled drafts, credit memos, CRM notes. Read rules zero the reads of a meter
in a window (kwh 0, quality CORRECTED) as they become visible. The source CSVs are never written.
"""
import os

from .util import read_json, write_json_atomic


class Sandbox:
    def __init__(self, folder=None):
        self.folder = folder
        self.path = os.path.join(folder, "overlays.json") if folder else None
        self.overlays = (read_json(self.path, []) if self.path else []) or []

    def _next(self):
        return "OV-%05d" % (len(self.overlays) + 1)

    def _save(self):
        if self.path:
            write_json_atomic(self.path, self.overlays)

    def set_row(self, file, key_field, key, row, case_id, step, scenario_time):
        """Idempotent: a second correction of the same row by the same case updates that overlay."""
        for ov in self.overlays:
            if ov.get("kind") == "row" and ov["file"] == file and ov["key"] == key and ov["case_id"] == case_id:
                ov["row"].update(row)
                ov["step"] = step
                self._save()
                return ov
        ov = {"overlay_id": self._next(), "kind": "row", "file": file, "key_field": key_field, "key": key, "row": dict(row),
              "case_id": case_id, "step": step, "scenario_time": scenario_time}
        self.overlays.append(ov)
        self._save()
        return ov

    def add_read_rule(self, meter, from_date, to_date, case_id, step, scenario_time):
        for ov in self.overlays:
            if ov.get("kind") == "read_rule" and ov["meter"] == meter and ov["from_date"] == from_date and ov.get("to_date") == to_date:
                return ov
        ov = {"overlay_id": self._next(), "kind": "read_rule", "meter": meter, "from_date": from_date, "to_date": to_date, "kwh": 0,
              "quality": "CORRECTED", "case_id": case_id, "step": step, "scenario_time": scenario_time, "file": "mdm_daily_reads.csv"}
        self.overlays.append(ov)
        self._save()
        return ov

    def by_case(self, case_id):
        return [o for o in self.overlays if o["case_id"] == case_id]

    def creator(self, file, key):
        """Case that created the overlay of a row (to credit $ prevented)."""
        for ov in self.overlays:
            if ov.get("kind") == "row" and ov["file"] == file and ov["key"] == key:
                return ov["case_id"]
        return None
