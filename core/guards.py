"""Checks that a rewrite kept the facts, and light cleanup of API output."""
from __future__ import annotations

import re

_PROTECT = re.compile(
    r"https?://\S+|"                      # URLs
    r"\[\d+(?:[,\u2013-]\s*\d+)*\]|"      # [3], [1, 4], [2-5]
    r"\b\d+(?:[.,]\d+)*%?|"               # numbers / percentages / years
    r"\b[A-Z]{2,}[0-9A-Z-]*\b"            # acronyms: LULC, AOI, NDVI, B11
)


def protected_tokens(text: str) -> set[str]:
    return {m.group(0).rstrip(".,") for m in _PROTECT.finditer(text)}


def missing_protected(src: str, out: str) -> list[str]:
    have = protected_tokens(out)
    return sorted(t for t in protected_tokens(src) if t not in have and t not in out)


def clean(text: str) -> str:
    t = text.strip()
    if len(t) > 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*", " ", t)  # paragraph text must be one block
    return t.strip()


def words(t: str) -> int:
    return len(t.split())
