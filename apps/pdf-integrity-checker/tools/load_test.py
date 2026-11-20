#!/usr/bin/env python3
"""Upload a folder of PDFs to a running checker and time the whole run.

    python tools/load_test.py --dir /tmp/pdfs --url http://localhost:8000

Uses only the standard library. Uploads in batches (like the web UI), then
polls /api/stats until nothing is queued or processing.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _req(url: str, data: bytes | None = None, headers: dict | None = None, auth: str | None = None):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=300) as r:  # noqa: S310
        return json.loads(r.read())


def upload_batch(base: str, files: list[Path], kind: str, auth: str | None) -> list[dict]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\n{kind}\r\n'.encode()]
    for f in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{f.name}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n".encode() + f.read_bytes() + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    return _req(f"{base}/api/upload", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, auth)["results"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="folder of PDFs")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--auth", help="user:password if APP_PASSWORD is set")
    args = ap.parse_args()

    files = sorted(Path(args.dir).glob("*.pdf"))
    batches = [files[i : i + args.batch] for i in range(0, len(files), args.batch)]
    t0 = time.time()
    with ThreadPoolExecutor(args.concurrency) as pool:
        results = [r for rs in pool.map(lambda b: upload_batch(args.url, b, "submission", args.auth), batches) for r in rs]
    t_up = time.time() - t0
    failed = [r for r in results if not r["ok"]]
    print(f"uploaded {len(results) - len(failed)}/{len(files)} files in {t_up:.1f}s ({len(failed)} rejected at upload)")

    while True:
        s = _req(f"{args.url}/api/stats", auth=args.auth)
        b = s["by_status"]
        pending = b.get("queued", 0) + b.get("processing", 0)
        if not pending:
            break
        print(f"  {time.time() - t0:6.1f}s  pending={pending}  {b}")
        time.sleep(2)
    total = time.time() - t0
    print(f"all checks finished in {total:.1f}s ({len(files) / total:.1f} PDFs/s) -> {s['by_status']}")


if __name__ == "__main__":
    main()
