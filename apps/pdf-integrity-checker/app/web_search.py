"""Internet plagiarism search.

For each submission:

1. ``select_queries`` picks a handful of distinctive ~10-word phrases spread
   evenly through the document.
2. Each phrase is sent as an exact-phrase ("quoted") query to a web search
   provider (Brave, Serper.dev or a self-hosted SearXNG).
3. URLs that come back for one or more phrases are ranked by hit count; the
   top pages are downloaded, their text extracted (HTML, PDF or plain text)
   and fingerprinted like any other document.
4. The pages are stored in the index as ``web`` sources, so the normal
   plagiarism matcher measures how much of the submission they contain — and
   every later submission is compared against them for free.

Query results are cached in SQLite per (provider, query), and each URL is
downloaded at most once, which keeps API costs down on re-checks and when
several submissions copy from the same page.
"""

from __future__ import annotations

import html.parser
import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pypdf import PdfReader

from .ai_detect import split_sentences

log = logging.getLogger("integrity.web")

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
USER_AGENT = "Mozilla/5.0 (compatible; PDFIntegrityChecker/1.0; plagiarism check)"


class SearchError(Exception):
    pass


class FetchError(Exception):
    pass


# ---- query selection --------------------------------------------------------
def select_queries(text: str, n: int, phrase_words: int = 10) -> list[str]:
    """Pick up to `n` exact-match phrases spread evenly through `text`."""
    if n <= 0:
        return []
    candidates = []
    for sent in split_sentences(text):
        words = _WORD.findall(sent)
        if len(words) < phrase_words + 2:
            continue
        alpha = sum(1 for w in words if w.isalpha())
        if alpha / len(words) < 0.8:  # skip tables, citations, numeric lines
            continue
        start = 2 if len(words) >= phrase_words + 4 else 1  # skip "However, the ..." openers
        candidates.append(" ".join(words[start : start + phrase_words]))
    if not candidates:
        return []
    if len(candidates) <= n:
        picked = candidates
    else:
        step = len(candidates) / n
        picked = [candidates[int(i * step + step / 2)] for i in range(n)]
    seen, out = set(), []
    for p in picked:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(f'"{p}"')
    return out


# ---- search providers -------------------------------------------------------
class RateLimiter:
    """Global queries-per-second limiter shared by all worker threads."""

    def __init__(self, qps: float):
        self.interval = 1.0 / qps if qps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if at > now:
            time.sleep(at - now)


def _http_json(req: urllib.request.Request, timeout: float) -> dict:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-configured endpoint
        return json.loads(resp.read().decode("utf-8"))


def _search_once(query: str, s) -> list[dict]:
    n = s.web_results_per_query
    if s.web_search == "brave":
        if not s.web_search_api_key:
            raise SearchError("WEB_SEARCH=brave needs WEB_SEARCH_API_KEY")
        url = f"{s.brave_api_url}?{urllib.parse.urlencode({'q': query, 'count': min(n, 20)})}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json", "X-Subscription-Token": s.web_search_api_key})
        data = _http_json(req, 30)
        rows = (data.get("web") or {}).get("results") or []
        return [{"url": r.get("url"), "title": r.get("title", ""), "snippet": r.get("description", "")} for r in rows]

    if s.web_search == "serper":
        if not s.web_search_api_key:
            raise SearchError("WEB_SEARCH=serper needs WEB_SEARCH_API_KEY")
        req = urllib.request.Request(
            s.serper_api_url, data=json.dumps({"q": query, "num": n}).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "X-API-KEY": s.web_search_api_key})
        data = _http_json(req, 30)
        rows = data.get("organic") or []
        return [{"url": r.get("link"), "title": r.get("title", ""), "snippet": r.get("snippet", "")} for r in rows]

    if s.web_search == "searxng":
        if not s.searxng_url:
            raise SearchError("WEB_SEARCH=searxng needs SEARXNG_URL")
        url = f"{s.searxng_url.rstrip('/')}/search?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        data = _http_json(req, 30)
        rows = data.get("results") or []
        return [{"url": r.get("url"), "title": r.get("title", ""), "snippet": r.get("content", "")} for r in rows[:n]]

    raise SearchError(f"Unknown WEB_SEARCH provider: {s.web_search!r}")


def search(query: str, s, limiter: RateLimiter | None = None) -> list[dict]:
    """Run one query with rate limiting and retry on 429 / 5xx."""
    delay = 2.0
    for attempt in range(4):
        if limiter:
            limiter.wait()
        try:
            rows = _search_once(query, s)
            return [r for r in rows if r.get("url", "").startswith(("http://", "https://"))]
        except SearchError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            hint = " (check the API key / quota)" if exc.code in (401, 402, 403, 429) else ""
            raise SearchError(f"{s.web_search} search failed: HTTP {exc.code}{hint}") from exc
        except Exception as exc:
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
                continue
            raise SearchError(f"{s.web_search} search failed: {exc}") from exc
    raise SearchError("search failed")


# ---- page fetching ----------------------------------------------------------
def is_public_host(host: str) -> bool:
    """True if every address `host` resolves to is a public internet address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def _check_url(url: str, allow_private: bool) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError(f"unsupported URL: {url}")
    if not allow_private and not is_public_host(parts.hostname):
        raise FetchError(f"refusing to fetch non-public host {parts.hostname}")


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target so a public page can't bounce us
    onto an internal address (SSRF)."""

    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HTMLText(html.parser.HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> tuple[str, str]:
    p = _HTMLText()
    p.feed(raw)
    p.close()
    return p.title.strip(), re.sub(r"[ \t]+", " ", "".join(p.parts))


def fetch_page(url: str, s) -> tuple[str, str]:
    """Download `url` and return (title, text)."""
    _check_url(url, s.web_allow_private_hosts)
    opener = urllib.request.build_opener(_GuardedRedirects(s.web_allow_private_hosts))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5"})
    limit = s.web_fetch_max_mb * 1024 * 1024
    try:
        with opener.open(req, timeout=s.web_fetch_timeout_s) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read(limit + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"{url}: {exc}") from exc
    if len(body) > limit:
        body = body[:limit]  # partial text is still useful for matching

    if "pdf" in ctype or body[:5] == b"%PDF-":
        try:
            reader = PdfReader(io.BytesIO(body))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:200])
        except Exception as exc:
            raise FetchError(f"{url}: unreadable PDF ({exc})") from exc
        return "", text
    decoded = body.decode(charset, errors="replace")
    if "html" in ctype or decoded.lstrip()[:1] == "<":
        return html_to_text(decoded)
    if ctype.startswith("text/") or not ctype:
        return "", decoded
    raise FetchError(f"{url}: unsupported content type {ctype}")
