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

    # Internet plagiarism search: "off", "brave", "serper" or "searxng".
    web_search: str = field(default_factory=lambda: os.getenv("WEB_SEARCH", "off").strip().lower())
    web_search_api_key: str = field(default_factory=lambda: os.getenv("WEB_SEARCH_API_KEY", ""))
    searxng_url: str = field(default_factory=lambda: os.getenv("SEARXNG_URL", ""))
    brave_api_url: str = field(
        default_factory=lambda: os.getenv("BRAVE_API_URL", "https://api.search.brave.com/res/v1/web/search"))
    serper_api_url: str = field(default_factory=lambda: os.getenv("SERPER_API_URL", "https://google.serper.dev/search"))
    web_queries_per_doc: int = field(default_factory=lambda: _int("WEB_QUERIES_PER_DOC", 8))
    web_results_per_query: int = field(default_factory=lambda: _int("WEB_RESULTS_PER_QUERY", 10))
    web_max_pages_per_doc: int = field(default_factory=lambda: _int("WEB_MAX_PAGES_PER_DOC", 5))
    web_search_qps: float = field(default_factory=lambda: _float("WEB_SEARCH_QPS", 1.0))
    web_concurrency: int = field(default_factory=lambda: _int("WEB_CONCURRENCY", 8))
    web_fetch_timeout_s: int = field(default_factory=lambda: _int("WEB_FETCH_TIMEOUT_S", 15))
    web_fetch_max_mb: int = field(default_factory=lambda: _int("WEB_FETCH_MAX_MB", 10))
    # A submission goes to needs_review when more than this share of its
    # web queries fail (quota exhausted, provider down, ...).
    web_max_failed_share: float = field(default_factory=lambda: _float("WEB_MAX_FAILED_SHARE", 0.5))
    # Only for testing against local mock servers. Never enable in production:
    # it lets search results point the server at internal addresses.
    web_allow_private_hosts: bool = field(default_factory=lambda: _bool("WEB_ALLOW_PRIVATE_HOSTS", False))

    # Optional HTTP Basic auth for the whole site. Leave unset to disable.
    app_username: str = field(default_factory=lambda: os.getenv("APP_USERNAME", "admin"))
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "integrity.sqlite3"

    @property
    def web_enabled(self) -> bool:
        return self.web_search not in ("", "off", "none", "false", "0")

    def analysis_options(self) -> dict:
        """The picklable subset handed to worker processes."""
        return {
            "web_queries": self.web_queries_per_doc if self.web_enabled else 0,
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
