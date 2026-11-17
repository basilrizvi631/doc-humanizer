"""Plain text / Markdown. Paragraphs = blocks separated by blank lines; separators preserved.
In Markdown, headings, code fences, tables, lists, quotes and HTML blocks are left alone."""
from __future__ import annotations

import re

from . import Segment

_MD_SKIP = re.compile(r"^\s*(#|\||[-*+] |\d+[.)] |>|<|!\[|\$\$|---|===)")


class TextHandler:
    def __init__(self, min_words=25, markdown=False):
        self.min_words, self.markdown = min_words, markdown
        self.parts: list[str] = []

    def load(self, path: str) -> list[Segment]:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        self.parts = re.split(r"(\n[ \t]*\n+)", raw)
        segs, in_code = [], False
        for i, part in enumerate(self.parts):
            if i % 2 == 1:  # separator
                continue
            fences = part.count("```")
            code = in_code or fences > 0
            if fences % 2 == 1:
                in_code = not in_code
            skip = self.markdown and (code or any(_MD_SKIP.match(l) for l in part.splitlines() if l.strip()))
            text = re.sub(r"\s*\n\s*", " ", part).strip()
            editable = bool(text) and not skip and len(text.split()) >= self.min_words
            segs.append(Segment(f"t{i}", text, editable, {"index": i, "raw": part}))
        return segs

    def save(self, segments: list[Segment], replacements: dict[str, str], out_path: str) -> dict:
        parts = list(self.parts)
        n = 0
        for s in segments:
            new = replacements.get(s.id)
            if new is not None and new != s.text:
                lead = re.match(r"^\s*", s.meta["raw"]).group(0)
                trail = re.search(r"\s*$", s.meta["raw"]).group(0)
                parts[s.meta["index"]] = lead + new + trail
                n += 1
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        return {"replaced": n}
