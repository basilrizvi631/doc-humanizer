"""Document handlers: load -> list[Segment]; save(segments, replacements) -> same format."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Segment:
    id: str
    text: str
    editable: bool = True
    meta: dict = field(default_factory=dict)


SUPPORTED = (".pdf", ".docx", ".txt", ".md")

WIN_FONTS = "C:/Windows/Fonts"
DEFAULT_FONT_MAP = {
    "Times.*Bold.*Italic": "timesbi.ttf", "Times.*Bold": "timesbd.ttf", "Times.*Italic": "timesi.ttf",
    "Times": "times.ttf", "Calibri.*Bold": "calibrib.ttf", "Calibri.*Italic": "calibrii.ttf",
    "Calibri": "calibri.ttf", "Arial.*Bold": "arialbd.ttf", "Arial.*Italic": "ariali.ttf", "Arial": "arial.ttf",
    "Georgia": "georgia.ttf", "Garamond": "GARA.TTF", "Verdana": "verdana.ttf",
}


def get_handler(path: str, min_words: int = 25, font_dir: str | None = "auto",
                font_map: dict | None = None, max_grow: float = 0.25):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from .pdf_io import PDFHandler

        if font_dir == "auto":
            font_dir = WIN_FONTS if os.path.isdir(WIN_FONTS) else None
        fmap = font_map if font_map else (DEFAULT_FONT_MAP if font_dir else {})
        return PDFHandler(min_words=min_words, font_dir=font_dir, font_map=fmap, max_grow=max_grow)
    if ext == ".docx":
        from .docx_io import DOCXHandler

        return DOCXHandler(min_words=min_words)
    if ext in (".txt", ".md"):
        from .text_io import TextHandler

        return TextHandler(min_words=min_words, markdown=(ext == ".md"))
    raise ValueError(f"Unsupported file type {ext}. Supported: {', '.join(SUPPORTED)}")
