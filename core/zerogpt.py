"""ZeroGPT API client: humanize (rewrite) + detect (score).

Endpoints, payload shape and response fields are configurable in config.yaml because they
come from ZeroGPT's API docs (zerogpt.com -> API). Use the app's "Test API" button to see
the raw responses and confirm everything is wired correctly before spending credits on a
whole document.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time

import requests

log = logging.getLogger(__name__)


class ZeroGPTError(RuntimeError):
    pass


def _fill(template, text: str):
    """Replace every "{text}" string value in a JSON template with the text."""
    if isinstance(template, dict):
        return {k: _fill(v, text) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, text) for v in template]
    if template == "{text}":
        return text
    return template


def _get_path(data, path: str):
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _find_key(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        for v in d.values():
            r = _find_key(v, keys)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find_key(v, keys)
            if r is not None:
                return r
    return None


def _strings(d, out):
    if isinstance(d, str):
        out.append(d)
    elif isinstance(d, dict):
        for v in d.values():
            _strings(v, out)
    elif isinstance(d, list):
        for v in d:
            _strings(v, out)
    return out


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def guess_rewrite(data, source: str) -> str | None:
    """Fallback when no result path is configured: the response string that looks most like a
    rewrite of the source (long enough, overlapping vocabulary, not identical)."""
    src_w, n = _words(source), max(1, len(source.split()))
    best, best_score, identical = None, 0.0, None
    for s in _strings(data, []):
        if s.strip() == source.strip():
            identical = s
            continue
        if len(s.split()) < 0.4 * n:
            continue
        w = _words(s)
        overlap = len(w & src_w) / max(1, len(src_w))
        if overlap > best_score:
            best, best_score = s, overlap
    if best_score >= 0.25:
        return best
    return identical  # the humanizer returned the text unchanged


class ZeroGPTClient:
    def __init__(self, api_key: str, cfg: dict, cache_file: str = ".zerogpt_cache.json"):
        if not api_key:
            raise ZeroGPTError("No ZeroGPT API key. Paste it in the app or set ZEROGPT_API_KEY.")
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({cfg.get("auth_header", "ApiKey"): api_key,
                                     "Content-Type": "application/json"})
        self.timeout = cfg.get("timeout", 120)
        self.calls = {"humanize": 0, "detect": 0}
        self.lock = threading.Lock()
        self.cache_file = cache_file
        try:
            with open(cache_file, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    # ---------------------------------------------------------------- plumbing
    def _key(self, kind: str, text: str, salt: str = "") -> str:
        return kind + ":" + hashlib.sha1((salt + "\x00" + text).encode("utf-8")).hexdigest()

    def _save_cache(self):
        tmp = self.cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f)
        os.replace(tmp, self.cache_file)

    def post(self, url: str, payload: dict) -> dict:
        last = None
        for attempt in range(4):
            try:
                r = self.session.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(4 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                raise ZeroGPTError(f"HTTP {r.status_code}, not JSON: {r.text[:300]}") from None
            if r.status_code >= 400 or data.get("success") is False:
                msg = data.get("message") or data.get("error") or r.text[:300]
                raise ZeroGPTError(f"ZeroGPT error (HTTP {r.status_code}): {msg}")
            return data
        raise ZeroGPTError(f"ZeroGPT unreachable after retries: {last}")

    # ---------------------------------------------------------------- API
    def humanize(self, text: str, salt: str = "") -> str:
        """salt lets you ask again for a *different* rewrite of the same text (retries)."""
        k = self._key("h", text, salt + json.dumps(self.cfg["humanize_payload"], sort_keys=True))
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        payload = _fill(copy.deepcopy(self.cfg["humanize_payload"]), text)
        data = self.post(self.cfg["humanize_url"], payload)
        path = self.cfg.get("humanize_result_path")
        out = _get_path(data, path) if path else None
        if not isinstance(out, str) or not out.strip():
            out = guess_rewrite(data, text)
        if not out:
            raise ZeroGPTError("Could not find the humanized text in ZeroGPT's response. Use 'Test API' "
                               "and set humanize_result_path in config.yaml. Raw: " + json.dumps(data)[:400])
        out = out.strip()
        with self.lock:
            self.calls["humanize"] += 1
            self.cache[k] = out
            self._save_cache()
        return out

    def detect(self, text: str) -> dict:
        """{'ai': 0..1, 'flagged': [sentences]}"""
        k = self._key("d", text)
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        payload = _fill(copy.deepcopy(self.cfg["detect_payload"]), text)
        data = self.post(self.cfg["detect_url"], payload)
        pct = _get_path(data, self.cfg["detect_score_path"]) if self.cfg.get("detect_score_path") else None
        if pct is None:
            pct = _find_key(data, ["fakePercentage", "fake_percentage", "aiPercentage", "ai_percentage"])
        if pct is None:
            raise ZeroGPTError("No AI percentage in detector response. Raw: " + json.dumps(data)[:400])
        flagged = _find_key(data, ["h", "highlighted_sentences"]) or []
        res = {"ai": float(pct) / 100.0 if float(pct) > 1 else float(pct),
               "flagged": [s for s in flagged if isinstance(s, str)]}
        with self.lock:
            self.calls["detect"] += 1
            self.cache[k] = res
            self._save_cache()
        return res

    def test(self) -> str:
        """Raw responses for a short sample, for wiring/debugging. Not cached."""
        sample = ("Artificial intelligence has emerged as a transformative force across numerous industries. "
                  "Furthermore, it enables organizations to streamline operations and enhance decision-making "
                  "processes, fostering innovation and driving sustainable growth in an ever-evolving landscape.")
        lines = []
        for name, url, pl in (("DETECT", self.cfg["detect_url"], self.cfg["detect_payload"]),
                              ("HUMANIZE", self.cfg["humanize_url"], self.cfg["humanize_payload"])):
            try:
                r = self.session.post(url, json=_fill(copy.deepcopy(pl), sample), timeout=self.timeout)
                body = r.text
                try:
                    body = json.dumps(r.json(), indent=2)[:3000]
                except ValueError:
                    pass
                lines.append(f"### {name}  →  HTTP {r.status_code}\n{url}\n```json\n{body}\n```")
            except requests.RequestException as e:
                lines.append(f"### {name}  →  request failed\n{url}\n{e}")
        return "\n\n".join(lines)
