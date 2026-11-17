"""Clipboard autopilot for the QuillBot (manual) workflow.

    python autopilot.py "C:\\path\\paper.pdf"

Loop, without ever switching windows:
  1. The current chunk is already on your clipboard -> in QuillBot: click the input, Ctrl+A, Ctrl+V,
     click "Humanize".
  2. Click QuillBot's copy button on the output (or select it + Ctrl+C).
  3. The autopilot sees the new clipboard text, checks it kept every number/citation/acronym,
     saves it, and puts the NEXT chunk on your clipboard. Repeat.

Keys in this window (Windows): [b] back one chunk, [s] skip chunk, [r] re-copy current chunk,
[q] finish now and build the document.
Progress is shared with the app's QuillBot tab (same .progress folder), so you can mix both.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

try:
    import pyperclip
except ImportError:
    sys.exit("Missing package: run  pip install pyperclip")

from core.manual import ManualJob

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import msvcrt  # Windows: non-blocking key presses

    def key_pressed():
        if msvcrt.kbhit():
            return msvcrt.getwch().lower()
        return None
except ImportError:  # other OS: no hotkeys, clipboard loop still works
    def key_pressed():
        return None


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", t.lower()))


def looks_like_rewrite(chunk: str, clip: str) -> bool:
    """Accept clipboard text only if it plausibly is a rewrite of this chunk
    (so copying a URL or something unrelated by accident is ignored)."""
    clip = clip.strip()
    if not clip or clip == chunk.strip():
        return False
    n = len(chunk.split())
    if not 0.5 * n <= len(clip.split()) <= 1.8 * n:
        return False
    cw = _words(chunk)
    return len(cw & _words(clip)) / max(1, len(cw)) >= 0.30


def beep(ok: bool = True):
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:  # noqa: BLE001
        print("\a", end="", flush=True)


def show(job: ManualJob):
    u = job.units[job.i]
    page = f"page {u['page'] + 1}, " if u.get("page") is not None else ""
    print(f"\n── Chunk {job.i + 1}/{len(job.units)} ({page}{len(u['text'].split())} words) · "
          f"{job.done} done ──  copied to clipboard ✓")
    print("   " + u["text"][:160] + ("…" if len(u["text"]) > 160 else ""))


def put(job: ManualJob) -> str:
    text = job.units[job.i]["text"]
    pyperclip.copy(text)
    show(job)
    return text


def main():
    ap = argparse.ArgumentParser(description="Clipboard autopilot for humanizing a document with QuillBot by hand.")
    ap.add_argument("input")
    ap.add_argument("--max-words", type=int, default=120, help="chunk size (QuillBot free ≈ 125)")
    ap.add_argument("--min-words", type=int, default=25)
    ap.add_argument("-o", "--output")
    ap.add_argument("--poll", type=float, default=0.4)
    a = ap.parse_args()

    job = ManualJob(a.input, a.max_words, a.min_words, progress_dir=os.path.join(HERE, ".progress"))
    if not job.units:
        sys.exit("No paragraphs long enough to rewrite (scanned PDF? run OCR first).")
    job.i = job.first_todo()
    print(f"{len(job.units)} chunks, {job.done} already done. Open https://quillbot.com/ai-humanizer")
    print("Paste (Ctrl+V) into QuillBot → Humanize → copy the output. Keys: [b]ack [s]kip [r]e-copy [q]uit+build")
    last = put(job)

    while job.done < len(job.units):
        k = key_pressed()
        if k == "q":
            break
        if k in ("b", "s", "r"):
            if k == "b":
                job.i = max(0, job.i - 1)
            elif k == "s":
                job.i = min(len(job.units) - 1, job.i + 1)
            job.persist()
            last = put(job)
            continue
        try:
            clip = pyperclip.paste()
        except pyperclip.PyperclipException:
            time.sleep(a.poll)
            continue
        if clip != last and looks_like_rewrite(job.units[job.i]["text"], clip):
            job.set_output(job.i, clip)
            miss = job.check(job.i)
            if miss:
                beep(False)
                print(f"   ⚠️  saved, but it dropped: {', '.join(miss[:8])}  (fix later in the app, or press [b] to redo)")
            else:
                beep(True)
                print("   ✅ saved")
            if job.done == len(job.units):
                break
            job.i = job.first_todo() if job.i + 1 >= len(job.units) or job.units[job.i + 1]["out"] else job.i + 1
            job.persist()
            last = put(job)
        elif clip != last:
            last = clip  # unrelated copy: ignore it
        time.sleep(a.poll)

    root, ext = os.path.splitext(a.input)
    out = a.output or f"{root}_humanized{ext}"
    stats = job.build(out)
    print(f"\nBuilt {out}\n{stats['chunks_humanized']}/{stats['chunks_total']} chunks humanized.")
    if stats.get("chunks_with_missing_facts"):
        print("Check chunks:", ", ".join(map(str, stats["chunks_with_missing_facts"])))


if __name__ == "__main__":
    main()
