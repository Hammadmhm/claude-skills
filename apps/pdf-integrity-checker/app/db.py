"""SQLite persistence (WAL mode, one connection per thread)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'submission',  -- submission | reference | web
    status          TEXT NOT NULL DEFAULT 'queued',      -- queued | processing | accepted | rejected | needs_review | error | indexed
    decision_source TEXT,                                -- auto | manual
    plagiarism_pct  REAL,
    ai_pct          REAL,
    pages           INTEGER,
    words           INTEGER,
    reasons         TEXT,     -- JSON list of strings
    details         TEXT,     -- JSON object (matches, AI segments)
    error           TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    processed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);

CREATE TABLE IF NOT EXISTS web_queries (
    provider   TEXT NOT NULL,
    query      TEXT NOT NULL,
    results    TEXT NOT NULL,  -- JSON list of {url, title, snippet}
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, query)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS fingerprints (
    hash   INTEGER NOT NULL,
    doc_id INTEGER NOT NULL,
    PRIMARY KEY (hash, doc_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_fp_doc ON fingerprints(doc_id);
"""

PUBLIC_COLUMNS = (
    "id, filename, sha256, size_bytes, kind, status, decision_source, plagiarism_pct, "
    "ai_pct, pages, words, reasons, error, note, created_at, processed_at, url, stage"
)

# Columns added after the first release; created on startup if missing.
MIGRATIONS = {"url": "TEXT", "stage": "TEXT"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(documents)")}
        for col, typ in MIGRATIONS.items():
            if col not in have:
                c.execute(f"ALTER TABLE documents ADD COLUMN {col} {typ}")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_url ON documents(url) WHERE url IS NOT NULL")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # ---- documents -----------------------------------------------------
    def insert_document(self, filename: str, stored_path: str, sha256: str, size: int, kind: str) -> int:
        cur = self.conn().execute(
            "INSERT INTO documents (filename, stored_path, sha256, size_bytes, kind, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (filename, stored_path, sha256, size, kind, now()),
        )
        return int(cur.lastrowid)

    def get(self, doc_id: int, full: bool = False) -> dict | None:
        cols = "*" if full else PUBLIC_COLUMNS
        row = self.conn().execute(f"SELECT {cols} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return _decode(row) if row else None

    def update(self, doc_id: int, **fields) -> None:
        if not fields:
            return
        for key in ("reasons", "details"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn().execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))

    def list(self, status: str | None, kind: str | None, q: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        else:
            where.append("kind != 'web'")  # web pages are listed only on request
        if q:
            where.append("filename LIKE ?")
            args.append(f"%{q}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        c = self.conn()
        total = c.execute(f"SELECT COUNT(*) FROM documents {clause}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM documents {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return [_decode(r) for r in rows], int(total)

    def all_for_export(self) -> list[dict]:
        rows = self.conn().execute(f"SELECT {PUBLIC_COLUMNS} FROM documents WHERE kind != 'web' ORDER BY id").fetchall()
        return [_decode(r) for r in rows]

    def stats(self) -> dict:
        rows = self.conn().execute(
            "SELECT kind, status, COUNT(*) AS n FROM documents GROUP BY kind, status"
        ).fetchall()
        out = {"total": 0, "references": 0, "web_pages": 0, "by_status": {}}
        for r in rows:
            if r["kind"] == "reference":
                out["references"] += r["n"]
                continue
            if r["kind"] == "web":
                if r["status"] == "indexed":
                    out["web_pages"] += r["n"]
                continue
            out["total"] += r["n"]
            out["by_status"][r["status"]] = r["n"]
        return out

    def pending_ids(self) -> list[int]:
        rows = self.conn().execute(
            "SELECT id FROM documents WHERE status IN ('queued', 'processing') ORDER BY id"
        ).fetchall()
        return [r[0] for r in rows]

    def find_by_sha(self, sha256: str, exclude_id: int) -> dict | None:
        row = self.conn().execute(
            "SELECT id, filename FROM documents WHERE sha256 = ? AND id != ? ORDER BY id LIMIT 1",
            (sha256, exclude_id),
        ).fetchone()
        return dict(row) if row else None

    def delete(self, doc_id: int) -> None:
        c = self.conn()
        c.execute("BEGIN")
        c.execute("DELETE FROM fingerprints WHERE doc_id = ?", (doc_id,))
        c.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        c.execute("COMMIT")

    # ---- web sources ---------------------------------------------------
    def cached_query(self, provider: str, query: str) -> list[dict] | None:
        row = self.conn().execute(
            "SELECT results FROM web_queries WHERE provider = ? AND query = ?", (provider, query)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_query(self, provider: str, query: str, results: list[dict]) -> None:
        self.conn().execute(
            "INSERT OR REPLACE INTO web_queries (provider, query, results, created_at) VALUES (?, ?, ?, ?)",
            (provider, query, json.dumps(results), now()))

    def web_doc_by_url(self, url: str) -> dict | None:
        row = self.conn().execute("SELECT id, kind, status FROM documents WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def insert_web_doc(self, url: str, title: str, words: int, status: str, error: str | None = None) -> int:
        cur = self.conn().execute(
            "INSERT INTO documents (filename, stored_path, sha256, size_bytes, kind, status, decision_source, "
            "words, error, url, created_at, processed_at) VALUES (?, '', '', 0, 'web', ?, 'auto', ?, ?, ?, ?, ?)",
            ((title or url)[:255], status, words, error, url, now(), now()))
        return int(cur.lastrowid)

    # ---- fingerprints --------------------------------------------------
    def store_fingerprints(self, doc_id: int, hashes: list[int]) -> None:
        c = self.conn()
        c.execute("BEGIN")
        c.execute("DELETE FROM fingerprints WHERE doc_id = ?", (doc_id,))
        c.executemany("INSERT OR IGNORE INTO fingerprints (hash, doc_id) VALUES (?, ?)", ((h, doc_id) for h in hashes))
        c.execute("COMMIT")

    def clear_fingerprints(self, doc_id: int) -> None:
        self.conn().execute("DELETE FROM fingerprints WHERE doc_id = ?", (doc_id,))

    def get_fingerprints(self, doc_id: int) -> list[int]:
        return [r[0] for r in self.conn().execute("SELECT hash FROM fingerprints WHERE doc_id = ?", (doc_id,))]

    def overlapping_docs(self, doc_id: int, min_source_matches: int) -> list[dict]:
        """Indexed documents sharing at least `min_source_matches` fingerprints with `doc_id`."""
        rows = self.conn().execute(
            "SELECT d.id, d.kind, d.status, COUNT(*) AS n FROM fingerprints g "
            "JOIN fingerprints f ON f.hash = g.hash AND f.doc_id != g.doc_id "
            "JOIN documents d ON d.id = f.doc_id "
            "WHERE g.doc_id = ? GROUP BY d.id HAVING n >= ?",
            (doc_id, min_source_matches),
        ).fetchall()
        return [dict(r) for r in rows]

    def match_fingerprints(self, doc_id: int, hashes: list[int], min_source_matches: int,
                           compare_all: bool = False, top: int = 10) -> dict:
        """Compare `hashes` against the index.

        By default a submission is compared against reference sources and
        submissions uploaded *before* it (lower id), so the earlier upload is
        treated as the original no matter which finished processing first.
        `compare_all=True` compares against every other document.
        Returns the overall matched share and the top matching sources.
        """
        if not hashes:
            return {"matched": 0, "total": 0, "sources": []}
        c = self.conn()
        c.execute("CREATE TEMP TABLE IF NOT EXISTS q (hash INTEGER PRIMARY KEY)")
        c.execute("DELETE FROM q")
        c.executemany("INSERT OR IGNORE INTO q (hash) VALUES (?)", ((h,) for h in hashes))
        total = c.execute("SELECT COUNT(*) FROM q").fetchone()[0]

        per_source = c.execute(
            "SELECT f.doc_id, COUNT(*) AS n FROM fingerprints f JOIN q ON q.hash = f.hash "
            "JOIN documents d ON d.id = f.doc_id "
            "WHERE f.doc_id != ? AND (? OR d.id < ? OR d.kind IN ('reference', 'web')) "
            "GROUP BY f.doc_id HAVING n >= ? ORDER BY n DESC",
            (doc_id, 1 if compare_all else 0, doc_id, min_source_matches),
        ).fetchall()
        source_ids = [r["doc_id"] for r in per_source]

        matched = 0
        if source_ids:
            c.execute("CREATE TEMP TABLE IF NOT EXISTS s (doc_id INTEGER PRIMARY KEY)")
            c.execute("DELETE FROM s")
            c.executemany("INSERT INTO s (doc_id) VALUES (?)", ((i,) for i in source_ids))
            matched = c.execute(
                "SELECT COUNT(DISTINCT q.hash) FROM q JOIN fingerprints f ON f.hash = q.hash "
                "JOIN s ON s.doc_id = f.doc_id"
            ).fetchone()[0]

        sources = []
        for r in per_source[:top]:
            meta = c.execute("SELECT filename, kind, url FROM documents WHERE id = ?", (r["doc_id"],)).fetchone()
            if meta is None:
                continue
            sources.append({
                "doc_id": r["doc_id"],
                "filename": meta["filename"],
                "kind": meta["kind"],
                "url": meta["url"],
                "shared_fingerprints": r["n"],
                "pct": round(100.0 * r["n"] / total, 1),
            })
        c.execute("DELETE FROM q")
        return {"matched": int(matched), "total": int(total), "sources": sources}


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("reasons", "details"):
        if key in d and d[key]:
            d[key] = json.loads(d[key])
    return d
