"""AI-generated-text detection.

Two providers:

* ``heuristic`` (default, offline) — a stylometric screen. It splits the text
  into ~150-word segments and scores each on signals that are statistically
  more common in LLM output: uniform sentence lengths (low "burstiness"),
  stock LLM phrasing ("it is important to note", "delve", ...), and heavy use
  of sentence-initial transitions. The reported percentage is the share of
  words that sit in segments scored as AI-like — the same reporting shape as
  commercial detectors.

  This is a screening heuristic, not a trained classifier. It WILL produce
  false positives and false negatives. For real decisions, plug in a trained
  detector through the ``http`` provider.

* ``http`` — POSTs the text as JSON to any detector endpoint you configure
  (a self-hosted model or a commercial API) and reads the score from the
  response. See README for the environment variables.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import urllib.request

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'“(\[]?[A-Z0-9])")
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)

# Phrases strongly over-represented in LLM output relative to human writing.
AI_MARKERS = [
    "it is important to note", "it is worth noting", "it's important to note",
    "delve", "delves", "delving", "tapestry", "multifaceted", "realm",
    "underscores", "underscore", "pivotal", "paramount", "intricate",
    "intricacies", "foster", "fostering", "navigate the complexities",
    "plays a crucial role", "plays a vital role", "a crucial role",
    "in today's fast-paced", "in today's world", "ever-evolving",
    "ever-changing", "landscape of", "testament to", "serves as a",
    "in conclusion", "in summary", "overall,", "additionally,",
    "furthermore,", "moreover,", "notably,", "crucial", "seamless",
    "seamlessly", "leverage", "leveraging", "holistic", "robust",
    "comprehensive", "showcasing", "showcases", "nuanced", "a myriad of",
    "embark", "harness", "unlock", "elevate", "commendable", "meticulous",
    "meticulously", "bustling", "vibrant", "profound", "invaluable",
]
_MARKER_RE = re.compile(
    "|".join(r"\b" + re.escape(m) + (r"\b" if m[-1].isalnum() else "") for m in AI_MARKERS),
    re.IGNORECASE,
)
_TRANSITIONS = (
    "however", "moreover", "furthermore", "additionally", "overall",
    "in conclusion", "ultimately", "consequently", "therefore", "thus",
    "in addition", "notably", "importantly", "similarly", "conversely",
)

SEGMENT_WORDS = 150


class DetectorError(Exception):
    pass


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    return [s for s in _SENT_SPLIT.split(flat) if len(_WORD.findall(s)) >= 3]


def _segments(sentences: list[str]) -> list[list[str]]:
    segs: list[list[str]] = []
    cur: list[str] = []
    count = 0
    for s in sentences:
        cur.append(s)
        count += len(_WORD.findall(s))
        if count >= SEGMENT_WORDS:
            segs.append(cur)
            cur, count = [], 0
    if cur:
        if segs and count < SEGMENT_WORDS / 2:
            segs[-1].extend(cur)  # fold a short tail into the previous segment
        else:
            segs.append(cur)
    return segs


def score_segment(sentences: list[str]) -> dict:
    lengths = [len(_WORD.findall(s)) for s in sentences]
    words = sum(lengths)
    mean_len = statistics.fmean(lengths) if lengths else 0.0
    cv = (statistics.pstdev(lengths) / mean_len) if len(lengths) > 1 and mean_len else 0.6
    text = " ".join(sentences)
    markers = len(_MARKER_RE.findall(text))
    marker_rate = markers * 100.0 / max(words, 1)
    transition_ratio = sum(1 for s in sentences if s.lower().startswith(_TRANSITIONS)) / max(len(sentences), 1)

    # Hand-set logistic weights (see module docstring — not a trained model).
    z = (
        -1.6
        + 7.0 * (0.45 - cv)  # human prose is burstier (cv typically 0.5-0.8)
        + 1.1 * marker_rate  # stock-phrase density per 100 words
        + 3.0 * transition_ratio
        + 0.06 * max(0.0, 26.0 - abs(mean_len - 21.0) * 2.0) / 2.0  # LLM sweet-spot sentence length
    )
    prob = 1.0 / (1.0 + math.exp(-z))
    return {
        "words": words,
        "probability": round(prob, 3),
        "burstiness": round(cv, 3),
        "marker_hits": markers,
        "transition_ratio": round(transition_ratio, 3),
        "text": text,
    }


def detect_heuristic(text: str) -> dict:
    segments = [score_segment(seg) for seg in _segments(_sentences(text))]
    total = sum(s["words"] for s in segments)
    if not total:
        return {"ai_pct": 0.0, "provider": "heuristic", "flagged_segments": [], "segments": 0}
    flagged = [s for s in segments if s["probability"] >= 0.5]
    ai_pct = 100.0 * sum(s["words"] for s in flagged) / total
    top = sorted(flagged, key=lambda s: s["probability"], reverse=True)[:5]
    return {
        "ai_pct": round(ai_pct, 1),
        "provider": "heuristic",
        "segments": len(segments),
        "flagged_segments": [
            {
                "probability": s["probability"],
                "burstiness": s["burstiness"],
                "marker_hits": s["marker_hits"],
                "excerpt": (s["text"][:400] + "…") if len(s["text"]) > 400 else s["text"],
            }
            for s in top
        ],
    }


def _dig(obj, dotted: str):
    for part in dotted.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def detect_http(text: str, opts: dict) -> dict:
    url = opts.get("ai_detector_url")
    if not url:
        raise DetectorError("AI_DETECTOR=http but AI_DETECTOR_URL is not set")
    body = json.dumps({opts.get("ai_detector_text_field", "text"): text[: opts.get("ai_detector_max_chars", 50000)]})
    headers = {"Content-Type": "application/json", **(opts.get("ai_detector_headers") or {})}
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — operator-configured URL
            payload = json.loads(resp.read().decode("utf-8"))
        raw = float(_dig(payload, opts.get("ai_detector_score_field", "score")))
    except Exception as exc:
        raise DetectorError(f"AI detector request failed: {exc}") from exc
    scale = float(opts.get("ai_detector_score_scale", 1.0)) or 1.0
    pct = max(0.0, min(100.0, raw * 100.0 / scale))
    return {"ai_pct": round(pct, 1), "provider": "http", "flagged_segments": [], "segments": None}


def detect(text: str, opts: dict) -> dict:
    provider = opts.get("ai_detector", "heuristic")
    if provider == "http":
        return detect_http(text, opts)
    if provider == "heuristic":
        return detect_heuristic(text)
    raise DetectorError(f"Unknown AI_DETECTOR provider: {provider!r}")
