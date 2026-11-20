"""Background processing pipeline.

    upload ──► queue ──► process pool (extract + fingerprint + AI score, parallel)
                              │
                              ▼
                  index lock (plagiarism match + store fingerprints + decide, serial)

Extraction and AI scoring are CPU-bound and run in parallel worker
processes. Matching against the fingerprint index and inserting the new
document's fingerprints happen one document at a time under a lock, so every
submission is compared against reference sources and submissions uploaded
before it — the same "compare against prior submissions" semantics
commercial checkers use. Because workers finish out of order, indexing a
document also re-scores any later upload (or, for a reference source, any
submission) that overlaps it, so results always follow upload order.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import web_search
from .analysis import analyze
from .config import Settings
from .db import Database, now
from .plagiarism import fingerprints, tokenize

log = logging.getLogger("integrity.worker")


def decide(plagiarism_pct: float, ai_pct: float, threshold: float) -> tuple[str, list[str]]:
    reasons = []
    if plagiarism_pct > threshold:
        reasons.append(f"Plagiarism similarity {plagiarism_pct:.1f}% is above the {threshold:g}% limit")
    if ai_pct > threshold:
        reasons.append(f"AI-generated likelihood {ai_pct:.1f}% is above the {threshold:g}% limit")
    return ("rejected" if reasons else "accepted"), reasons


class Pipeline:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.index_lock = threading.Lock()
        self.procs = ProcessPoolExecutor(
            max_workers=settings.workers,
            mp_context=mp.get_context("spawn"),
            max_tasks_per_child=100,  # recycle workers to cap memory growth
        )
        # Threads only wait on process futures, so a few more than the
        # process count keeps every worker busy.
        self.threads = ThreadPoolExecutor(max_workers=settings.workers * 2, thread_name_prefix="doc")
        # Internet search is I/O-bound and rate limited, so it runs in its own
        # pool and never blocks PDF extraction of the next files.
        self.web_threads = ThreadPoolExecutor(max_workers=max(1, settings.web_concurrency), thread_name_prefix="web")
        self.limiter = web_search.RateLimiter(settings.web_search_qps)
        self._failed_urls: set[str] = set()

    def resume_pending(self) -> int:
        ids = self.db.pending_ids()
        for doc_id in ids:
            self.submit(doc_id)
        return len(ids)

    def submit(self, doc_id: int, compare_all: bool = False) -> None:
        self.threads.submit(self._run_safe, doc_id, compare_all)

    def delete(self, doc_id: int) -> None:
        """Remove a document and re-score submissions that were matched against it."""
        with self.index_lock:
            affected = self._affected_by(self.db.get(doc_id) or {"id": doc_id, "kind": "submission"})
            self.db.delete(doc_id)
        for other in affected:
            self.threads.submit(self._rescore_safe, other)

    def shutdown(self) -> None:
        self.threads.shutdown(wait=False, cancel_futures=True)
        self.web_threads.shutdown(wait=False, cancel_futures=True)
        self.procs.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    def _run_safe(self, doc_id: int, compare_all: bool = False) -> None:
        try:
            self._run(doc_id, compare_all)
        except Exception as exc:  # never let one file kill the worker thread
            log.exception("processing failed for document %s", doc_id)
            self.db.update(doc_id, status="error", stage=None, error=f"Internal error: {exc}", processed_at=now())

    def _run(self, doc_id: int, compare_all: bool = False) -> None:
        doc = self.db.get(doc_id, full=True)
        if doc is None:
            return  # deleted while queued
        self.db.update(doc_id, status="processing", stage="extracting text", error=None)

        future = self.procs.submit(analyze, doc["stored_path"], doc["kind"], self.settings.analysis_options())
        try:
            result = future.result(timeout=self.settings.per_file_timeout_s)
        except FutureTimeout:
            future.cancel()
            self.db.update(doc_id, status="error", stage=None, processed_at=now(),
                           error=f"Timed out after {self.settings.per_file_timeout_s}s")
            return

        if self.db.get(doc_id) is None:
            return  # deleted while processing

        if not result["ok"]:
            self.db.update(doc_id, status="error", stage=None, error=result["error"], pages=0, words=0,
                           processed_at=now())
            return

        if self.settings.web_enabled and doc["kind"] == "submission" and not result.get("needs_review"):
            self.db.update(doc_id, stage="searching the internet")
            self.web_threads.submit(self._web_stage_safe, doc, result, compare_all)
            return
        self._complete(doc, result, compare_all, None)

    def _complete(self, doc: dict, result: dict, compare_all: bool, web_info: dict | None) -> None:
        with self.index_lock:
            if self.db.get(doc["id"]) is None:
                return  # deleted meanwhile
            self._finish(doc, result, compare_all, web_info)
            affected = self._affected_by(doc)
        for other in affected:
            self.threads.submit(self._rescore_safe, other)

    # ---- internet search ---------------------------------------------
    def _web_stage_safe(self, doc: dict, result: dict, compare_all: bool) -> None:
        try:
            web_info = self.web_check(result.get("queries") or [])
            self._complete(doc, result, compare_all, web_info)
        except Exception as exc:
            log.exception("web stage failed for document %s", doc["id"])
            self.db.update(doc["id"], status="error", stage=None, error=f"Internal error: {exc}", processed_at=now())

    def web_check(self, queries: list[str]) -> dict:
        """Search the web for `queries`, download and index the best-matching
        pages. Returns a summary for the document's details."""
        s = self.settings
        info = {"provider": s.web_search, "queries": len(queries), "queries_failed": 0,
                "errors": [], "pages": [], "failed_share": 0.0}
        hits: dict[str, dict] = {}
        for q in queries:
            rows = self.db.cached_query(s.web_search, q)
            if rows is None:
                try:
                    rows = web_search.search(q, s, self.limiter)
                except web_search.SearchError as exc:
                    info["queries_failed"] += 1
                    if str(exc) not in info["errors"]:
                        info["errors"].append(str(exc))
                    continue
                self.db.cache_query(s.web_search, q, rows)
            for r in rows:
                h = hits.setdefault(r["url"], {"title": r.get("title") or "", "hits": 0})
                h["hits"] += 1
        if queries:
            info["failed_share"] = info["queries_failed"] / len(queries)

        ranked = sorted(hits.items(), key=lambda kv: -kv[1]["hits"])[: s.web_max_pages_per_doc]
        for url, meta in ranked:
            page = {"url": url, "title": meta["title"], "query_hits": meta["hits"]}
            existing = self.db.web_doc_by_url(url)
            if existing:
                page["status"] = "already indexed"
            elif url in self._failed_urls:
                page["status"] = "could not be downloaded"
            else:
                try:
                    title, text = web_search.fetch_page(url, s)
                    self._index_web_page(url, title or meta["title"], text)
                    page["status"] = "downloaded"
                except web_search.FetchError as exc:
                    self._failed_urls.add(url)
                    page["status"] = "could not be downloaded"
                    log.info("fetch failed: %s", exc)
            info["pages"].append(page)
        return info

    def _index_web_page(self, url: str, title: str, text: str) -> None:
        s = self.settings
        tokens = tokenize(text)
        hashes = list(fingerprints(tokens, s.shingle_k, s.winnow_w))
        with self.index_lock:
            if self.db.web_doc_by_url(url):
                return  # another thread got there first
            web_id = self.db.insert_web_doc(url, title, len(tokens), "indexed")
            self.db.store_fingerprints(web_id, hashes)
            affected = self._affected_by({"id": web_id, "kind": "web"})
        for other in affected:
            self.threads.submit(self._rescore_safe, other)

    def _affected_by(self, doc: dict) -> list[int]:
        """Finished submissions whose score depends on `doc`: later uploads
        that overlap it (or any overlapping submission, if `doc` is a
        reference source). Call with the index lock held."""
        out = []
        for o in self.db.overlapping_docs(doc["id"], self.settings.min_source_matches):
            if o["kind"] != "submission" or o["status"] not in ("accepted", "rejected"):
                continue
            if doc["kind"] in ("reference", "web") or o["id"] > doc["id"]:
                out.append(o["id"])
        return out

    def _rescore_safe(self, doc_id: int) -> None:
        try:
            self._rescore(doc_id)
        except Exception:
            log.exception("re-scoring failed for document %s", doc_id)

    def _rescore(self, doc_id: int) -> None:
        """Recompute the plagiarism score from stored fingerprints (no re-extraction)."""
        s = self.settings
        with self.index_lock:
            doc = self.db.get(doc_id, full=True)
            if not doc or doc["kind"] != "submission" or doc["status"] not in ("accepted", "rejected"):
                return
            match = self.db.match_fingerprints(doc_id, self.db.get_fingerprints(doc_id), s.min_source_matches)
            plag_pct = round(100.0 * match["matched"] / match["total"], 1) if match["total"] else 0.0
            details = doc["details"] or {}
            details["plagiarism"] = match
            fields = {"plagiarism_pct": plag_pct, "details": details}
            if doc["decision_source"] != "manual" and doc["ai_pct"] is not None:
                fields["status"], fields["reasons"] = decide(plag_pct, doc["ai_pct"], s.reject_threshold)
            self.db.update(doc_id, **fields)

    def _finish(self, doc: dict, result: dict, compare_all: bool = False, web_info: dict | None = None) -> None:
        doc_id = doc["id"]
        s = self.settings
        hashes = result.get("fingerprints") or []
        base = {"pages": result["pages"], "words": result["words"], "processed_at": now(), "stage": None}

        if doc["kind"] == "reference":
            self.db.store_fingerprints(doc_id, hashes)
            self.db.update(doc_id, status="indexed", decision_source="auto", reasons=[], **base)
            return

        if result.get("needs_review"):
            self.db.update(doc_id, status="needs_review", decision_source="auto",
                           reasons=[result["needs_review"]], plagiarism_pct=None, ai_pct=None, **base)
            return

        match = self.db.match_fingerprints(doc_id, hashes, s.min_source_matches, compare_all=compare_all)
        plag_pct = round(100.0 * match["matched"] / match["total"], 1) if match["total"] else 0.0
        details = {"plagiarism": match}
        if web_info is not None:
            details["web"] = web_info
        dup = self.db.find_by_sha(doc["sha256"], doc_id)
        if dup:
            details["exact_duplicate_of"] = dup

        # Index this document so later uploads are compared against it.
        self.db.store_fingerprints(doc_id, hashes)

        ai = result.get("ai")
        ai_pct = ai["ai_pct"] if ai else None
        if ai:
            details["ai"] = ai
        status, reasons = decide(plag_pct, ai_pct or 0.0, s.reject_threshold)

        # A rejection stands on the evidence found. An acceptance only stands
        # if every check actually ran; otherwise a human has to look.
        incomplete = []
        if "ai_error" in result:
            incomplete.append(result["ai_error"])
        if web_info and web_info["failed_share"] > s.web_max_failed_share:
            incomplete.append(
                f"Internet search incomplete ({web_info['queries_failed']} of {web_info['queries']} queries "
                f"failed): {'; '.join(web_info['errors'][:2])}")
        if incomplete:
            if status == "accepted":
                status = "needs_review"
                reasons = incomplete
            else:
                reasons = reasons + incomplete
        self.db.update(doc_id, status=status, decision_source="auto", plagiarism_pct=plag_pct,
                       ai_pct=ai_pct, reasons=reasons, details=details, note=None, **base)
