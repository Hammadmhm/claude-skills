"""FastAPI application: upload API, results API, and the dashboard page."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .db import Database, now
from .worker import Pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("integrity")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    app.state.db = Database(str(settings.db_path))
    app.state.pipeline = Pipeline(app.state.db, settings)
    resumed = app.state.pipeline.resume_pending()
    log.info("ready: %d worker processes, threshold %.0f%%, AI detector=%s, internet search=%s, resumed %d pending",
             settings.workers, settings.reject_threshold, settings.ai_detector,
             settings.web_search if settings.web_enabled else "off", resumed)
    yield
    app.state.pipeline.shutdown()


app = FastAPI(title="PDF Integrity Checker", lifespan=lifespan)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional site-wide HTTP Basic auth (enabled when APP_PASSWORD is set)."""
    if not settings.app_password or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            if secrets.compare_digest(user, settings.app_username) and secrets.compare_digest(pw, settings.app_password):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="PDF Integrity Checker"'})


def _db(request: Request) -> Database:
    return request.app.state.db


# ---- upload -------------------------------------------------------------
async def _save_upload(upload: UploadFile) -> tuple[str, str, int]:
    """Stream an upload to disk. Returns (path, sha256, size)."""
    limit = settings.max_file_mb * 1024 * 1024
    dest = settings.upload_dir / f"{uuid.uuid4().hex}.pdf"
    sha = hashlib.sha256()
    size = 0
    first = True
    try:
        with open(dest, "wb") as out:
            while chunk := await upload.read(CHUNK):
                if first:
                    if not chunk.lstrip()[:5].startswith(b"%PDF-"):
                        raise ValueError("not a PDF file")
                    first = False
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"larger than {settings.max_file_mb} MB")
                sha.update(chunk)
                out.write(chunk)
        if first:
            raise ValueError("empty file")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return str(dest), sha.hexdigest(), size


@app.post("/api/upload")
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    kind: Literal["submission", "reference"] = Form("submission"),
):
    db, pipeline = _db(request), request.app.state.pipeline
    results = []
    for f in files:
        name = os.path.basename(f.filename or "upload.pdf")[:255]
        try:
            path, sha, size = await _save_upload(f)
        except ValueError as exc:
            results.append({"filename": name, "ok": False, "error": str(exc)})
            continue
        finally:
            await f.close()
        doc_id = db.insert_document(name, path, sha, size, kind)
        pipeline.submit(doc_id)
        results.append({"filename": name, "ok": True, "id": doc_id, "status": "queued"})
    return {"results": results}


# ---- results ------------------------------------------------------------
@app.get("/api/config")
def get_config():
    return {
        "reject_threshold": settings.reject_threshold,
        "max_file_mb": settings.max_file_mb,
        "ai_detector": settings.ai_detector,
        "workers": settings.workers,
        "web_search": settings.web_search if settings.web_enabled else "off",
    }


@app.get("/api/stats")
def stats(request: Request):
    return _db(request).stats()


@app.get("/api/documents")
def list_documents(
    request: Request,
    status: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    items, total = _db(request).list(status, kind, q, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _require(db: Database, doc_id: int, full: bool = False) -> dict:
    doc = db.get(doc_id, full=full)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@app.get("/api/documents/{doc_id}")
def get_document(request: Request, doc_id: int):
    doc = _require(_db(request), doc_id, full=True)
    doc.pop("stored_path", None)
    return doc


@app.get("/api/documents/{doc_id}/file")
def get_file(request: Request, doc_id: int):
    doc = _require(_db(request), doc_id, full=True)
    if doc["kind"] == "web":
        raise HTTPException(404, "Web sources have no stored file; open their URL instead")
    return FileResponse(doc["stored_path"], media_type="application/pdf", filename=doc["filename"])


class Override(BaseModel):
    status: Literal["accepted", "rejected"]
    note: str = ""


@app.post("/api/documents/{doc_id}/override")
def override(request: Request, doc_id: int, body: Override):
    db = _db(request)
    doc = _require(db, doc_id)
    if doc["kind"] != "submission" or doc["status"] in ("queued", "processing"):
        raise HTTPException(409, "Only processed submissions can be overridden")
    db.update(doc_id, status=body.status, decision_source="manual", note=body.note[:2000] or None)
    return db.get(doc_id)


@app.post("/api/documents/{doc_id}/recheck")
def recheck(request: Request, doc_id: int):
    """Re-run analysis and compare against ALL other indexed documents,
    including ones uploaded after this one."""
    db = _db(request)
    doc = _require(db, doc_id)
    if doc["kind"] == "web":
        raise HTTPException(409, "Web sources cannot be re-checked")
    if doc["status"] in ("queued", "processing"):
        raise HTTPException(409, "Already being processed")
    db.clear_fingerprints(doc_id)
    db.update(doc_id, status="queued", decision_source=None, note=None)
    request.app.state.pipeline.submit(doc_id, compare_all=True)
    return db.get(doc_id)


@app.delete("/api/documents/{doc_id}")
def delete_document(request: Request, doc_id: int):
    db = _db(request)
    doc = _require(db, doc_id, full=True)
    request.app.state.pipeline.delete(doc_id)
    Path(doc["stored_path"]).unlink(missing_ok=True)
    return {"deleted": doc_id}


@app.get("/api/export.csv")
def export_csv(request: Request):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "filename", "kind", "status", "decision", "plagiarism_pct", "ai_pct",
                "pages", "words", "reasons", "note", "uploaded_at", "processed_at"])
    for d in _db(request).all_for_export():
        w.writerow([d["id"], d["filename"], d["kind"], d["status"], d["decision_source"] or "",
                    d["plagiarism_pct"] if d["plagiarism_pct"] is not None else "",
                    d["ai_pct"] if d["ai_pct"] is not None else "",
                    d["pages"] or "", d["words"] or "", " | ".join(d["reasons"] or []),
                    d["note"] or "", d["created_at"], d["processed_at"] or ""])
    stamp = now().replace(":", "").replace("-", "")[:15]
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="integrity-results-{stamp}.csv"'})


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
