"""Internet-search tests against a local mock search engine + mock web pages."""

from __future__ import annotations

import dataclasses
import json
import os
import random
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="integrity-web-test-"))
os.environ.setdefault("WORKERS", "2")
os.environ.pop("APP_PASSWORD", None)

from make_sample_pdfs import build_pdf, human_paragraph, llm_paragraph  # noqa: E402

from app import web_search  # noqa: E402

ARTICLE = [human_paragraph(random.Random(500 + i)) for i in range(30)]
OTHER = [human_paragraph(random.Random(900 + i)) for i in range(10)]


def human_text(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    return [human_paragraph(rng) for _ in range(n)]


class MockWeb(BaseHTTPRequestHandler):
    calls: list[str] = []

    def log_message(self, *args):  # silence
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _results(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        return [(f"{base}/article.html", "The Article"), (f"{base}/other.html", "Other"), (f"{base}/missing", "Gone")]

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        MockWeb.calls.append(path)
        if path == "/searxng/search":
            self._send(200, {"results": [{"url": u, "title": t, "content": "…"} for u, t in self._results()]})
        elif path == "/brave":
            if self.headers.get("X-Subscription-Token") != "k":
                return self._send(401, {"error": "bad key"})
            self._send(200, {"web": {"results": [{"url": u, "title": t, "description": "…"} for u, t in self._results()]}})
        elif path == "/denied/search":
            self._send(403, {"error": "quota"})
        elif path == "/article.html":
            body = "".join(f"<p>{p}</p>" for p in ARTICLE)
            self._send(200, f"<html><head><title>The Article</title><script>var x=1;</script></head><body>{body}</body></html>",
                       "text/html; charset=utf-8")
        elif path == "/other.html":
            self._send(200, "<html><body>" + "".join(f"<p>{p}</p>" for p in OTHER) + "</body></html>", "text/html")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        MockWeb.calls.append(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/serper" and self.headers.get("X-API-KEY") == "k" and body.get("q"):
            return self._send(200, {"organic": [{"link": u, "title": t, "snippet": "…"} for u, t in self._results()]})
        self._send(401, {"error": "bad key"})


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockWeb)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

        from fastapi.testclient import TestClient

        from app.main import app, settings

        cls.app, cls.base_settings = app, settings
        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()
        cls.pipeline = app.state.pipeline
        cls.pipeline.limiter = web_search.RateLimiter(0)

    @classmethod
    def tearDownClass(cls):
        cls.pipeline.settings = cls.base_settings
        cls.client_cm.__exit__(None, None, None)
        cls.server.shutdown()

    def use(self, **kw):
        s = dataclasses.replace(self.base_settings, web_allow_private_hosts=True, **kw)
        self.pipeline.settings = s
        return s

    def upload(self, name: str, paras: list[str]) -> int:
        r = self.client.post("/api/upload", data={"kind": "submission"},
                             files=[("files", (name, build_pdf(paras), "application/pdf"))])
        return r.json()["results"][0]["id"]

    def wait(self, doc_id: int, until=lambda d: d["status"] not in ("queued", "processing"), timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            d = self.client.get(f"/api/documents/{doc_id}").json()
            if until(d):
                return d
            time.sleep(0.2)
        self.fail(f"document {doc_id} did not reach the expected state: {d}")

    # ---- units ---------------------------------------------------------
    def test_select_queries(self):
        text = " ".join(human_text(1, 30))
        qs = web_search.select_queries(text, 8)
        self.assertEqual(len(qs), 8)
        for q in qs:
            self.assertTrue(q.startswith('"') and q.endswith('"'))
            self.assertEqual(len(q.strip('"').split()), 10)
            self.assertIn(q.strip('"'), " ".join(web_search._WORD.findall(text)))
        self.assertEqual(web_search.select_queries(text, 0), [])

    def test_html_to_text(self):
        title, text = web_search.html_to_text(
            "<html><head><title>T</title><style>p{}</style></head><body><p>Hello</p><script>evil()</script><p>World</p></body></html>")
        self.assertEqual(title, "T")
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("evil", text)

    def test_private_hosts_are_refused(self):
        self.assertFalse(web_search.is_public_host("127.0.0.1"))
        self.assertFalse(web_search.is_public_host("10.1.2.3"))
        self.assertFalse(web_search.is_public_host("169.254.169.254"))  # cloud metadata
        s = dataclasses.replace(self.base_settings, web_allow_private_hosts=False)
        with self.assertRaises(web_search.FetchError):
            web_search.fetch_page(f"{self.base}/article.html", s)
        with self.assertRaises(web_search.FetchError):
            web_search.fetch_page("file:///etc/passwd", s)
        guard = web_search._GuardedRedirects(allow_private=False)
        with self.assertRaises(web_search.FetchError):
            guard.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data")

    def test_provider_formats(self):
        common = dict(web_allow_private_hosts=True, web_results_per_query=10)
        for provider, extra in {
            "brave": {"brave_api_url": f"{self.base}/brave", "web_search_api_key": "k"},
            "serper": {"serper_api_url": f"{self.base}/serper", "web_search_api_key": "k"},
            "searxng": {"searxng_url": f"{self.base}/searxng"},
        }.items():
            s = dataclasses.replace(self.base_settings, web_search=provider, **common, **extra)
            rows = web_search.search('"some exact phrase"', s)
            self.assertEqual(rows[0]["url"], f"{self.base}/article.html", provider)
            self.assertEqual(rows[0]["title"], "The Article", provider)

        bad = dataclasses.replace(self.base_settings, web_search="brave", brave_api_url=f"{self.base}/brave",
                                  web_search_api_key="wrong")
        with self.assertRaisesRegex(web_search.SearchError, "HTTP 401.*API key"):
            web_search.search('"x"', bad)
        with self.assertRaisesRegex(web_search.SearchError, "needs WEB_SEARCH_API_KEY"):
            web_search.search('"x"', dataclasses.replace(bad, web_search_api_key=""))

    def test_fetch_page(self):
        s = self.use()
        title, text = web_search.fetch_page(f"{self.base}/article.html", s)
        self.assertEqual(title, "The Article")
        self.assertIn(ARTICLE[0][:30], text)
        with self.assertRaises(web_search.FetchError):
            web_search.fetch_page(f"{self.base}/missing", s)

    # ---- end to end ----------------------------------------------------
    def test_copied_from_web_is_rejected_and_earlier_uploads_rescored(self):
        # An upload made while internet search was off copies the article and passes...
        self.use(web_search="off")
        early = self.upload("early.pdf", ARTICLE[5:20] + human_text(61, 15))
        self.assertEqual(self.wait(early)["status"], "accepted")

        # ...then search is switched on and a new upload copies the same page.
        self.use(web_search="searxng", searxng_url=f"{self.base}/searxng")
        doc_id = self.upload("from-web.pdf", ARTICLE[:18] + human_text(60, 12))
        doc = self.wait(doc_id)
        self.assertEqual(doc["status"], "rejected", doc["reasons"])
        self.assertGreater(doc["plagiarism_pct"], 40)
        top = doc["details"]["plagiarism"]["sources"][0]
        self.assertEqual((top["kind"], top["url"]), ("web", f"{self.base}/article.html"))
        web = doc["details"]["web"]
        self.assertEqual(web["queries_failed"], 0)
        statuses = {p["url"].rsplit("/", 1)[-1]: p["status"] for p in web["pages"]}
        self.assertEqual(statuses["missing"], "could not be downloaded")
        self.assertIn(statuses["article.html"], ("downloaded", "already indexed"))

        # The earlier upload is re-scored against the newly indexed page.
        self.wait(early, until=lambda d: d["status"] == "rejected")

        self.assertGreaterEqual(self.client.get("/api/stats").json()["web_pages"], 1)
        web_docs = self.client.get("/api/documents", params={"kind": "web"}).json()["items"]
        self.assertTrue(any(d["url"] == f"{self.base}/article.html" for d in web_docs))
        self.assertTrue(all(d["kind"] != "web" for d in self.client.get("/api/documents").json()["items"]))

        # Re-check uses the cached query results: no new search calls.
        before = sum(1 for c in MockWeb.calls if c == "/searxng/search")
        self.client.post(f"/api/documents/{doc_id}/recheck")
        self.assertEqual(self.wait(doc_id)["status"], "rejected")
        self.assertEqual(sum(1 for c in MockWeb.calls if c == "/searxng/search"), before)

    def test_search_failure_needs_review_unless_already_rejected(self):
        self.use(web_search="searxng", searxng_url=f"{self.base}/denied")
        clean = self.wait(self.upload("clean.pdf", human_text(70, 30)))
        self.assertEqual(clean["status"], "needs_review")
        self.assertIn("Internet search incomplete", clean["reasons"][0])

        rng = random.Random(71)
        ai = self.wait(self.upload("ai.pdf", [llm_paragraph(rng) for _ in range(25)]))
        self.assertEqual(ai["status"], "rejected")
        self.assertTrue(any("Internet search incomplete" in r for r in ai["reasons"]))


if __name__ == "__main__":
    unittest.main()
