"""Manual round-trip mode (for tools without an API, e.g. QuillBot's AI Humanizer).

The document is split into chunks that fit the tool's free word limit (at sentence
boundaries). You paste each chunk into the tool and paste the result back; the document is
then rebuilt in its original format. Progress autosaves, so you can stop and resume.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .guards import clean, missing_protected, words
from .io import get_handler

_SENT = re.compile(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\"'(\[])")


def split_sentences(text: str) -> list[str]:
    out = []
    for p in _SENT.split(text.strip()):
        if out and re.search(r"\b(?:e\.g|i\.e|et al|etc|vs|Fig|Eq|approx)\.$", out[-1], re.I):
            out[-1] += " " + p
        else:
            out.append(p)
    return [s for s in out if s.strip()]


def make_chunks(text: str, max_words: int) -> list[str]:
    chunks, cur = [], []
    for s in split_sentences(text):
        if cur and words(" ".join(cur + [s])) > max_words:
            chunks.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def file_key(path: str, max_words: int, min_words: int) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    h.update(f"{max_words}-{min_words}".encode())
    return h.hexdigest()[:16]


class ManualJob:
    def __init__(self, path: str, max_words: int = 120, min_words: int = 25, progress_dir: str = ".progress"):
        self.path, self.max_words, self.min_words = path, max_words, min_words
        handler = get_handler(path, min_words)
        segs = handler.load(path)
        self.units = []
        for s in segs:
            if not s.editable:
                continue
            for k, ch in enumerate(make_chunks(s.text, max_words)):
                self.units.append({"seg": s.id, "k": k, "page": s.meta.get("page"), "text": ch, "out": ""})
        os.makedirs(progress_dir, exist_ok=True)
        self.save_path = os.path.join(progress_dir, file_key(path, max_words, min_words) + ".json")
        self.i = 0
        if os.path.exists(self.save_path):  # resume
            with open(self.save_path, encoding="utf-8") as f:
                saved = json.load(f)
            outs = {(u["seg"], u["k"]): u["out"] for u in saved.get("units", [])}
            for u in self.units:
                u["out"] = outs.get((u["seg"], u["k"]), "")
            self.i = min(saved.get("i", 0), max(0, len(self.units) - 1))

    # ---------------------------------------------------------------- state
    @property
    def done(self) -> int:
        return sum(1 for u in self.units if u["out"].strip())

    def persist(self):
        tmp = self.save_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"source": os.path.basename(self.path), "i": self.i, "units": self.units}, f,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, self.save_path)

    def set_output(self, idx: int, text: str):
        if 0 <= idx < len(self.units):
            self.units[idx]["out"] = clean(text or "")
            self.persist()

    def check(self, idx: int) -> list[str]:
        u = self.units[idx]
        return missing_protected(u["text"], u["out"]) if u["out"].strip() else []

    def first_todo(self) -> int:
        for n, u in enumerate(self.units):
            if not u["out"].strip():
                return n
        return len(self.units) - 1

    # ---------------------------------------------------------------- build
    def build(self, out_path: str) -> dict:
        handler = get_handler(self.path, self.min_words)
        segs = handler.load(self.path)
        parts: dict[str, list[str]] = {}
        touched = set()
        for u in self.units:
            parts.setdefault(u["seg"], []).append(u["out"].strip() or u["text"])
            if u["out"].strip():
                touched.add(u["seg"])
        reps = {sid: " ".join(p) for sid, p in parts.items() if sid in touched}
        stats = handler.save(segs, reps, out_path)
        stats["chunks_humanized"] = self.done
        stats["chunks_total"] = len(self.units)
        stats["chunks_with_missing_facts"] = [n + 1 for n in range(len(self.units)) if self.check(n)]
        return stats
