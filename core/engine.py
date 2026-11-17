"""Document -> paragraphs -> ZeroGPT humanize (+ verify with ZeroGPT detect) -> same-format document."""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field

from .guards import clean, missing_protected, words
from .io import get_handler
from .zerogpt import ZeroGPTClient, ZeroGPTError

log = logging.getLogger(__name__)


@dataclass
class Settings:
    target: float = 0.10            # stop when ZeroGPT says <= 10% AI
    max_attempts: int = 3           # humanize calls per paragraph per pass
    verify: bool = True             # score each rewrite with the ZeroGPT detector
    skip_human: bool = True         # leave paragraphs ZeroGPT already scores <= target
    passes: int = 2                 # document-level passes over still-flagged paragraphs
    min_words: int = 25             # shorter blocks (titles, captions, bullets) stay as they are
    length_tol: tuple = (0.70, 1.35)  # rewrites far outside this are deprioritised (PDF fit)
    workers: int = 3
    font_dir: str | None = "auto"


@dataclass
class ParaResult:
    id: str
    page: int | None
    original: str
    final: str
    changed: bool = False
    ai_before: float | None = None
    ai_after: float | None = None
    attempts: int = 0
    missing: list = field(default_factory=list)
    note: str = ""


def _rank(c: dict, s: Settings):
    """Lower is better: facts kept > fits the layout > lowest AI score."""
    lo, hi = s.length_tol
    fit = 0 if lo <= c["ratio"] <= hi else 1
    ai = c["ai"] if c["ai"] is not None else 0.5
    return (len(c["missing"]), fit, ai)


def _fatal(e: Exception) -> bool:
    m = str(e).lower()
    return any(k in m for k in ("credit", "balance", "insufficient", "401", "402", "403", "invalid api key",
                                "unauthorized", "quota", "limit", "words left", "exceeded", "upgrade"))


class Engine:
    def __init__(self, client: ZeroGPTClient, settings: Settings):
        self.c, self.s = client, settings

    # ------------------------------------------------------------ one paragraph
    def humanize_paragraph(self, seg, start: str | None = None, pass_no: int = 1) -> ParaResult:
        s, text = self.s, seg.text
        res = ParaResult(seg.id, seg.meta.get("page"), text, start or text)
        if s.verify:
            try:
                res.ai_before = self.c.detect(start or text)["ai"]
            except ZeroGPTError as e:
                res.note = f"detect failed: {e}"
            if s.skip_human and res.ai_before is not None and res.ai_before <= s.target:
                res.ai_after = res.ai_before
                res.changed = start is not None and start != text
                res.note = res.note or "already reads as human"
                return res
        cands = []
        src = start or text
        for attempt in range(1, s.max_attempts + 1):
            res.attempts = attempt
            try:
                out = clean(self.c.humanize(src, salt=f"p{pass_no}a{attempt}"))
                c = {"text": out, "missing": missing_protected(text, out),
                     "ratio": words(out) / max(1, words(text)), "ai": None}
                if s.verify:
                    c["ai"] = self.c.detect(out)["ai"]
            except ZeroGPTError as e:
                if _fatal(e):
                    raise
                res.note = f"attempt {attempt} failed: {e}"[:200]
                continue
            cands.append(c)
            best = min(cands, key=lambda x: _rank(x, s))
            if not best["missing"] and (not s.verify or best["ai"] <= s.target):
                break
            # next attempt: re-humanize the best version so far (keeps facts), or the original
            src = best["text"] if not best["missing"] else text
        if not cands:
            res.note = res.note or "all attempts failed"
            return res
        best = min(cands, key=lambda x: _rank(x, s))
        res.note = ""
        res.final, res.changed, res.ai_after, res.missing = best["text"], True, best["ai"], best["missing"]
        if best["missing"]:
            res.note = "check: rewrite dropped " + ", ".join(best["missing"][:6])
        return res

    # ------------------------------------------------------------ whole document
    def doc_score(self, texts: list[str], chunk_words: int = 1000):
        chunks, cur, n = [], [], 0
        for t in texts:
            if cur and n + words(t) > chunk_words:
                chunks.append("\n\n".join(cur))
                cur, n = [], 0
            cur.append(t)
            n += words(t)
        if cur:
            chunks.append("\n\n".join(cur))
        tot = wsum = 0.0
        for ch in chunks:
            try:
                r = self.c.detect(ch)
            except ZeroGPTError as e:
                log.warning("doc score chunk failed: %s", e)
                continue
            tot += r["ai"] * words(ch)
            wsum += words(ch)
        return tot / wsum if wsum else None

    def analyze(self, path: str):
        handler = get_handler(path, self.s.min_words, self.s.font_dir)
        segs = handler.load(path)
        todo = [x for x in segs if x.editable]
        return handler, segs, todo

    def run(self, in_path: str, out_path: str | None = None, progress=None, report_path: str | None = None):
        """progress(fraction, message) is called as work proceeds."""
        progress = progress or (lambda f, m: log.info("%3.0f%% %s", f * 100, m))
        s = self.s
        root, ext = os.path.splitext(in_path)
        out_path = out_path or f"{root}_humanized{ext}"
        handler, segs, todo = self.analyze(in_path)
        if not todo:
            raise ZeroGPTError("No paragraphs long enough to rewrite were found (scanned PDF? run OCR first).")
        results: dict[str, ParaResult] = {}
        summary = {"input": in_path, "output": out_path, "paragraphs": len(todo),
                   "words": sum(words(x.text) for x in todo), "passes": []}

        if s.verify:
            progress(0.01, "Scoring the original with ZeroGPT…")
            summary["doc_ai_before"] = self.doc_score([x.text for x in todo])

        total_steps = len(todo) * max(1, s.passes)
        done_steps = 0
        current = todo
        stopped = None
        for p in range(1, max(1, s.passes) + 1):
            label = f"Pass {p}/{s.passes}"
            in_pass = 0
            with ThreadPoolExecutor(max_workers=max(1, s.workers)) as ex:
                futs = {}
                for seg in current:
                    prev = results.get(seg.id)
                    start = prev.final if prev and prev.changed else None
                    futs[ex.submit(self.humanize_paragraph, seg, start, p)] = seg
                for f in as_completed(futs):
                    seg = futs[f]
                    try:
                        r = f.result()
                        prev = results.get(seg.id)
                        if prev is not None and r.ai_after is not None and prev.ai_after is not None \
                                and r.ai_after > prev.ai_after and not r.missing:
                            r = prev  # never make a paragraph worse
                        results[seg.id] = r
                    except ZeroGPTError as e:
                        if _fatal(e):
                            stopped = str(e)  # out of words/credits or bad key: keep what we have
                            ex.shutdown(cancel_futures=True)
                            break
                        results.setdefault(seg.id, ParaResult(seg.id, seg.meta.get("page"), seg.text,
                                                              seg.text, note=f"failed: {e}"))
                    done_steps += 1
                    in_pass += 1
                    progress(min(0.97, done_steps / total_steps), f"{label}: {in_pass}/{len(current)} paragraphs")
            reps = {k: v.final for k, v in results.items() if v.changed}
            save_stats = handler.save(segs, reps, out_path)  # save after every pass
            entry = {"pass": p, "saved": save_stats.get("replaced")}
            if s.verify and not stopped:
                entry["doc_ai"] = self.doc_score([results[x.id].final if x.id in results else x.text for x in todo])
            summary["passes"].append(entry)
            if stopped:
                break
            if not s.verify or p == s.passes:
                break
            if entry.get("doc_ai") is not None and entry["doc_ai"] <= s.target:
                break
            current = [x for x in todo if x.id not in results or not results[x.id].changed
                       or results[x.id].ai_after is None or results[x.id].ai_after > s.target]
            current = [x for x in current if not (results.get(x.id) and results[x.id].note == "already reads as human")]
            if not current:
                break
            total_steps = done_steps + len(current)

        summary["doc_ai_after"] = summary["passes"][-1].get("doc_ai") if summary["passes"] else None
        summary["stopped"] = stopped
        summary["rewritten"] = sum(1 for r in results.values() if r.changed)
        summary["needs_proofread"] = [r.id for r in results.values() if r.missing]
        summary["api_calls"] = dict(self.c.calls)
        summary["save"] = save_stats
        rows = [results.get(x.id) or ParaResult(x.id, x.meta.get("page"), x.text, x.text) for x in todo]
        if report_path:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump({"summary": summary, "paragraphs": [asdict(r) for r in rows]}, f, indent=2, ensure_ascii=False)
        progress(1.0, "Done")
        return out_path, summary, rows
