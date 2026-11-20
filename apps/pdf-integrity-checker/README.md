# PDF Integrity Checker

A self-hosted website that checks uploaded PDFs for **plagiarism** (against the internet and your own uploads) and **AI-generated writing**. A submission is **rejected automatically** when either score is above **30%**; you can change that limit with `REJECT_THRESHOLD`.

- Upload hundreds of PDFs at once by drag-and-drop. The browser sends them in batches, and a pool of worker processes checks them in parallel.
- Measured on 4 CPU cores: **250 PDFs of ~21 pages each in 60 s**, and 300 PDFs of ~5 pages each in 19 s. Every file was classified correctly on the synthetic test sets.
- The dashboard has status filters, search, a CSV export and a detail view for each file. The detail view shows matching sources and the passages flagged as AI-like, with **manual override**, **re-check** and **delete** buttons.
- **Internet search:** each PDF's distinctive phrases are searched on the web, and the pages that come up are downloaded and compared against the PDF. Supported backends are Brave Search API, Serper.dev (Google results) and a self-hosted SearXNG.
- Without a search backend it runs fully offline. You can also plug in an external AI detector over HTTP.

## Quick start

```bash
cd apps/pdf-integrity-checker
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. With Docker:

```bash
docker build -t pdf-integrity-checker apps/pdf-integrity-checker
docker run -p 8000:8000 -v integrity-data:/data -e APP_PASSWORD=change-me \
  -e WEB_SEARCH=brave -e WEB_SEARCH_API_KEY=your-key -e WEB_SEARCH_QPS=20 pdf-integrity-checker
```

Run **one** uvicorn process (no `--workers N`). The parallelism lives inside the app's process pool (`WORKERS`), and the plagiarism index is updated by one process at a time.

## How the decision is made

| Check | Method | Score |
|---|---|---|
| Plagiarism | Text is extracted with `pypdf`. The bibliography and quoted passages are removed, then the text is fingerprinted with MOSS-style winnowing over 5-word sequences (Schleimer et al., 2003). Fingerprints go into a SQLite index. | % of the document's fingerprints found in **web pages**, **reference sources** or **submissions uploaded before it** |
| AI writing | `heuristic` (default): each ~150-word passage is scored on sentence-length uniformity ("burstiness"), stock LLM phrasing and transition-word density. `http`: your own detector. | % of words in passages judged AI-like |
| Decision | `rejected` if plagiarism **> 30%** or AI **> 30%**, otherwise `accepted`. PDFs with almost no text (scanned images) go to `needs_review`, as does any file where the AI detector fails. | |

**Upload order decides who the original is.** Files are processed in parallel and can finish in any order. Each submission is still compared only against files uploaded *before* it, plus reference sources. When an earlier file or a new reference source is indexed, later overlapping submissions are re-scored automatically. The **Re-check** button compares one file against *every* other file, including later ones. Use it when two files in the same batch overlap and you want both flagged.

## Internet search

Turn it on with `WEB_SEARCH`. Each submission then goes through these steps:

1. **Pick phrases.** Up to `WEB_QUERIES_PER_DOC` (default 8) distinctive 10-word phrases are chosen, spread evenly through the document. Bibliography and quoted text are skipped.
2. **Search.** Each phrase is searched as an exact match ("in quotes").
3. **Download.** The pages that come up most often (up to `WEB_MAX_PAGES_PER_DOC`, default 5) are downloaded. HTML, PDF and plain-text pages are read.
4. **Score.** The pages are fingerprinted and stored as **web sources**, and the submission's plagiarism % includes everything it shares with them. The detail view links straight to each matching page.

Every downloaded page stays in the index. Later submissions are compared against it with no extra searching, and earlier submissions that overlap it are re-scored automatically. Search results are cached per phrase, so **Re-check** does not use extra searches.

| Backend | Setup | Notes |
|---|---|---|
| `brave` | `WEB_SEARCH=brave`, `WEB_SEARCH_API_KEY=<key>` from api-dashboard.search.brave.com | Independent web index. Metered at about $5 per 1,000 searches (late 2026). |
| `serper` | `WEB_SEARCH=serper`, `WEB_SEARCH_API_KEY=<key>` from serper.dev | Google results. Check serper.dev for current pricing and free quota. |
| `searxng` | `WEB_SEARCH=searxng`, `SEARXNG_URL=http://your-searxng:8080` | Free and self-hosted. Enable the `json` output format in its `settings.yml`. It scrapes upstream engines, which may rate-limit or CAPTCHA it at volume. Use a paid API for reliable bulk checks. |

Google's own Custom Search JSON API is not offered: it closed to new customers in January 2026 and shuts down on 1 January 2027.

**Cost and speed.** Expect about 7–8 searches per PDF, so **about 2,000 searches for 250 PDFs (about $10 on Brave)**. Searches go through a global rate limiter (`WEB_SEARCH_QPS`). Set it to your plan's limit:

| `WEB_SEARCH_QPS` | 250 PDFs (measured with a mock engine at 200 ms per call) |
|---|---|
| 20 | about 1.5 minutes |
| 1 (default, safe for any plan) | about 30 minutes |

Searching runs in its own thread pool, so PDF extraction never waits on it. The dashboard shows "searching the internet…" while a file is in this stage.

**When search fails** (bad key, quota used up, provider down): a file that would have been **accepted** goes to **needs_review** instead of passing unchecked. A file that is already over the limit is still **rejected**. This applies when more than `WEB_MAX_FAILED_SHARE` (default 50%) of its searches fail. Fix the key or top up the quota, then use **Re-check**.

**Safety.** Pages are fetched with a 10 MB / 15 s cap. Only public internet addresses are allowed, and that is re-checked on every redirect. Private, loopback and cloud-metadata IPs are refused, so a malicious search result cannot make the server read internal URLs.

**Reference sources.** Choose "Reference sources" before uploading to add material that submissions should be compared against, such as past papers, textbooks or known essay-mill content. Reference sources are indexed but never accepted or rejected themselves.

## Limits of this approach — read before relying on auto-reject

- **Internet coverage is sampled, not exhaustive.** Each PDF gets about 8 phrase searches, not one search per sentence. A paper that copies one short passage may slip through if no sampled phrase falls inside it, while large-scale copying is very likely to be hit. Raise `WEB_QUERIES_PER_DOC` for more coverage at more cost. Paywalled journals, private course sites and essay-mill databases are not reachable by any web search; Turnitin covers those through publisher and student-paper agreements. Add such material as **reference sources**.
- **Paraphrasing** (rewording sentence by sentence) defeats exact-phrase search and fingerprinting alike.
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
| `WEB_SEARCH` | `off` | `brave`, `serper`, `searxng` or `off` |
| `WEB_SEARCH_API_KEY` / `SEARXNG_URL` | | Backend credentials / address |
| `WEB_QUERIES_PER_DOC` | `8` | Phrases searched per PDF |
| `WEB_MAX_PAGES_PER_DOC` | `5` | Pages downloaded per PDF |
| `WEB_RESULTS_PER_QUERY` | `10` | Results read per search |
| `WEB_SEARCH_QPS` | `1` | Global searches-per-second cap |
| `WEB_CONCURRENCY` | `8` | Files in the internet-search stage at once |
| `WEB_FETCH_TIMEOUT_S` / `WEB_FETCH_MAX_MB` | `15` / `10` | Page download limits |
| `WEB_MAX_FAILED_SHARE` | `0.5` | Above this share of failed searches, a would-be acceptance becomes `needs_review` |
| `APP_USERNAME` / `APP_PASSWORD` | `admin` / *(unset)* | Set a password to enable HTTP Basic auth on the whole site |
| `DATA_DIR` | `./data` | Uploaded PDFs + SQLite database |

Put the app behind HTTPS (for example nginx or Caddy) whenever `APP_PASSWORD` is set, because Basic auth sends the credentials with every request. Raise the proxy's upload limit to match: nginx `client_max_body_size 600m;` covers a batch of 10 × 50 MB files.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/upload` | multipart: `files` (repeatable), `kind` = `submission` \| `reference` |
| `GET` | `/api/documents?status=&kind=&q=&limit=&offset=` | Paged list (`kind=web` lists indexed web pages) |
| `GET` | `/api/documents/{id}` | Full result incl. matching sources and flagged passages |
| `GET` | `/api/documents/{id}/file` | Original PDF |
| `POST` | `/api/documents/{id}/override` | `{"status": "accepted"\|"rejected", "note": "…"}` |
| `POST` | `/api/documents/{id}/recheck` | Re-analyse against all other documents |
| `DELETE` | `/api/documents/{id}` | Remove the file and its fingerprints |
| `GET` | `/api/stats`, `/api/export.csv`, `/healthz` | |

## Tests and load test

```bash
python -m unittest discover -s tests          # 19 tests: engine, API, internet search (local mock engine)
python tools/make_sample_pdfs.py --count 300 --out /tmp/pdfs
python tools/load_test.py --dir /tmp/pdfs --url http://localhost:8000
```
