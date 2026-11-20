"""Runtime settings, read once from environment variables.

Every knob has a safe default so `uvicorn app.main:app` works with no config.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", APP_ROOT / "data")))

    # Decision rule: a submission is rejected when either score is strictly
    # greater than this percentage.
    reject_threshold: float = field(default_factory=lambda: _float("REJECT_THRESHOLD", 30.0))

    # Upload limits.
    max_file_mb: int = field(default_factory=lambda: _int("MAX_FILE_MB", 50))

    # Parallelism: number of worker processes that extract + analyse PDFs.
    workers: int = field(default_factory=lambda: _int("WORKERS", max(1, (os.cpu_count() or 2))))
    per_file_timeout_s: int = field(default_factory=lambda: _int("PER_FILE_TIMEOUT_S", 300))

    # Documents with fewer extractable words than this go to manual review
    # (usually scanned PDFs that need OCR).
    min_words: int = field(default_factory=lambda: _int("MIN_WORDS", 50))

    # Plagiarism fingerprinting (MOSS-style winnowing over word k-grams).
    shingle_k: int = field(default_factory=lambda: _int("SHINGLE_K", 5))
    winnow_w: int = field(default_factory=lambda: _int("WINNOW_W", 4))
    # A source document only counts if it shares at least this many
    # fingerprints — filters out stock phrases shared by unrelated papers.
    min_source_matches: int = field(default_factory=lambda: _int("MIN_SOURCE_MATCHES", 3))
    exclude_bibliography: bool = field(default_factory=lambda: _bool("EXCLUDE_BIBLIOGRAPHY", True))
    exclude_quotes: bool = field(default_factory=lambda: _bool("EXCLUDE_QUOTES", True))

    # AI detection provider: "heuristic" (built in, offline) or "http"
    # (any external detector reachable over HTTP — see README).
    ai_detector: str = field(default_factory=lambda: os.getenv("AI_DETECTOR", "heuristic").strip().lower())
    ai_detector_url: str = field(default_factory=lambda: os.getenv("AI_DETECTOR_URL", ""))
    ai_detector_headers: dict = field(
        default_factory=lambda: json.loads(os.getenv("AI_DETECTOR_HEADERS", "{}") or "{}")
    )
    ai_detector_text_field: str = field(default_factory=lambda: os.getenv("AI_DETECTOR_TEXT_FIELD", "text"))
    ai_detector_score_field: str = field(default_factory=lambda: os.getenv("AI_DETECTOR_SCORE_FIELD", "score"))
    # "1" if the API returns 0..1, "100" if it returns a percentage.
    ai_detector_score_scale: float = field(default_factory=lambda: _float("AI_DETECTOR_SCORE_SCALE", 1.0))
    ai_detector_max_chars: int = field(default_factory=lambda: _int("AI_DETECTOR_MAX_CHARS", 50000))

    # Optional HTTP Basic auth for the whole site. Leave unset to disable.
    app_username: str = field(default_factory=lambda: os.getenv("APP_USERNAME", "admin"))
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "integrity.sqlite3"

    def analysis_options(self) -> dict:
        """The picklable subset handed to worker processes."""
        return {
            "min_words": self.min_words,
            "shingle_k": self.shingle_k,
            "winnow_w": self.winnow_w,
            "exclude_bibliography": self.exclude_bibliography,
            "exclude_quotes": self.exclude_quotes,
            "ai_detector": self.ai_detector,
            "ai_detector_url": self.ai_detector_url,
            "ai_detector_headers": self.ai_detector_headers,
            "ai_detector_text_field": self.ai_detector_text_field,
            "ai_detector_score_field": self.ai_detector_score_field,
            "ai_detector_score_scale": self.ai_detector_score_scale,
            "ai_detector_max_chars": self.ai_detector_max_chars,
        }


settings = Settings()
