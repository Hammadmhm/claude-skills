#!/usr/bin/env python3
"""Generate synthetic text PDFs for testing and load tests (stdlib only).

Produces a mix of original papers, papers that copy a large share of an
earlier paper, and papers written in a stock "LLM" register, so the checker
has something to accept and something to reject.

    python tools/make_sample_pdfs.py --count 300 --out /tmp/pdfs
"""

from __future__ import annotations

import argparse
import random
import textwrap
from pathlib import Path

VOCAB = (
    "river stone market harbor winter ledger copper orchard signal lantern furnace meadow "
    "quarry archive tunnel engine valley ferry granite pigeon mill canal thatch beacon "
    "compass saddle barley kettle loom anvil chimney cellar wagon spindle trellis furrow "
    "bellows cistern parish tannery foundry wharf granary smithy almanac ledger brook"
).split()
VERBS = "measured carried shifted doubled collapsed recovered traced argued noticed recorded rejected built".split()
LLM_SENTENCES = [
    "It is important to note that this topic plays a crucial role in today's fast-paced world.",
    "Furthermore, the multifaceted landscape of this field underscores the pivotal importance of a holistic approach.",
    "Moreover, researchers continue to delve into the intricate tapestry of factors that foster meaningful outcomes.",
    "Additionally, this comprehensive analysis serves as a testament to the robust and nuanced nature of the subject.",
    "In conclusion, navigating the complexities of this realm requires a seamless and meticulous strategy.",
    "Overall, leveraging these invaluable insights can unlock profound benefits for stakeholders across the board.",
    "Notably, the ever-evolving nature of this domain showcases the paramount need for continued exploration.",
]


def human_paragraph(rng: random.Random) -> str:
    out = []
    for _ in range(rng.randint(5, 9)):
        n = rng.choice([4, 6, 9, 14, 22, 31, 8, 17])
        words = [rng.choice(VOCAB) for _ in range(n)]
        words.insert(rng.randint(1, len(words) - 1), rng.choice(VERBS))
        out.append(" ".join(words).capitalize() + rng.choice([".", ".", ".", "?", "!"]))
    return " ".join(out)


def llm_paragraph(rng: random.Random) -> str:
    return " ".join(rng.sample(LLM_SENTENCES, k=5))


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(paragraphs: list[str]) -> bytes:
    """Write a minimal multi-page PDF with Helvetica text."""
    lines: list[str] = []
    for p in paragraphs:
        lines.extend(textwrap.wrap(p, 95) + [""])
    pages = [lines[i : i + 50] for i in range(0, len(lines), 50)] or [[""]]

    objs: list[bytes] = []
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, page_lines in enumerate(pages):
        content = "BT /F1 10 Tf 13 TL 50 760 Td " + " ".join(f"({_escape(ln)}) Tj T*" for ln in page_lines) + " ET"
        stream = content.encode("latin-1", "replace")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_ids[i] + 1} 0 R >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=300, help="number of PDFs (default 300)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--paragraphs", type=int, default=30, help="paragraphs per paper (default 30, ~4-6 pages)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    originals: list[list[str]] = []
    kinds = {"original": 0, "copied": 0, "llm": 0}
    for i in range(args.count):
        roll = rng.random()
        if originals and roll < 0.15:
            src = rng.choice(originals)
            n_copy = int(len(src) * rng.uniform(0.4, 0.8))
            paras = src[:n_copy] + [human_paragraph(rng) for _ in range(args.paragraphs - n_copy)]
            kind = "copied"
        elif roll < 0.30:
            paras = [llm_paragraph(rng) for _ in range(args.paragraphs)]
            kind = "llm"
        else:
            paras = [human_paragraph(rng) for _ in range(args.paragraphs)]
            originals.append(paras)
            kind = "original"
        kinds[kind] += 1
        (out / f"paper_{i:04d}_{kind}.pdf").write_bytes(build_pdf(paras))
    print(f"wrote {args.count} PDFs to {out}: {kinds}")


if __name__ == "__main__":
    main()
