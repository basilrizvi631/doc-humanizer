"""ZeroGPT Document Humanizer — web UI.

    python app.py      ->  opens http://127.0.0.1:7860
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile

import gradio as gr

from core.config import load
from core.engine import Engine, Settings
from core.manual import ManualJob
from core.tohuman import DEFAULT_WORDS_PER_KEY, KeyPool, ToHumanClient, parse_tokens
from core.zerogpt import ZeroGPTClient, ZeroGPTError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "urllib3", "gradio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

CFG = load()
API, RUN = CFG["api"], CFG["run"]
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".zerogpt_cache.json")


def _api_cfg(h_url, h_payload, h_path, d_url, d_payload, header):
    try:
        hp, dp = json.loads(h_payload), json.loads(d_payload)
    except json.JSONDecodeError as e:
        raise gr.Error(f"Payload is not valid JSON: {e}") from e
    return {**API, "humanize_url": h_url.strip(), "humanize_payload": hp, "humanize_result_path": h_path.strip(),
            "detect_url": d_url.strip(), "detect_payload": dp, "auth_header": header.strip() or "ApiKey"}


def _client(key, *adv):
    key = (key or "").strip() or os.environ.get("ZEROGPT_API_KEY", "")
    if not key:
        raise gr.Error("Paste your ZeroGPT API key first (Dashboard → API → API Key).")
    return ZeroGPTClient(key, _api_cfg(*adv), cache_file=CACHE)


def _path(f):
    if f is None:
        raise gr.Error("Upload a document first.")
    return f if isinstance(f, str) else f.name


def _pct(x):
    return "" if x is None else f"{x * 100:.0f}%"


# --------------------------------------------------------------------------- actions
def test_api(key, *adv):
    try:
        return _client(key, *adv).test()
    except ZeroGPTError as e:
        raise gr.Error(str(e)) from e


def analyze(file, min_words):
    path = _path(file)
    eng = Engine(None, Settings(min_words=int(min_words)))
    try:
        _, segs, todo = eng.analyze(path)
    except Exception as e:  # noqa: BLE001
        raise gr.Error(f"Could not read the document: {e}") from e
    n_words = sum(len(s.text.split()) for s in todo)
    rows = [[s.meta.get("page", 0) + 1 if s.meta.get("page") is not None else "", len(s.text.split()),
             s.text[:140] + ("…" if len(s.text) > 140 else "")] for s in todo]
    md = (f"### {len(todo)} paragraphs will be humanized ({n_words:,} words)\n"
          f"{len(segs) - len(todo)} other blocks (title, headings, bullets, tables, equations, references, "
          f"captions) stay exactly as they are.\n\n"
          f"**Credits:** about {n_words:,} words humanized per attempt. With verification on, each rewrite is "
          f"also scored, so a full run usually costs 2–4× the word count.")
    if not todo:
        md = ("### No paragraphs found to rewrite\nIf this is a scanned PDF, run OCR first "
              "(e.g. `ocrmypdf in.pdf out.pdf`). Otherwise lower *Min words per paragraph*.")
    return md, gr.update(value=rows, headers=["Page", "Words", "Paragraph"])


def run(file, key, target, attempts, passes, verify, skip_human, min_words, workers,
        h_url, h_payload, h_path, d_url, d_payload, header, progress=gr.Progress()):
    path = _path(file)
    client = _client(key, h_url, h_payload, h_path, d_url, d_payload, header)
    settings = Settings(target=target / 100, max_attempts=int(attempts), passes=int(passes),
                        verify=bool(verify), skip_human=bool(skip_human), min_words=int(min_words),
                        workers=int(workers))
    work = tempfile.mkdtemp(prefix="zgh_")
    name, ext = os.path.splitext(os.path.basename(path))
    src = os.path.join(work, name + ext)
    shutil.copy(path, src)
    out = os.path.join(work, f"{name}_humanized{ext}")
    rep = os.path.join(work, f"{name}_report.json")
    try:
        out, summary, rows = Engine(client, settings).run(
            src, out, progress=lambda f, m: progress(f, desc=m), report_path=rep)
    except ZeroGPTError as e:
        raise gr.Error(str(e)) from e

    b, a = summary.get("doc_ai_before"), summary.get("doc_ai_after")
    lines = [f"## Done: {summary['rewritten']} of {summary['paragraphs']} paragraphs humanized"]
    if b is not None or a is not None:
        lines.append(f"**ZeroGPT, whole document:** {_pct(b)} AI before → **{_pct(a)} AI after**")
    for p in summary["passes"]:
        if p.get("doc_ai") is not None:
            lines.append(f"- pass {p['pass']}: {_pct(p['doc_ai'])} AI")
    if summary["needs_proofread"]:
        lines.append(f"\n⚠️ **{len(summary['needs_proofread'])} paragraph(s) may have lost a number, "
                     f"citation or acronym**; they're marked *check* in the table. Proofread those.")
    shrunk = (summary.get("save") or {}).get("shrunk") or []
    if shrunk:
        lines.append(f"\nℹ️ {len(shrunk)} paragraph(s) were set slightly smaller to fit their original space.")
    lines.append(f"\nAPI calls: {summary['api_calls']['humanize']} humanize, {summary['api_calls']['detect']} detect "
                 "(repeat texts are cached and free).")
    table = [[r.page + 1 if r.page is not None else "", _pct(r.ai_before), _pct(r.ai_after), r.attempts,
              r.note or ("rewritten" if r.changed else "unchanged"),
              r.original[:110] + "…", r.final[:110] + "…"] for r in rows]
    return ("\n".join(lines), out, rep,
            gr.update(value=table, headers=["Page", "AI before", "AI after", "Tries", "Status", "Original", "Humanized"]))


# --------------------------------------------------------------------------- manual (QuillBot) mode
PROGRESS = os.path.join(HERE, ".progress")


def _tb(**kw):
    try:
        return gr.Textbox(buttons=["copy"], **kw)      # Gradio 6
    except TypeError:
        return gr.Textbox(show_copy_button=True, **kw)  # Gradio 4/5


def _m_view(job):
    n = len(job.units)
    u = job.units[job.i]
    page = f"page {u['page'] + 1} · " if u.get("page") is not None else ""
    head = (f"### Chunk {job.i + 1} of {n}\n{page}{len(u['text'].split())} words · "
            f"**{job.done}/{n} done** {'✅' if job.done == n else ''}")
    miss = job.check(job.i)
    if miss:
        note = "⚠️ Your pasted text is missing: **" + ", ".join(miss[:8]) + "**. Fix it before moving on."
    elif u["out"].strip():
        note = "✅ Saved."
    else:
        note = "Copy the text on the left, humanize it in QuillBot, paste the result on the right."
    return head, u["text"], u["out"], note


def m_start(file, max_words, min_words):
    path = _path(file)
    try:
        job = ManualJob(path, int(max_words), int(min_words), progress_dir=PROGRESS)
    except Exception as e:  # noqa: BLE001
        raise gr.Error(f"Could not read the document: {e}") from e
    if not job.units:
        raise gr.Error("No paragraphs long enough to rewrite (scanned PDF? run OCR first, or lower Min words).")
    resumed = job.done > 0
    job.i = job.first_todo() if resumed else 0
    msg = (f"Resumed: {job.done} of {len(job.units)} chunks already done." if resumed
           else f"{len(job.units)} chunks to humanize.")
    gr.Info(msg)
    return (job, *_m_view(job))


def _need(job):
    if job is None:
        raise gr.Error("Upload a document and click Start first.")


def m_move(job, pasted, delta):
    _need(job)
    if (pasted or "").strip() != job.units[job.i]["out"]:
        job.set_output(job.i, pasted)
    job.i = max(0, min(len(job.units) - 1, job.i + delta))
    job.persist()
    return (job, *_m_view(job))


def m_next_todo(job, pasted):
    _need(job)
    if (pasted or "").strip() != job.units[job.i]["out"]:
        job.set_output(job.i, pasted)
    job.i = job.first_todo()
    job.persist()
    return (job, *_m_view(job))


def m_build(job, pasted):
    _need(job)
    if (pasted or "").strip() != job.units[job.i]["out"]:
        job.set_output(job.i, pasted)
    work = tempfile.mkdtemp(prefix="qb_")
    name, ext = os.path.splitext(os.path.basename(job.path))
    out = os.path.join(work, f"{name}_humanized{ext}")
    stats = job.build(out)
    lines = [f"## Built: {stats['chunks_humanized']} of {stats['chunks_total']} chunks humanized"]
    if stats["chunks_humanized"] < stats["chunks_total"]:
        lines.append("Chunks you skipped keep their original text.")
    if stats.get("chunks_with_missing_facts"):
        lines.append("⚠️ Check chunks " + ", ".join(map(str, stats["chunks_with_missing_facts"]))
                     + ": a number, citation or acronym went missing.")
    if stats.get("shrunk"):
        lines.append(f"ℹ️ {len(stats['shrunk'])} paragraph(s) were set slightly smaller to fit their original space.")
    lines.append("Paste the finished text into ZeroGPT / QuillBot's AI Detector to check it.")
    return "\n\n".join(lines), out


# --------------------------------------------------------------------------- toHuman API mode
TH_CACHE = os.path.join(HERE, ".tohuman_cache.json")
TH_USAGE = os.path.join(HERE, ".tohuman_key_usage.json")
TH_KEYS_FILE = os.path.join(HERE, "tohuman_keys.txt")
TH = CFG.get("tohuman", {})
KEY_HEADERS = ["Key", "Used", "Limit", "Left", "This run", "Status"]


def _th_default_tokens() -> str:
    """Prefill: TOHUMAN_API_KEYS / TOHUMAN_API_KEY env vars, then tohuman_keys.txt next to app.py."""
    env = os.environ.get("TOHUMAN_API_KEYS") or os.environ.get("TOHUMAN_API_KEY") or ""
    if env.strip():
        return "\n".join(parse_tokens(env))
    try:
        with open(TH_KEYS_FILE, encoding="utf-8") as f:
            return "\n".join(parse_tokens(f.read()))
    except OSError:
        return ""


def _th_pool(tokens, budget, required=True):
    if not parse_tokens(tokens):
        if required:
            raise gr.Error("Paste at least one toHuman API token (one per line).")
        return None
    return KeyPool(tokens, int(budget or DEFAULT_WORDS_PER_KEY), TH_USAGE)


def _keys_table(pool):
    return gr.update(value=pool.rows() if pool else [], headers=KEY_HEADERS)


def th_keys(tokens, budget):
    return _keys_table(_th_pool(tokens, budget, required=False))


def th_reset(tokens, budget):
    pool = _th_pool(tokens, budget)
    pool.reset()
    gr.Info(f"Counters reset for {len(pool.tokens)} key(s).")
    return _keys_table(_th_pool(tokens, budget))


def _th_client(tokens, intensity, budget):
    pool = _th_pool(tokens, budget)
    return ToHumanClient(pool, {**TH, "intensity": intensity}, cache_file=TH_CACHE)


def th_test(tokens, intensity, budget):
    client = _th_client(tokens, intensity, budget)
    return client.test(), _keys_table(client.pool)


def th_analyze(file, min_words, tokens, budget):
    md, tbl = analyze(file, min_words)
    md = md.split("**Credits:**")[0] + ("**toHuman words:** each paragraph is sent once, so this run uses about "
                                         "the word count above. Paragraphs already humanized in an earlier run "
                                         "are cached and cost nothing.")
    pool = _th_pool(tokens, budget, required=False)
    if pool and file is not None:
        need = sum(r[1] for r in (tbl.get("value") or []) if isinstance(r[1], int))
        cap = pool.capacity()
        active = sum(1 for r in pool.rows() if r[3] > 0)
        md += (f"\n\n**Keys:** {len(pool.tokens)} key(s), {pool.budget:,} words each → **{cap:,} words left** "
               f"across {active} usable key(s). This document needs about **{need:,}** "
               f"(≈ {-(-need // pool.budget)} key(s)).")
        if need > cap:
            md += ("\n\n⚠️ Not enough words left on your keys for the whole document. It will stop when the keys "
                   "run out and give you the partial file; add more keys to finish it.")
    return md, tbl, _keys_table(pool)


def th_run(file, tokens, intensity, budget, min_words, workers, progress=gr.Progress()):
    path = _path(file)
    client = _th_client(tokens, intensity, budget)
    settings = Settings(verify=False, skip_human=False, max_attempts=1, passes=1,
                        min_words=int(min_words), workers=int(workers))
    work = tempfile.mkdtemp(prefix="th_")
    name, ext = os.path.splitext(os.path.basename(path))
    src = os.path.join(work, name + ext)
    shutil.copy(path, src)
    out = os.path.join(work, f"{name}_humanized{ext}")
    rep = os.path.join(work, f"{name}_report.json")
    try:
        out, summary, rows = Engine(client, settings).run(
            src, out, progress=lambda f, m: progress(f, desc=m), report_path=rep)
    except ZeroGPTError as e:
        raise gr.Error(str(e)) from e
    lines = [f"## {summary['rewritten']} of {summary['paragraphs']} paragraphs humanized"]
    if summary.get("stopped"):
        lines.append(f"⛔ **Stopped early:** {summary['stopped']}\n\nThe file below contains everything finished so "
                     "far (the rest keeps its original text). Add more keys (or reset the counters when your "
                     "allowance renews), upload the **same file** and click Humanize: finished paragraphs come from "
                     "the cache at no cost and it continues with the rest.")
    used = [r for r in client.pool.rows() if r[4]]
    lines.append(f"Words sent this run: **{client.words_used:,}** across **{len(used)}** key(s) "
                 "(cached paragraphs cost nothing).")
    if summary["needs_proofread"]:
        lines.append(f"⚠️ {len(summary['needs_proofread'])} paragraph(s) may have lost a number, citation or "
                     "acronym (marked *check*). Proofread those.")
    shrunk = (summary.get("save") or {}).get("shrunk") or []
    if shrunk:
        lines.append(f"ℹ️ {len(shrunk)} paragraph(s) were set slightly smaller to fit their original space.")
    lines.append("Check the result in ZeroGPT's free web checker; edit any highlighted sentences by hand.")
    table = [[r.page + 1 if r.page is not None else "", r.note or ("humanized" if r.changed else "not done"),
              r.original[:110] + "…", r.final[:110] + "…"] for r in rows]
    return ("\n\n".join(lines), out, rep,
            gr.update(value=table, headers=["Page", "Status", "Original", "Humanized"]),
            _keys_table(client.pool))


# --------------------------------------------------------------------------- layout
with gr.Blocks(title="Document Humanizer") as demo:
    gr.Markdown("# 📄 Document Humanizer\n"
                "AI-written **PDF, Word (.docx), TXT or Markdown** in → humanized file out, in **the same format "
                "and layout**. Titles, headings, tables, equations, bullet lists and references are never touched.")
    with gr.Tabs():
        with gr.Tab("toHuman API (automatic)"):
            gr.Markdown("Upload your document, paste your **toHuman API tokens** (one per line), click **Humanize**. "
                        "Each key is used for up to the word limit below, then the next key takes over. Each body "
                        "paragraph is humanized once and written back into the same file format and layout.")
            with gr.Row():
                with gr.Column(scale=1, min_width=320):
                    t_file = gr.File(label="1 · AI-written document", file_types=[".pdf", ".docx", ".txt", ".md"])
                    t_token = gr.Textbox(label="2 · toHuman API tokens (one per line, used top to bottom)", lines=5,
                                         value=_th_default_tokens(),
                                         placeholder="token-1\ntoken-2\ntoken-3")
                    t_budget = gr.Number(value=TH.get("words_per_key", DEFAULT_WORDS_PER_KEY), precision=0,
                                         minimum=1, label="Words per key before switching to the next key")
                    t_int = gr.Radio(["minimal", "medium", "heavy"], value=TH.get("intensity", "medium"),
                                     label="Intensity (heavy = most rewriting)")
                    with gr.Accordion("Advanced", open=False):
                        t_min = gr.Slider(5, 80, value=RUN["min_words"], step=1, label="Min words per paragraph")
                        t_workers = gr.Slider(1, 4, value=2, step=1, label="Parallel requests")
                    with gr.Row():
                        t_test = gr.Button("🔌 Test all keys (~25 words each)")
                        t_an = gr.Button("🔍 Analyze")
                    t_go = gr.Button("✨ Humanize document", variant="primary", size="lg")
                with gr.Column(scale=2):
                    t_status = gr.Markdown("Upload a file to see how many words it will use.")
                    with gr.Accordion("API keys: words used per key", open=True):
                        t_keys = gr.Dataframe(headers=KEY_HEADERS, value=th_keys(_th_default_tokens(),
                                              TH.get("words_per_key", DEFAULT_WORDS_PER_KEY))["value"],
                                              interactive=False, wrap=True)
                        t_reset = gr.Button("↺ Reset key counters (when your word allowance renews)", size="sm")
                    with gr.Row():
                        t_out = gr.File(label="Humanized document")
                        t_rep = gr.File(label="Report (JSON)")
                    t_table = gr.Dataframe(headers=["Page", "Words", "Paragraph"], wrap=True, interactive=False,
                                           label="Paragraphs")
                    with gr.Accordion("API test output", open=False) as t_acc:
                        t_api = gr.Markdown()
            t_token.blur(th_keys, [t_token, t_budget], t_keys)
            t_budget.change(th_keys, [t_token, t_budget], t_keys)
            t_reset.click(th_reset, [t_token, t_budget], t_keys)
            t_test.click(th_test, [t_token, t_int, t_budget], [t_api, t_keys]).then(
                lambda: gr.update(open=True), None, t_acc)
            t_an.click(th_analyze, [t_file, t_min, t_token, t_budget], [t_status, t_table, t_keys])
            t_file.upload(th_analyze, [t_file, t_min, t_token, t_budget], [t_status, t_table, t_keys])
            t_go.click(th_run, [t_file, t_token, t_int, t_budget, t_min, t_workers],
                       [t_status, t_out, t_rep, t_table, t_keys])

        with gr.Tab("QuillBot (free, manual paste)"):
            gr.Markdown("**How it works:** the document is split into chunks that fit QuillBot's free limit. "
                        "For each chunk: **copy** it → paste into "
                        "[QuillBot AI Humanizer](https://quillbot.com/ai-humanizer) (use the *Academic* tab for papers) "
                        "→ copy QuillBot's output → **paste it on the right** → **Save & next**. "
                        "Progress saves automatically, so you can close the app and continue later with the same file.")
            m_job = gr.State(None)
            with gr.Row():
                with gr.Column(scale=1, min_width=300):
                    m_file = gr.File(label="AI-written document", file_types=[".pdf", ".docx", ".txt", ".md"])
                    m_max = gr.Slider(40, 500, value=120, step=5, label="Max words per chunk (QuillBot free ≈ 125)")
                    m_min = gr.Slider(5, 80, value=RUN["min_words"], step=1, label="Min words per paragraph")
                    m_start_btn = gr.Button("▶ Start / Resume", variant="primary")
                    m_build_btn = gr.Button("📦 Build humanized document", variant="primary")
                    m_status = gr.Markdown()
                    m_out = gr.File(label="Humanized document")
                with gr.Column(scale=3):
                    m_head = gr.Markdown("Upload a file and click **Start**.")
                    with gr.Row():
                        m_orig = _tb(label="1 · Copy this into QuillBot", lines=12, interactive=False)
                        m_paste = gr.Textbox(label="2 · Paste QuillBot's output here", lines=12,
                                             placeholder="Paste the humanized text…")
                    m_note = gr.Markdown()
                    with gr.Row():
                        m_prev = gr.Button("◀ Previous")
                        m_todo = gr.Button("⏭ Next unfinished")
                        m_next = gr.Button("Save & next ▶", variant="primary")
            view = [m_job, m_head, m_orig, m_paste, m_note]
            m_start_btn.click(m_start, [m_file, m_max, m_min], view)
            m_next.click(lambda j, p: m_move(j, p, 1), [m_job, m_paste], view)
            m_prev.click(lambda j, p: m_move(j, p, -1), [m_job, m_paste], view)
            m_todo.click(m_next_todo, [m_job, m_paste], view)
            m_build_btn.click(m_build, [m_job, m_paste], [m_status, m_out])

        with gr.Tab("ZeroGPT API (automatic, paid)"):
            gr.Markdown("### ZeroGPT API mode\n"
                        "Upload an AI-written **PDF, Word (.docx), TXT or Markdown** file. Every body paragraph is "
                        "rewritten with ZeroGPT's humanizer and checked with ZeroGPT's detector, then written back "
                        "into **the same file format and layout**. Titles, headings, tables, equations, bullet lists "
                        "and references are left untouched.")
            with gr.Row():
                with gr.Column(scale=1, min_width=340):
                    file = gr.File(label="1 · AI-written document", file_types=[".pdf", ".docx", ".txt", ".md"])
                    key = gr.Textbox(label="2 · ZeroGPT API key", type="password",
                                     value=os.environ.get("ZEROGPT_API_KEY", ""),
                                     placeholder="Dashboard → API → Your API Key")
                    with gr.Group():
                        target = gr.Slider(0, 50, value=RUN["target"] * 100, step=1, label="Target: max % AI (ZeroGPT)")
                        attempts = gr.Slider(1, 5, value=RUN["max_attempts"], step=1, label="Humanize attempts per paragraph")
                        passes = gr.Slider(1, 4, value=RUN["passes"], step=1, label="Document passes")
                        verify = gr.Checkbox(value=RUN["verify"], label="Verify every rewrite with ZeroGPT detector")
                        skip_human = gr.Checkbox(value=RUN["skip_human"], label="Skip paragraphs that already score as human")
                    with gr.Accordion("Advanced", open=False):
                        min_words = gr.Slider(5, 80, value=RUN["min_words"], step=1, label="Min words per paragraph")
                        workers = gr.Slider(1, 8, value=RUN["workers"], step=1, label="Parallel requests")
                        gr.Markdown("**API wiring** (from your ZeroGPT API docs). Use *Test API* to check it.")
                        h_url = gr.Textbox(label="Humanize URL", value=API["humanize_url"])
                        h_payload = gr.Textbox(label="Humanize JSON body ({text} = paragraph)",
                                               value=json.dumps(API["humanize_payload"]))
                        h_path = gr.Textbox(label="Humanized-text field (e.g. data.message; empty = auto)",
                                            value=API.get("humanize_result_path") or "")
                        d_url = gr.Textbox(label="Detect URL", value=API["detect_url"])
                        d_payload = gr.Textbox(label="Detect JSON body", value=json.dumps(API["detect_payload"]))
                        header = gr.Textbox(label="Auth header name", value=API.get("auth_header", "ApiKey"))
                    with gr.Row():
                        test_btn = gr.Button("🔌 Test API")
                        analyze_btn = gr.Button("🔍 Analyze")
                    run_btn = gr.Button("✨ Humanize document", variant="primary", size="lg")
                with gr.Column(scale=2):
                    status = gr.Markdown("Upload a file, paste your API key, click **Test API** once, then **Humanize**.")
                    with gr.Row():
                        out_file = gr.File(label="Humanized document")
                        rep_file = gr.File(label="Report (JSON)")
                    table = gr.Dataframe(headers=["Page", "Words", "Paragraph"], wrap=True, interactive=False,
                                         label="Paragraphs")
                    with gr.Accordion("API test output", open=False) as api_acc:
                        api_out = gr.Markdown()

            adv = [h_url, h_payload, h_path, d_url, d_payload, header]
            test_btn.click(test_api, [key, *adv], api_out).then(lambda: gr.update(open=True), None, api_acc)
            analyze_btn.click(analyze, [file, min_words], [status, table])
            file.upload(analyze, [file, min_words], [status, table])
            run_btn.click(run, [file, key, target, attempts, passes, verify, skip_human, min_words, workers, *adv],
                          [status, out_file, rep_file, table])


if __name__ == "__main__":
    demo.queue().launch(inbrowser=True)
