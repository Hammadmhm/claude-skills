"""CPU-heavy per-document work. Runs inside worker processes.

Must stay importable and picklable on its own (no DB access here): the
process pool sends a file path + options in and gets a plain dict back.
"""

from __future__ import annotations

from . import ai_detect
from .extract import ExtractionError, extract_text, strip_bibliography, strip_quotes
from .plagiarism import fingerprints, tokenize
from .web_search import select_queries


def analyze(path: str, kind: str, opts: dict) -> dict:
    """Extract, fingerprint and (for submissions) AI-score one PDF."""
    try:
        text, pages = extract_text(path)
    except ExtractionError as exc:
        return {"ok": False, "error": str(exc), "pages": 0, "words": 0}

    body = strip_bibliography(text) if opts["exclude_bibliography"] else text
    words = len(tokenize(body))
    result = {"ok": True, "pages": pages, "words": words, "error": None}

    if words < opts["min_words"]:
        result["needs_review"] = (
            f"Only {words} words of text could be extracted — the PDF is probably scanned "
            "images and needs OCR or a manual check."
        )
        result["fingerprints"] = []
        return result

    plag_text = strip_quotes(body) if opts["exclude_quotes"] else body
    result["fingerprints"] = list(fingerprints(tokenize(plag_text), opts["shingle_k"], opts["winnow_w"]))

    if kind == "submission":
        result["queries"] = select_queries(plag_text, opts.get("web_queries", 0))
        try:
            result["ai"] = ai_detect.detect(body, opts)
        except ai_detect.DetectorError as exc:
            result["ai_error"] = str(exc)
    return result
