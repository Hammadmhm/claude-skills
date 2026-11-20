"""Plagiarism fingerprinting (MOSS-style winnowing).

Each document is tokenised into lowercase words, turned into overlapping
word k-grams, hashed, and reduced to a fingerprint set by winnowing (keep the
minimum hash in every window of `w` consecutive k-gram hashes). Winnowing
guarantees that any shared passage of at least `w + k - 1` words is detected
while storing only a fraction of the hashes.

The plagiarism percentage of a document is the share of its fingerprints that
also occur in some other indexed document (earlier submission or reference
source) — the same "similarity index" idea used by commercial checkers.

Reference: Schleimer, Wilkerson & Aiken, "Winnowing: Local Algorithms for
Document Fingerprinting", SIGMOD 2003.
"""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _hash(s: str) -> int:
    # 64-bit signed so it fits SQLite's INTEGER type.
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big", signed=True)


def fingerprints(tokens: list[str], k: int = 5, w: int = 4) -> set[int]:
    if len(tokens) < k:
        return set()
    hashes = [_hash(" ".join(tokens[i : i + k])) for i in range(len(tokens) - k + 1)]
    if len(hashes) <= w:
        return {min(hashes)}
    return {min(hashes[i : i + w]) for i in range(len(hashes) - w + 1)}


def containment(doc: set[int], other: set[int]) -> float:
    """Share of `doc`'s fingerprints that also appear in `other` (0..1)."""
    if not doc:
        return 0.0
    return len(doc & other) / len(doc)
