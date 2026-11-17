# Document Humanizer

Three modes, one app:

* **toHuman API (automatic)**: paste your tohuman.io API token, upload, click Humanize. Each paragraph is humanized once, and every result is cached. If your monthly word limit runs out mid-document, the run stops cleanly and gives you the partial file. Re-run the same file later and it continues where it stopped, with finished paragraphs costing nothing.
  You can paste **several toHuman tokens, one per line**. Each key is used for up to *Words per key* words (default 2400, set in the app or `words_per_key` in `config.yaml`), then the next key takes over. Keys the API rejects (bad token or out of words) are skipped automatically. The *API keys* table shows each key's used / left words; usage is remembered in `.tohuman_key_usage.json`, so the next run starts on keys that still have words. Click **Reset key counters** when your allowance renews. To avoid pasting every time, put the tokens in `tohuman_keys.txt` (one per line) next to `app.py`, or set `setx TOHUMAN_API_KEYS "tok1,tok2,tok3"`.
* **QuillBot (free, manual paste)**: QuillBot has no public API, so the app does everything except the pasting. It splits your document into chunks that fit QuillBot's free limit and shows them one at a time with a Copy button. You paste each into quillbot.com/ai-humanizer and paste the result back, then the app rebuilds the document in its original format. Progress autosaves (`.progress/`), so you can stop and resume with the same file. Each paste is checked for lost numbers, citations and acronyms.
* **ZeroGPT API (automatic, paid)**: fully automatic, described below.


Upload an AI-written **PDF, Word (.docx), TXT or Markdown** file and get back the **same file type with the same layout**. Every body paragraph is rewritten by ZeroGPT's humanizer and checked by ZeroGPT's detector. The title, headings, tables, equations, bullet lists and references are left untouched.

## Setup (Windows, once)

1. Clone the repo (`git clone https://github.com/basilrizvi631/doc-humanizer.git`) or unzip it to a folder.
2. Double-click **setup.bat**. It creates a virtual environment and installs everything (no GPU or PyTorch needed).
3. Add credit to your ZeroGPT account (Dashboard → Credits). The API won't work with a $0.00 balance.

## Use

Double-click **run.bat**. The app opens in your browser at http://127.0.0.1:7860.

1. Upload your document. The app immediately shows which paragraphs it will rewrite and how many words that is.
2. Paste your **ZeroGPT API key** (Dashboard → API → Your API Key). You can also set it once with `setx ZEROGPT_API_KEY "..."`.
3. Click **🔌 Test API** once. Both sections should show `HTTP 200` (see "First run" below).
4. Click **✨ Humanize document**, then download the humanized file and the report.

## How it works

For each paragraph:

1. The original is scored with the ZeroGPT detector. Paragraphs already at or below the target are skipped, which saves credits.
2. The paragraph is humanized and the rewrite is scored. If it's still above the target, the best version so far is humanized again, up to *attempts* times.
3. The best rewrite is chosen by these rules, in order: it keeps every number, citation and acronym; it fits the original space; it has the lowest AI score.

After each pass the whole document is scored and saved. Paragraphs still above the target get another pass, until the document is under the target or the passes run out. A paragraph is never replaced by a version that scores worse. Results are cached in `.zerogpt_cache.json`, so re-running the same document never pays twice for the same text.

## First run: checking the API wiring

ZeroGPT's endpoints and field names are set in `config.yaml` (and under **Advanced** in the app). **Test API** shows the raw responses:

* **DETECT** should show a `fakePercentage` value.
* **HUMANIZE** should show your rewritten sample text in some field.

If HUMANIZE returns 404 or an error about a missing field, open the API docs in your ZeroGPT dashboard and copy the humanizer URL and request body into **Advanced** (or `config.yaml`). Use `{text}` where the text goes, e.g. `{"string": "{text}"}`. If the rewritten text is in a field like `data.message`, enter that path in *Humanized-text field*.

## Command line

```
.venv\Scripts\activate
python humanize.py --test
python humanize.py "C:\path\paper.pdf" --target 10 --passes 2
```

## Notes

* **Credits:** a full run costs roughly 2-4× the document's word count (rewrites plus detector checks). Turning off *Verify* is cheapest but gives no scores.
* Scanned PDFs need OCR first (`ocrmypdf in.pdf out.pdf`).
* On Windows, rewritten PDF text uses the matching installed font (Times New Roman, Calibri, Arial, …). Otherwise a close generic font is used.
* Check the report's *Status* column: any paragraph marked "check" may have lost a number or citation, so proofread it.

## Clipboard autopilot (fastest QuillBot workflow)

Instead of switching between the app and QuillBot for every chunk, run:

```
.venv\Scripts\activate
python autopilot.py "C:\path\paper.pdf"
```

The first chunk is put on your clipboard. Then repeat this for each chunk:

1. In QuillBot, press **Ctrl+A** and **Ctrl+V** in the input box, then click **Humanize**.
2. Click QuillBot's **copy** button on the output.

The autopilot sees the copied output, checks it kept every number, citation and acronym (it beeps on a problem), saves it, and puts the **next** chunk on your clipboard. Copying something unrelated by accident is ignored. Keys in the autopilot window: **b** back, **s** skip, **r** re-copy, **q** finish and build. When all chunks are done it builds `paper_humanized.pdf` next to the original. Progress is shared with the app's QuillBot tab, so you can stop and resume either way.
