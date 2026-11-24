"""Knowledge store (prompt §14): ChromaDB 1.5.9 with local all-MiniLM-L6-v2 embeddings; keyword fallback.

Collections: rules (docs/workflow_rules.md + rule catalogue), thresholds (sla/triage/freshness), case_history
(seed_case_history.json + every sealed case). Similar past cases are context only - they never change the
recommendation's option or confidence.
"""
import json
import logging
import math
import os
import re
import uuid

from .util import DOCS_DIR, MODELS_DIR

log = logging.getLogger("utilico.knowledge")
MODEL_PATH = os.path.join(MODELS_DIR, "all-MiniLM-L6-v2")
COLLECTIONS = ("rules", "thresholds", "case_history")


def local_embedding_function(allow_download=False):
    """ChromaDB's ONNX MiniLM function pointed at models/ (never ~/.cache, never the network at runtime)."""
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    from pathlib import Path

    class LocalMiniLM(ONNXMiniLM_L6_V2):
        DOWNLOAD_PATH = Path(MODEL_PATH)

        def _download_model_if_not_exists(self):
            if not allow_download and not os.path.exists(os.path.join(MODEL_PATH, "onnx", "model.onnx")):
                raise RuntimeError("MiniLM model files missing in models/ - run scripts/setup.py")
            return super()._download_model_if_not_exists()

    return LocalMiniLM(preferred_providers=["CPUExecutionProvider"])


def _chunks_from_markdown(text, prefix):
    parts, cur, title = [], [], prefix
    for line in text.splitlines():
        if line.startswith("#"):
            if cur:
                parts.append((title, "\n".join(cur).strip()))
            title, cur = line.lstrip("#").strip(), []
        else:
            cur.append(line)
    if cur:
        parts.append((title, "\n".join(cur).strip()))
    return [(t, b) for t, b in parts if b]


def base_documents(cfg):
    docs = {"rules": [], "thresholds": [], "case_history": []}
    path = os.path.join(DOCS_DIR, "workflow_rules.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for i, (t, b) in enumerate(_chunks_from_markdown(fh.read(), "workflow_rules")):
                docs["rules"].append(("RULES-%03d" % (i + 1), "%s\n%s" % (t, b[:1500]), {"source": "docs/workflow_rules.md", "title": t}))
    rc = cfg.rules()
    for k, v in list(rc["reconciliation"].items()) + list(rc["prebill"].items()):
        docs["rules"].append(("CAT-" + k, "%s %s: fires when %s" % (k, v["name"], v["fires_when"]), {"source": "config/rules/v1.0.json", "title": k}))
    for c in rc["causes"]:
        docs["rules"].append(("CAT-" + c["code"], "Cause %s %s: %s. System fixes %s" % (c["code"], c["label"], c["condition"], ", ".join(c["s_codes"])),
                              {"source": "config/rules/v1.0.json", "title": c["code"]}))
    for name in ("sla.json", "triage.json", "freshness.json"):
        data = cfg.get(name)
        docs["thresholds"].append(("THR-" + name.split(".")[0], "%s: %s" % (name, json.dumps({k: v for k, v in data.items() if k != "_comment"})),
                                   {"source": "config/" + name, "title": name}))
    for h in cfg.seed_history():
        docs["case_history"].append((h["id"], history_text(h), history_meta(h)))
    return docs


def history_text(h):
    return "%s case closed %s: %s. Cause %s. Resolution: %s. Amount $%.2f, %s manual days." % (
        h["segment"], h["closed"], h["pattern"], h["cause"], h["resolution"], float(h["amount"]), h.get("manual_days", "-"))


def history_meta(h):
    return {"closed": h["closed"], "segment": h["segment"], "pattern": h["pattern"], "cause": h["cause"],
            "resolution": h["resolution"], "amount": float(h["amount"]), "manual_days": h.get("manual_days") if h.get("manual_days") is not None else -1,
            "source": h.get("source", "seed")}


_TOKEN = re.compile(r"[a-z0-9+]+")


def _tokens(s):
    return [t for t in _TOKEN.findall(s.lower()) if len(t) > 1]


def keyword_score(q, doc):
    qt, dt = set(_tokens(q)), _tokens(doc)
    if not qt or not dt:
        return 0.0
    ds = set(dt)
    return len(qt & ds) / math.sqrt(len(qt) * len(ds))


class KnowledgeBase:
    """Process-wide: the embedding model and the embeddings of the base documents (computed once)."""

    def __init__(self, cfg, force_fallback=False):
        self.cfg = cfg
        self.mode, self.error = "chromadb", None
        self.docs = base_documents(cfg)
        self.ef = None
        self.embeddings = {}
        self._ephemeral = None
        if force_fallback:
            self.mode, self.error = "fallback", "forced"
            return
        try:
            import chromadb  # noqa: F401
            self.ef = local_embedding_function()
            for coll, rows in self.docs.items():
                self.embeddings[coll] = [list(map(float, v)) for v in self.ef([r[1] for r in rows])] if rows else []
        except Exception as e:     # any failure -> fallback mode, the app still runs
            self.mode, self.error = "fallback", "%s: %s" % (type(e).__name__, e)
            log.warning("Knowledge store: fallback mode (%s)", self.error)

    def embed(self, texts):
        return [list(map(float, v)) for v in self.ef(texts)]

    def ephemeral_client(self):
        if self._ephemeral is None:
            import chromadb
            from chromadb.config import Settings
            self._ephemeral = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False, allow_reset=True))
        return self._ephemeral


class KnowledgeStore:
    """One per dataset + scenario. folder=None -> in-memory (self-test)."""

    def __init__(self, kb, folder=None):
        self.kb, self.folder = kb, folder
        self.mode = kb.mode
        self.extra = []          # sealed cases added in this scenario (fallback mode keeps them here too)
        self.colls = {}
        if self.mode == "chromadb":
            try:
                import chromadb
                from chromadb.config import Settings
                if folder:
                    os.makedirs(folder, exist_ok=True)
                    self.client = chromadb.PersistentClient(path=folder, settings=Settings(anonymized_telemetry=False, allow_reset=True))
                    self.prefix = ""
                else:
                    self.client = kb.ephemeral_client()
                    self.prefix = "s" + uuid.uuid4().hex[:10] + "_"
                self._open(seed_if_empty=True)
            except Exception as e:
                self.mode = "fallback"
                kb.error = kb.error or "%s: %s" % (type(e).__name__, e)
                log.warning("Knowledge store: fallback mode (%s)", e)

    def _open(self, seed_if_empty):
        for c in COLLECTIONS:
            col = self.client.get_or_create_collection(self.prefix + c, embedding_function=self.kb.ef, metadata={"hnsw:space": "cosine"})
            self.colls[c] = col
            if seed_if_empty and self.kb.docs[c]:
                # always re-sync the base documents (rules doc / thresholds / seed history may have changed);
                # sealed cases added to case_history are kept
                rows = self.kb.docs[c]
                col.upsert(ids=[r[0] for r in rows], documents=[r[1] for r in rows], metadatas=[r[2] for r in rows], embeddings=self.kb.embeddings[c])
                if c != "case_history":
                    stale = [i for i in col.get(include=[])["ids"] if i not in {r[0] for r in rows}]
                    if stale:
                        col.delete(ids=stale)

    def reset(self):
        self.extra = []
        if self.mode == "chromadb":
            for c in COLLECTIONS:
                try:
                    self.client.delete_collection(self.prefix + c)
                except Exception:
                    pass
            self.colls = {}
            self._open(seed_if_empty=True)

    def add_case(self, h):
        self.extra.append(h)
        if self.mode == "chromadb":
            text = history_text(h)
            self.colls["case_history"].upsert(ids=[h["id"]], documents=[text], metadatas=[history_meta(h)], embeddings=self.kb.embed([text]))

    def search(self, query, collection="case_history", k=3):
        if self.mode == "chromadb":
            col = self.colls[collection]
            n = min(k, col.count())
            if not n:
                return []
            r = col.query(query_embeddings=self.kb.embed([query]), n_results=n)
            out = []
            for i, doc_id in enumerate(r["ids"][0]):
                out.append({"id": doc_id, "text": r["documents"][0][i], "metadata": r["metadatas"][0][i],
                            "score": round(1.0 - float(r["distances"][0][i]), 4)})
            return out
        rows = list(self.kb.docs[collection])
        if collection == "case_history":
            rows += [(h["id"], history_text(h), history_meta(h)) for h in self.extra]
        scored = sorted(((keyword_score(query, r[1]), r) for r in rows), key=lambda x: (-x[0], x[1][0]))[:k]
        return [{"id": r[0], "text": r[1], "metadata": r[2], "score": round(s, 4)} for s, r in scored]
