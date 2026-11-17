"""DOCX: rewrite paragraph text in place, keeping paragraph style and first-run formatting.

Paragraphs containing hyperlinks, images, equations, fields or tracked changes are skipped
so nothing structural is lost.
"""
from __future__ import annotations

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from . import Segment

_SKIP_XPATH = ".//w:hyperlink | .//w:drawing | .//w:pict | .//m:oMath | .//w:fldChar | .//w:instrText | .//w:ins | .//w:del | .//w:footnoteReference"


class DOCXHandler:
    def __init__(self, min_words=25):
        self.min_words = min_words
        self.doc = None
        self.paras: list[Paragraph] = []

    def _iter(self, parent_elm, parent):
        for child in parent_elm.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                yield Paragraph(child, parent)
            elif tag == "tbl":
                tbl = Table(child, parent)
                for row in tbl.rows:
                    seen = set()
                    for cell in row.cells:
                        if id(cell._tc) in seen:  # merged cells repeat
                            continue
                        seen.add(id(cell._tc))
                        yield from self._iter(cell._tc, cell)

    def load(self, path: str) -> list[Segment]:
        self.doc = Document(path)
        self.paras = list(self._iter(self.doc.element.body, self.doc))
        segs = []
        for i, p in enumerate(self.paras):
            text = p.text
            complex_ = bool(p._p.xpath(_SKIP_XPATH))
            heading = (p.style is not None and p.style.name.lower().startswith(("heading", "title")))
            editable = len(text.split()) >= self.min_words and not complex_ and not heading
            segs.append(Segment(f"d{i}", text, editable, {"index": i}))
        return segs

    def save(self, segments: list[Segment], replacements: dict[str, str], out_path: str) -> dict:
        n = 0
        for s in segments:
            new = replacements.get(s.id)
            if new is None or new == s.text:
                continue
            p = self.paras[s.meta["index"]]
            runs = p.runs
            if not runs:
                p.add_run(new)
            else:
                runs[0].text = new
                for r in runs[1:]:
                    r._r.getparent().remove(r._r)
            n += 1
        self.doc.save(out_path)
        return {"replaced": n}
