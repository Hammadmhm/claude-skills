"""PDF text extraction and pre-processing."""

from __future__ import annotations

import re

from pypdf import PdfReader


class ExtractionError(Exception):
    pass


_BIBLIO_HEADING = re.compile(
    r"^\s*(?:\d+\.?\s*)?(references|bibliography|works cited|literature cited|reference list)\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Quoted passages of 20-600 chars ("straight" or “curly” quotes).
_QUOTED = re.compile(r"[\"“][^\"“”]{20,600}[\"”]")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def extract_text(path: str) -> tuple[str, int]:
    """Return (text, page_count). Raises ExtractionError on unreadable files."""
    try:
        reader = PdfReader(path)
    except Exception as exc:  # pypdf raises many types for corrupt files
        raise ExtractionError(f"Could not open PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ExtractionError("PDF is password-protected")
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError("PDF is password-protected") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")  # one bad page should not sink the document
    text = "\n".join(pages)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    return text, len(reader.pages)


def strip_bibliography(text: str) -> str:
    """Drop the reference list when its heading appears in the back half."""
    matches = list(_BIBLIO_HEADING.finditer(text))
    if not matches:
        return text
    last = matches[-1]
    if last.start() >= len(text) * 0.5:
        return text[: last.start()]
    return text


def strip_quotes(text: str) -> str:
    """Remove quoted passages — properly quoted text is not plagiarism."""
    return _QUOTED.sub(" ", text)
