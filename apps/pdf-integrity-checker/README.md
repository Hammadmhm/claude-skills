# PDF Integrity Checker

A self-hosted website that checks uploaded PDFs for **plagiarism** and **AI-generated writing**. A submission is **rejected automatically** when either score is above **30%**; you can change that limit with `REJECT_THRESHOLD`.

- Upload hundreds of PDFs at once by drag-and-drop. The browser sends them in batches, and a pool of worker processes checks them in parallel.
- Measured on 4 CPU cores: **250 PDFs of ~21 pages each in 60 s**, and 300 PDFs of ~5 pages each in 19 s. Every file was classified correctly on the synthetic test sets.
- The dashboard has status filters, search, a CSV export and a detail view for each file. The detail view shows matching sources and the passages flagged as AI-like, with **manual override**, **re-check** and **delete** buttons.
- Runs fully offline by default, with no paid services. You can plug in an external AI detector over HTTP.

## Quick start

```bash
cd apps/pdf-integrity-checker
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. With Docker:

```bash
docker build -t pdf-integrity-checker apps/pdf-integrity-checker
docker run -p 8000:8000 -v integrity-data:/data -e APP_PASSWORD=change-me pdf-integrity-checker
```

Run **one** uvicorn process (no `--workers N`). The parallelism lives inside the app's process pool (`WORKERS`), and the plagiarism index is updated by one process at a time.

## How the decision is made

| Check | Method | Score |
|---|---|---|
| Plagiarism | Text is extracted with `pypdf`. The bibliography and quoted passages are removed, then the text is fingerprinted with MOSS-style winnowing over 5-word sequences (Schleimer et al., 2003). Fingerprints go into a SQLite index. | % of the document's fingerprints found in **reference sources** or **submissions uploaded before it** |
| AI writing | `heuristic` (default): each ~150-word passage is scored on sentence-length uniformity ("burstiness"), stock LLM phrasing and transition-word density. `http`: your own detector. | % of words in passages judged AI-like |
| Decision | `rejected` if plagiarism **> 30%** or AI **> 30%**, otherwise `accepted`. PDFs with almost no text (scanned images) go to `needs_review`, as does any file where the AI detector fails. | |

**Upload order decides who the original is.** Files are processed in parallel and can finish in any order. Each submission is still compared only against files uploaded *before* it, plus reference sources. When an earlier file or a new reference source is indexed, later overlapping submissions are re-scored automatically. The **Re-check** button compares one file against *every* other file, including later ones. Use it when two files in the same batch overlap and you want both flagged.

**Reference sources.** Choose "Reference sources" before uploading to add material that submissions should be compared against, such as past papers, textbooks or known essay-mill content. Reference sources are indexed but never accepted or rejected themselves.

## Limits of this approach — read before relying on auto-reject

- **Plagiarism coverage = what is in your index.** The checker compares files only against what you have uploaded: earlier submissions plus reference sources. Unlike Turnitin or Copyleaks, it does **not** search the web or a publisher database. Text copied from a website you never uploaded will not be caught.
- **The built-in AI detector is a heuristic, not a trained model.** It catches text written in the typical LLM register. It will miss lightly edited AI text, and it can flag formulaic human writing. All AI detectors, commercial ones included, have documented false positives, especially for non-native English writers. With auto-reject on, a false positive rejects a real person's work. Keep a human review step for rejections: the **Override** buttons and the reviewer note exist for this. For production use, connect a trained detector (below).
- **Scanned PDFs** need OCR first. They land in `needs_review`.

## Connecting an external AI detector

Set `AI_DETECTOR=http`. The app POSTs `{"<text field>": "<document text>"}` as JSON to your endpoint and reads a score from the response. This works with a self-hosted model server or with a commercial API, if that API accepts a JSON text body:

| Variable | Example | Meaning |
|---|---|---|
| `AI_DETECTOR_URL` | `https://detector.internal/score` | Endpoint to POST to |
| `AI_DETECTOR_HEADERS` | `{"Authorization": "Bearer …"}` | JSON object of extra headers |
| `AI_DETECTOR_TEXT_FIELD` | `text` | Request field that carries the text |
| `AI_DETECTOR_SCORE_FIELD` | `documents.0.ai_probability` | Dotted path to the score in the response |
| `AI_DETECTOR_SCORE_SCALE` | `1` or `100` | Score range: `1` means 0..1, `100` means 0..100 |
| `AI_DETECTOR_MAX_CHARS` | `50000` | Text is truncated to this length |

## Configuration

| Variable | Default | |
|---|---|---|
| `REJECT_THRESHOLD` | `30` | Reject when a score is strictly above this % |
| `WORKERS` | CPU count | Parallel analysis processes |
| `MAX_FILE_MB` | `50` | Per-file upload limit |
| `PER_FILE_TIMEOUT_S` | `300` | A file that takes longer becomes `error` |
| `MIN_WORDS` | `50` | Fewer extractable words → `needs_review` |
| `EXCLUDE_BIBLIOGRAPHY` / `EXCLUDE_QUOTES` | `true` | Ignore the reference list / quoted passages for plagiarism |
| `SHINGLE_K` / `WINNOW_W` / `MIN_SOURCE_MATCHES` | `5` / `4` / `3` | Fingerprint tuning |
| `APP_USERNAME` / `APP_PASSWORD` | `admin` / *(unset)* | Set a password to enable HTTP Basic auth on the whole site |
| `DATA_DIR` | `./data` | Uploaded PDFs + SQLite database |

Put the app behind HTTPS (for example nginx or Caddy) whenever `APP_PASSWORD` is set, because Basic auth sends the credentials with every request. Raise the proxy's upload limit to match: nginx `client_max_body_size 600m;` covers a batch of 10 × 50 MB files.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/upload` | multipart: `files` (repeatable), `kind` = `submission` \| `reference` |
| `GET` | `/api/documents?status=&kind=&q=&limit=&offset=` | Paged list |
| `GET` | `/api/documents/{id}` | Full result incl. matching sources and flagged passages |
| `GET` | `/api/documents/{id}/file` | Original PDF |
| `POST` | `/api/documents/{id}/override` | `{"status": "accepted"\|"rejected", "note": "…"}` |
| `POST` | `/api/documents/{id}/recheck` | Re-analyse against all other documents |
| `DELETE` | `/api/documents/{id}` | Remove the file and its fingerprints |
| `GET` | `/api/stats`, `/api/export.csv`, `/healthz` | |

## Tests and load test

```bash
python -m unittest discover -s tests          # 12 tests: engine + end-to-end API
python tools/make_sample_pdfs.py --count 300 --out /tmp/pdfs
python tools/load_test.py --dir /tmp/pdfs --url http://localhost:8000
```
