"""Tests: run with `python -m unittest discover -s tests` from the app folder."""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

_TMP = tempfile.mkdtemp(prefix="integrity-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["WORKERS"] = "2"
os.environ.pop("APP_PASSWORD", None)

from make_sample_pdfs import build_pdf, human_paragraph, llm_paragraph  # noqa: E402

from app import ai_detect  # noqa: E402
from app.extract import strip_bibliography, strip_quotes  # noqa: E402
from app.plagiarism import containment, fingerprints, tokenize  # noqa: E402
from app.worker import decide  # noqa: E402


def human_text(seed: int, paras: int = 20) -> list[str]:
    rng = random.Random(seed)
    return [human_paragraph(rng) for _ in range(paras)]


class EngineTests(unittest.TestCase):
    def test_identical_text_is_full_match(self):
        fp = fingerprints(tokenize(" ".join(human_text(1))))
        self.assertEqual(containment(fp, fp), 1.0)

    def test_unrelated_text_is_near_zero(self):
        a = fingerprints(tokenize(" ".join(human_text(1))))
        b = fingerprints(tokenize(" ".join(human_text(2))))
        self.assertLess(containment(a, b), 0.02)

    def test_partial_copy_is_proportional(self):
        src, other = human_text(1), human_text(3)
        mixed = src[:10] + other[:10]
        c = containment(fingerprints(tokenize(" ".join(mixed))), fingerprints(tokenize(" ".join(src))))
        self.assertTrue(0.35 < c < 0.65, c)

    def test_case_and_punctuation_do_not_hide_copying(self):
        text = " ".join(human_text(4))
        disguised = text.upper().replace(".", ";")
        self.assertGreater(containment(fingerprints(tokenize(disguised)), fingerprints(tokenize(text))), 0.95)

    def test_bibliography_is_stripped(self):
        text = "Body text here.\n" * 50 + "References\n[1] Someone, 2020."
        self.assertNotIn("Someone", strip_bibliography(text))
        early = "References\nintro mention\n" + "Body text here.\n" * 50
        self.assertEqual(strip_bibliography(early), early)  # heading in first half is kept

    def test_quotes_are_stripped(self):
        self.assertNotIn("famous", strip_quotes('He said "this is a famous long quotation indeed" today.'))

    def test_ai_heuristic_separates_registers(self):
        rng = random.Random(5)
        llm = " ".join(llm_paragraph(rng) for _ in range(20))
        human = " ".join(human_text(6))
        self.assertGreater(ai_detect.detect_heuristic(llm)["ai_pct"], 80)
        self.assertLess(ai_detect.detect_heuristic(human)["ai_pct"], 20)

    def test_threshold_is_strictly_greater_than(self):
        self.assertEqual(decide(30.0, 30.0, 30)[0], "accepted")
        self.assertEqual(decide(30.1, 0, 30)[0], "rejected")
        self.assertEqual(decide(0, 30.1, 30)[0], "rejected")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import app

        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)

    def upload(self, files: dict[str, bytes], kind: str = "submission") -> list[dict]:
        r = self.client.post("/api/upload", data={"kind": kind},
                             files=[("files", (name, data, "application/pdf")) for name, data in files.items()])
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["results"]

    def wait(self, doc_id: int, timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            d = self.client.get(f"/api/documents/{doc_id}").json()
            if d["status"] not in ("queued", "processing"):
                return d
            time.sleep(0.2)
        self.fail(f"document {doc_id} did not finish")

    def test_end_to_end(self):
        original = human_text(10, 30)
        (a,) = self.upload({"original.pdf": build_pdf(original)})
        doc_a = self.wait(a["id"])
        self.assertEqual(doc_a["status"], "accepted", doc_a)
        self.assertLess(doc_a["plagiarism_pct"], 5)

        copied = original[:20] + human_text(11, 10)
        rng = random.Random(12)
        res = self.upload({
            "copied.pdf": build_pdf(copied),
            "llm.pdf": build_pdf([llm_paragraph(rng) for _ in range(25)]),
            "scanned.pdf": build_pdf([""]),
            "notes.txt": b"hello, not a pdf",
        })
        by_name = {r["filename"]: r for r in res}
        self.assertFalse(by_name["notes.txt"]["ok"])

        doc_b = self.wait(by_name["copied.pdf"]["id"])
        self.assertEqual(doc_b["status"], "rejected")
        self.assertGreater(doc_b["plagiarism_pct"], 50)
        self.assertEqual(doc_b["details"]["plagiarism"]["sources"][0]["doc_id"], a["id"])

        doc_llm = self.wait(by_name["llm.pdf"]["id"])
        self.assertEqual(doc_llm["status"], "rejected")
        self.assertGreater(doc_llm["ai_pct"], 30)

        self.assertEqual(self.wait(by_name["scanned.pdf"]["id"])["status"], "needs_review")

        # Re-check compares against later uploads too: A now matches B.
        self.client.post(f"/api/documents/{a['id']}/recheck")
        self.assertEqual(self.wait(a["id"])["status"], "rejected")

        # Manual override.
        r = self.client.post(f"/api/documents/{a['id']}/override", json={"status": "accepted", "note": "original author"})
        self.assertEqual(r.json()["decision_source"], "manual")

        stats = self.client.get("/api/stats").json()
        self.assertGreaterEqual(stats["total"], 4)
        self.assertIn("copied.pdf", self.client.get("/api/export.csv").text)

        self.assertEqual(self.client.delete(f"/api/documents/{a['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/documents/{a['id']}").status_code, 404)

    def test_reference_source_is_matched(self):
        ref = human_text(20, 30)
        (r,) = self.upload({"textbook.pdf": build_pdf(ref)}, kind="reference")
        self.assertEqual(self.wait(r["id"])["status"], "indexed")
        (s,) = self.upload({"essay.pdf": build_pdf(ref[:15] + human_text(21, 15))})
        doc = self.wait(s["id"])
        self.assertEqual(doc["status"], "rejected")
        self.assertEqual(doc["details"]["plagiarism"]["sources"][0]["kind"], "reference")

    def test_reference_added_later_rescores_submission(self):
        ref = human_text(30, 30)
        (s,) = self.upload({"essay2.pdf": build_pdf(ref[:18] + human_text(31, 12))})
        self.assertEqual(self.wait(s["id"])["status"], "accepted")
        (r,) = self.upload({"source-book.pdf": build_pdf(ref)}, kind="reference")
        self.wait(r["id"])
        end = time.time() + 30
        while time.time() < end and self.client.get(f"/api/documents/{s['id']}").json()["status"] != "rejected":
            time.sleep(0.2)
        self.assertEqual(self.client.get(f"/api/documents/{s['id']}").json()["status"], "rejected")

    def test_upload_order_wins_over_processing_order(self):
        """The earlier upload is the original even if the copy finishes first."""
        from app.main import app, settings

        db, pipeline = app.state.db, app.state.pipeline
        paras = human_text(40, 30)
        paths = []
        for name in ("first.pdf", "second.pdf"):
            p = settings.upload_dir / f"order-{name}"
            p.write_bytes(build_pdf(paras))
            paths.append(p)
        first = db.insert_document("first.pdf", str(paths[0]), "a" * 64, 1, "submission")
        second = db.insert_document("second.pdf", str(paths[1]), "b" * 64, 1, "submission")
        pipeline.submit(second)  # the later upload is processed first...
        self.assertEqual(self.wait(second)["status"], "accepted")  # ...with nothing earlier indexed yet
        pipeline.submit(first)
        self.assertEqual(self.wait(first)["status"], "accepted")
        end = time.time() + 30
        while time.time() < end and self.client.get(f"/api/documents/{second}").json()["status"] != "rejected":
            time.sleep(0.2)
        self.assertEqual(self.client.get(f"/api/documents/{second}").json()["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
