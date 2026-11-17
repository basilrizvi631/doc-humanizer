"""Layout-preserving PDF rewrite.

load():  split each text block into paragraphs (by font-size change / vertical gap), record
         bbox, line boxes, dominant font/size/colour/weight, alignment and line spacing.
save():  redact ONLY the old glyphs (images and vector graphics are kept), then flow the new
         text into the same box with insert_htmlbox using the recorded style. If the new text
         is longer it may grow into empty space below (max_grow) and is then scaled to fit.
"""
from __future__ import annotations

import html
import logging
import os
import re
import statistics
from collections import Counter, defaultdict

try:
    import pymupdf as fitz
except ImportError:  # older installs
    import fitz  # type: ignore

from . import Segment

log = logging.getLogger(__name__)

_SERIF = re.compile(r"times|serif|roman|georgia|cambria|garamond|minion|palatino|book|cmr|nimbusrom", re.I)
_MONO = re.compile(r"mono|courier|consol|menlo|cmtt", re.I)


_BULLET = re.compile(r"^([\u2022\u25cf\u25aa\u25e6\u2023\u2043\u2219\-\u2013*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+")
_MATH_CHARS = set("=+\u00b1\u00d7\u00f7\u2211\u03a3\u222b\u2264\u2265\u2208\u03bb\u03c3\u03bc\u03b1\u03b5\u0177^_{}\\|\u2212")


def _is_math(t: str) -> bool:
    t = t.strip()
    if not t:
        return False
    if re.search(r"arg(?:max|min)_\w|\bReLU\(|\u2211|\u03a3_", t):
        return True
    words = len(re.findall(r"[A-Za-z]{4,}", t))
    if re.search(r"\s=\s", t) and words < 5:
        return True                                   # a formula line, not prose with "x = 5" in it
    return sum(ch in _MATH_CHARS for ch in t) / len(t) > 0.08 and words < 8


def _span_text(line) -> str:
    return "".join(s["text"] for s in line["spans"])


class PDFHandler:
    def __init__(self, min_words=25, font_dir=None, font_map=None, max_grow=0.25):
        self.min_words = min_words
        self.font_dir = font_dir
        self.font_map = font_map or {}
        self.max_grow = max_grow
        self.path = None

    # ------------------------------------------------------------------ load
    def load(self, path: str) -> list[Segment]:
        self.path = path
        doc = fitz.open(path)
        segs: list[Segment] = []
        total_chars = 0
        for pno, page in enumerate(doc):
            d = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES)
            # Collect lines from ALL blocks: Word/LaTeX exports often store one line per block.
            lines = [l for b in d["blocks"] if b.get("type") == 0
                     for l in b["lines"] if _span_text(l).strip()]
            lines = self._merge_markers(lines)
            for gno, group in enumerate(self._group_lines(lines)):
                seg = self._make_segment(pno, gno, group)
                total_chars += len(seg.text)
                segs.append(seg)
        doc.close()
        # never touch the bibliography or citation-led entries
        in_refs = False
        for seg in segs:
            head = seg.text.strip().lower().rstrip(":")
            if re.fullmatch(r"(\d+\.?\s*)?(references|bibliography|works cited|literature cited)", head):
                in_refs = True
            if in_refs or re.match(r"^\[\d+\]", seg.text):
                seg.editable = False
        if total_chars < 50:
            log.warning("Almost no extractable text: this looks like a scanned PDF. "
                        "Run OCR first, e.g. `ocrmypdf in.pdf in_ocr.pdf`.")
        return segs

    # ---- paragraph reconstruction -------------------------------------------------
    @staticmethod
    def _baseline(line) -> float:
        return line["spans"][0]["origin"][1]

    @staticmethod
    def _is_bold(line) -> bool:
        c = Counter()
        for s in line["spans"]:
            bold = bool(s["flags"] & 16) or bool(re.search(r"bold|black|heavy", s["font"], re.I))
            c[bold] += len(s["text"].strip())
        return c.most_common(1)[0][0]

    def _continues(self, cur, ln) -> bool:
        prev = cur[-1]
        tp, tc = _span_text(prev).strip(), _span_text(ln).strip()
        ps, cs = self._line_size(prev), self._line_size(ln)
        if abs(ps - cs) > 0.6 or self._is_bold(prev) != self._is_bold(ln):
            return False                                  # heading / style change
        if _is_math(tp) or _is_math(tc):
            return False                                  # equations stay on their own
        if _BULLET.match(tc) and (tc[0] in "\u2022\u25cf\u25aa\u25e6\u2023\u2043\u2219-\u2013*"
                                  or tp.endswith((".", ";", ":"))):
            return False                                  # new list item
        pitch = self._baseline(ln) - self._baseline(prev)
        if pitch <= 0.5 or pitch > 2.3 * cs:
            return False                                  # same row (table) / column jump / big gap
        if len(cur) >= 2:
            prev_pitch = self._baseline(prev) - self._baseline(cur[-2])
            if pitch > prev_pitch * 1.25 + 1:
                return False                              # paragraph spacing
        if ln["bbox"][0] > prev["bbox"][2] or ln["bbox"][2] < prev["bbox"][0]:
            return False                                  # no horizontal overlap
        left = min(l["bbox"][0] for l in cur)
        right = max(l["bbox"][2] for l in cur)
        ends_sentence = tp.endswith((".", ":", "?", "!"))
        in_list = bool(_BULLET.match(_span_text(cur[0]).strip()))
        if in_list and ends_sentence:
            return False                                  # list item finished
        if not in_list and ln["bbox"][0] - left > 8 and ends_sentence:
            return False                                  # first-line indent of next paragraph
        if len(cur) >= 2 and ends_sentence and prev["bbox"][2] < left + 0.8 * (right - left):
            return False                                  # short last line ended the paragraph
        return True

    @staticmethod
    def _merge_markers(lines):
        """Join pieces of one visual line: a lone bullet/number + its text, or Word's split fragments."""
        out = []
        for ln in lines:
            if out:
                prev = out[-1]
                ptxt = _span_text(prev).strip()
                same_row = abs(prev["spans"][0]["origin"][1] - ln["spans"][0]["origin"][1]) < 1.5
                gap = ln["bbox"][0] - prev["bbox"][2]
                size = max(sp["size"] for sp in prev["spans"])
                marker = len(ptxt) <= 4 and _BULLET.match(ptxt + " ") and 0 <= gap < 40
                same_size = abs(size - max(sp["size"] for sp in ln["spans"])) < 0.6
                fragment = same_size and -1 <= gap < 1.6 * size  # Word split one visual line (justified gaps can be wide)
                if same_row and (marker or fragment):
                    merged = dict(ln)
                    merged["spans"] = prev["spans"] + [dict(sp) for sp in ln["spans"]]
                    if not prev["spans"][-1]["text"].endswith(" "):
                        merged["spans"][len(prev["spans"]) - 1] = dict(prev["spans"][-1], text=prev["spans"][-1]["text"] + " ")
                    merged["bbox"] = (prev["bbox"][0], min(prev["bbox"][1], ln["bbox"][1]),
                                      ln["bbox"][2], max(prev["bbox"][3], ln["bbox"][3]))
                    out[-1] = merged
                    continue
            out.append(ln)
        return out

    def _group_lines(self, lines):
        groups, cur = [], []
        for ln in lines:
            if cur and not self._continues(cur, ln):
                groups.append(cur)
                cur = []
            cur.append(ln)
        if cur:
            groups.append(cur)
        return groups

    @staticmethod
    def _line_size(line) -> float:
        c = Counter()
        for s in line["spans"]:
            c[round(s["size"], 1)] += len(s["text"])
        return c.most_common(1)[0][0]

    @staticmethod
    def _join(lines) -> str:
        out = ""
        for ln in lines:
            t = re.sub(r"\s+", " ", _span_text(ln)).strip()
            if not t:
                continue
            if not out:
                out = t
            elif re.search(r"[A-Za-z]-$", out) and t[:1].islower():
                out = out[:-1] + t  # de-hyphenate line breaks
            else:
                out += " " + t
        return out

    def _make_segment(self, pno, gno, lines) -> Segment:
        text = self._join(lines)
        x0 = min(l["bbox"][0] for l in lines)
        y0 = min(l["bbox"][1] for l in lines)
        x1 = max(l["bbox"][2] for l in lines)
        y1 = max(l["bbox"][3] for l in lines)
        style = self._style(lines)
        rotated = any(abs(l["dir"][1]) > 1e-3 for l in lines)
        math = any(_is_math(_span_text(l)) for l in lines)

        prefix, hang = "", 0.0
        m = _BULLET.match(text)
        if m:
            prefix = m.group(0)
            text = text[len(prefix):]
            first = lines[0]
            if len(lines) > 1:
                hang = max(0.0, lines[1]["bbox"][0] - first["bbox"][0])
            else:  # find where the text after the bullet starts
                for sp in first["spans"]:
                    if sp["text"].strip() and not _BULLET.match(sp["text"].strip() + " "):
                        hang = max(0.0, sp["bbox"][0] - first["bbox"][0])
                        break
        # paragraphs split by a page/column break: rewriting half a sentence breaks the join
        complete = (bool(re.search(r"[.!?:;)\]\"']\s*$", text))
                    and (not text[:1].islower() or text.split()[0] in ("where", "with", "which")))
        editable = (len(text.split()) >= self.min_words and not rotated and not math and complete
                    and not self._looks_like_table_or_math(text))
        meta = {
            "page": pno,
            "bbox": [x0, y0, x1, y1],
            "line_bboxes": [list(l["bbox"]) for l in lines],
            "style": style,
            "align": self._align(lines, x0, x1),
            "line_height": self._line_height(lines, style["size"]),
            "indent": 0.0 if prefix else (max(0.0, lines[0]["bbox"][0] - x0) if len(lines) > 1 else 0.0),
            "first_baseline": lines[0]["spans"][0]["origin"][1],
            "prefix": prefix,
            "hang": hang,
        }
        return Segment(f"p{pno}_g{gno}", text, editable, meta)

    @staticmethod
    def _looks_like_table_or_math(text: str) -> bool:
        if not text:
            return True
        symbols = sum(ch in "=+±×÷∑∫≤≥∈λσμ^_{}\\|" for ch in text)
        digits = sum(ch.isdigit() for ch in text)
        return symbols / len(text) > 0.05 or digits / len(text) > 0.35

    @staticmethod
    def _style(lines) -> dict:
        c = Counter()
        for ln in lines:
            for s in ln["spans"]:
                c[(s["font"], round(s["size"], 1), s["color"], s["flags"])] += len(s["text"].strip())
        font, size, color, flags = c.most_common(1)[0][0]
        if _MONO.search(font) or flags & 8:
            family = "monospace"
        elif _SERIF.search(font) or flags & 4:
            family = "serif"
        else:
            family = "sans-serif"
        return {
            "font": font, "size": size, "color": f"#{color:06x}", "family": family,
            "bold": bool(flags & 16) or bool(re.search(r"bold|black|heavy|semibold", font, re.I)),
            "italic": bool(flags & 2) or bool(re.search(r"italic|oblique", font, re.I)),
        }

    @staticmethod
    def _align(lines, x0, x1) -> str:
        if len(lines) < 3:
            return "left"
        body = lines[:-1]
        rights = [l["bbox"][2] for l in body]
        lefts = [l["bbox"][0] for l in body]
        width = x1 - x0
        centers = [(l["bbox"][0] + l["bbox"][2]) / 2 for l in lines]
        if statistics.pstdev(lefts) > 3 and statistics.pstdev(centers) < 2:
            return "center"
        if statistics.pstdev(rights) < 2.0 and min(rights) > x1 - 0.03 * width:
            return "justify"
        return "left"

    @staticmethod
    def _line_height(lines, size) -> float:
        ys = [l["spans"][0]["origin"][1] for l in lines]
        diffs = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0]
        if not diffs or not size:
            return 1.2
        return max(1.0, min(2.2, statistics.median(diffs) / size))

    # ------------------------------------------------------------------ save
    def _font_css(self, style) -> tuple[str, str]:
        """Return (font-family value, extra @font-face css) honoring font_map if given."""
        if self.font_dir:
            for pattern, file in self.font_map.items():
                if re.search(pattern, style["font"], re.I) and os.path.exists(os.path.join(self.font_dir, file)):
                    fam = "f_" + re.sub(r"\W", "", pattern)
                    return fam, f"@font-face {{font-family: {fam}; src: url({file});}}\n"
        return style["family"], ""

    def _html(self, seg: Segment, text: str) -> tuple[str, str]:
        st = seg.meta["style"]
        fam, face = self._font_css(st)
        css = face + (
            "* {margin:0; padding:0;}\n"
            f"p {{font-family: {fam}; font-size: {st['size']}px; color: {st['color']};"
            f" line-height: {seg.meta['line_height']:.3f}; text-align: {seg.meta['align']};"
            f" font-weight: {'bold' if st['bold'] else 'normal'};"
            f" font-style: {'italic' if st['italic'] else 'normal'};"
            f" text-indent: {seg.meta['indent'] - seg.meta.get('hang', 0):.1f}px;"
            f" margin-left: {seg.meta.get('hang', 0):.1f}px;}}"
        )
        return f"<p>{html.escape(seg.meta.get('prefix', '') + text)}</p>", css

    @staticmethod
    def _grow_limit(rect, others, page_rect, max_grow) -> float:
        """How far the box may extend downwards without hitting other content."""
        limit = min(page_rect.y1 - 18, rect.y1 + rect.height * max_grow)
        for o in others:
            if o.y0 >= rect.y1 - 1 and o.x0 < rect.x1 and o.x1 > rect.x0:
                limit = min(limit, o.y0 - 2)
        return max(rect.y1, limit)

    def _probe(self, css: str, archive) -> tuple[float, float]:
        """Measure where the HTML engine puts the first glyph relative to the box (cached per css)."""
        cache = self.__dict__.setdefault("_probe_cache", {})
        if css in cache:
            return cache[css]
        tmp = fitz.open()
        pg = tmp.new_page(width=1000, height=1000)
        box = fitz.Rect(100, 100, 900, 900)
        kw = {"css": css.replace("text-indent", "x-indent").replace("margin-left", "x-margin-left")}
        if archive is not None:
            kw["archive"] = archive
        pg.insert_htmlbox(box, "<p>Hxg probe</p>", **kw)
        try:
            ln = pg.get_text("dict")["blocks"][0]["lines"][0]
            res = (ln["bbox"][0] - box.x0, ln["spans"][0]["origin"][1] - box.y0)
        except (IndexError, KeyError):
            res = (0.0, 0.0)
        tmp.close()
        cache[css] = res
        return res

    def save(self, segments: list[Segment], replacements: dict[str, str], out_path: str) -> dict:
        doc = fitz.open(self.path)
        archive = fitz.Archive(self.font_dir) if self.font_dir else None
        todo = defaultdict(list)
        for s in segments:
            new = replacements.get(s.id)
            if new is not None and new.strip() and new != s.text:
                todo[s.meta["page"]].append((s, new))
        stats = {"replaced": 0, "shrunk": [], "failed": []}
        for pno, items in todo.items():
            page = doc[pno]
            page_segs = [x for x in segments if x.meta.get("page") == pno]
            images = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[6] == 1]
            for s, _ in items:
                for lb in s.meta["line_bboxes"]:
                    page.add_redact_annot(fitz.Rect(lb), fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for s, new in items:
                rect = fitz.Rect(s.meta["bbox"])
                body, css = self._html(s, new)
                # place the box so the first baseline / left edge land exactly where the old ones were
                dx, dy = self._probe(css, archive)
                if s.meta["align"] == "center":
                    dx = 0.0
                top = s.meta.get("first_baseline", rect.y0 + dy) - dy
                # every other paragraph keeps its original area, rewritten or not
                others = [fitz.Rect(x.meta["bbox"]) for x in page_segs if x.id != s.id] + images
                grown = fitz.Rect(rect.x0 - dx, top, rect.x1 - dx + 0.5,
                                  self._grow_limit(rect, others, page.rect, self.max_grow))
                kw = {"css": css, "scale_low": 0}
                if archive is not None:
                    kw["archive"] = archive
                spare, scale = page.insert_htmlbox(grown, body, **kw)
                if spare < 0:
                    stats["failed"].append(s.id)
                    log.error("could not fit text for %s on page %d", s.id, pno + 1)
                else:
                    stats["replaced"] += 1
                    if scale < 0.9:
                        stats["shrunk"].append((s.id, round(scale, 2)))
        doc.save(out_path, garbage=3, deflate=True)
        doc.close()
        if stats["shrunk"]:
            log.info("%d paragraphs were scaled down to fit (consider a tighter length_tolerance)",
                     len(stats["shrunk"]))
        return stats
