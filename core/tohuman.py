"""toHuman (tohuman.io) API client with multi-key rotation.

    POST https://tohuman.io/api/v1/humanizations
    Authorization: Bearer <token>
    {"content": "...", "intensity": "minimal" | "medium" | "heavy"}

Several API tokens can be given. Each token handles at most `words_per_key` words (default 2400);
once a token's budget is full, the next token takes over, and so on. A token that the API rejects
(bad token, or its own word limit reached) is skipped automatically. Usage per token is saved in
`.tohuman_key_usage.json`, so a later run picks up with the tokens that still have words left.

The response may contain the result directly, or a job id to poll; both are handled.
Every result is cached by (text, intensity): re-running a document never spends words twice,
and a run stopped by the word limit continues later from where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

import requests

from .zerogpt import ZeroGPTError, _find_key, _get_path, guess_rewrite

log = logging.getLogger(__name__)

RESULT_KEYS = ["humanized_content", "humanized_text", "humanized", "output", "result", "output_text",
               "rewritten", "text"]
DONE = {"completed", "complete", "done", "succeeded", "success", "finished"}
FAILED = {"failed", "error", "errored", "cancelled", "canceled"}
DEFAULT_WORDS_PER_KEY = 2400


class KeyRejected(ZeroGPTError):
    """This token can't be used any more (invalid, or its own word limit is reached)."""

    def __init__(self, msg: str, status: str):
        super().__init__(msg)
        self.status = status  # "invalid" | "exhausted"


def parse_tokens(text) -> list[str]:
    """Tokens separated by newlines, commas, semicolons or spaces. Lines starting with # are ignored."""
    if isinstance(text, (list, tuple)):
        text = "\n".join(text)
    lines = [ln for ln in (text or "").splitlines() if not ln.strip().startswith("#")]
    seen, out = set(), []
    for t in re.split(r"[\s,;]+", "\n".join(lines)):
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _kid(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _words(text: str) -> int:
    return len(text.split())


class KeyPool:
    """Hands out tokens so that each one is used for at most `budget` words, in the order given."""

    def __init__(self, tokens, budget: int = DEFAULT_WORDS_PER_KEY, usage_file: str = ".tohuman_key_usage.json"):
        self.tokens = parse_tokens(tokens)
        if not self.tokens:
            raise ZeroGPTError("No toHuman API token. Paste at least one token (one per line) or set TOHUMAN_API_KEYS.")
        self.budget = max(1, int(budget or DEFAULT_WORDS_PER_KEY))
        self.usage_file = usage_file
        self.lock = threading.Lock()
        try:
            with open(usage_file, encoding="utf-8") as f:
                self.usage = json.load(f)
        except (OSError, ValueError):
            self.usage = {}
        self.run_words = {_kid(t): 0 for t in self.tokens}  # words sent in this run, per token

    # ---------------------------------------------------------------- state
    def _st(self, token: str) -> dict:
        return self.usage.setdefault(_kid(token), {"used": 0, "status": "ok", "note": ""})

    def _save(self):
        tmp = self.usage_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.usage, f, indent=1)
        os.replace(tmp, self.usage_file)

    def label(self, token: str) -> str:
        return f"#{self.tokens.index(token) + 1}  …{token[-4:]}"

    # ---------------------------------------------------------------- allocation
    def acquire(self, n: int) -> str:
        """Reserve n words on the first token that still has room for them."""
        with self.lock:
            for t in self.tokens:
                st = self._st(t)
                if st["status"] != "ok":
                    continue
                # a single block bigger than the whole budget goes to an unused token on its own
                if st["used"] + n <= self.budget or (st["used"] == 0 and n > self.budget):
                    st["used"] += n
                    self.run_words[_kid(t)] += n
                    self._save()
                    return t
            left = sum(max(0, self.budget - self._st(t)["used"]) for t in self.tokens if self._st(t)["status"] == "ok")
            raise ZeroGPTError(
                f"toHuman word limit reached on all {len(self.tokens)} key(s) ({self.budget:,} words per key; "
                f"{left:,} words left in total, not enough for the next {n}-word paragraph). "
                "Add more keys, or reset the counters when your allowance renews.")

    def release(self, token: str, n: int):
        """The request failed without being charged: give the words back."""
        with self.lock:
            st = self._st(token)
            st["used"] = max(0, st["used"] - n)
            self.run_words[_kid(token)] = max(0, self.run_words[_kid(token)] - n)
            self._save()

    def mark(self, token: str, status: str, note: str = ""):
        with self.lock:
            st = self._st(token)
            st["status"], st["note"] = status, note[:160]
            self._save()
        log.warning("toHuman key %s marked %s: %s", self.label(token), status, note[:160])

    def add_used(self, token: str, n: int):
        with self.lock:
            self._st(token)["used"] += n
            self.run_words[_kid(token)] += n
            self._save()

    def reset(self):
        """Forget usage and errors for the listed tokens (e.g. when the monthly allowance renews)."""
        with self.lock:
            for t in self.tokens:
                self.usage.pop(_kid(t), None)
            self._save()

    # ---------------------------------------------------------------- reporting
    def rows(self) -> list[list]:
        out = []
        for t in self.tokens:
            st = self._st(t)
            left = max(0, self.budget - st["used"]) if st["status"] == "ok" else 0
            status = {"ok": "active" if left else "full", "exhausted": "out of words",
                      "invalid": "rejected"}.get(st["status"], st["status"])
            out.append([self.label(t), st["used"], self.budget, left, self.run_words[_kid(t)],
                        status + (f" ({st['note']})" if st.get("note") else "")])
        return out

    def capacity(self) -> int:
        return sum(r[3] for r in self.rows())


class ToHumanClient:
    def __init__(self, tokens, cfg: dict, cache_file: str = ".tohuman_cache.json",
                 usage_file: str = ".tohuman_key_usage.json", words_per_key: int | None = None):
        self.cfg = cfg
        self.url = cfg.get("url", "https://tohuman.io/api/v1/humanizations").rstrip("/")
        self.intensity = cfg.get("intensity", "medium")
        self.timeout = cfg.get("timeout", 120)
        self.pool = tokens if isinstance(tokens, KeyPool) else KeyPool(
            tokens, words_per_key or cfg.get("words_per_key", DEFAULT_WORDS_PER_KEY), usage_file)
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.calls = {"humanize": 0, "detect": 0}
        self.words_used = 0
        self.lock = threading.Lock()
        self.cache_file = cache_file
        try:
            with open(cache_file, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    # ------------------------------------------------------------------ http
    def _check(self, r: requests.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code in (401, 403):
            raise KeyRejected(f"token rejected (HTTP {r.status_code}): {_msg(data)}", "invalid")
        if r.status_code in (402,) or (r.status_code in (400, 422, 429) and _is_quota(data)):
            raise KeyRejected(f"word limit reached: {_msg(data)}", "exhausted")
        if r.status_code >= 400:
            raise ZeroGPTError(f"toHuman error (HTTP {r.status_code}): {_msg(data)}")
        return data

    def _request(self, method: str, url: str, token: str, **kw) -> dict:
        last = None
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(4):
            try:
                r = self.s.request(method, url, timeout=self.timeout, headers=headers, **kw)
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 500 or (r.status_code == 429 and not _is_quota(_safe_json(r))):
                last = f"HTTP {r.status_code}"
                time.sleep(int(r.headers.get("Retry-After", 0) or 0) or 5 * (attempt + 1))
                continue
            return self._check(r)
        raise ZeroGPTError(f"toHuman unreachable after retries: {last}")

    def _extract(self, data: dict, source: str) -> str | None:
        path = self.cfg.get("result_path")
        out = _get_path(data, path) if path else None
        if isinstance(out, str) and out.strip():
            return out
        out = _find_key(data, RESULT_KEYS)
        if isinstance(out, str) and out.strip() and out.strip() != source.strip():
            return out
        return guess_rewrite(data, source)

    def _poll(self, data: dict, source: str, token: str) -> str:
        job = _find_key(data, ["id", "uuid", "humanization_id"])
        if job is None:
            raise ZeroGPTError("toHuman returned no text and no job id. Raw: " + json.dumps(data)[:300])
        url = f"{self.url}/{job}"
        deadline = time.time() + self.cfg.get("poll_timeout", 180)
        delay = 1.5
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay * 1.5, 8)
            d = self._request("GET", url, token)  # a job can only be read with the token that created it
            status = str(_find_key(d, ["status", "state"]) or "").lower()
            if status in FAILED:
                raise ZeroGPTError(f"toHuman job failed: {_msg(d)}")
            out = self._extract(d, source)
            if out and (status in DONE or not status):
                return out
        raise ZeroGPTError("toHuman job timed out")

    def _humanize_with(self, token: str, text: str) -> str:
        data = self._request("POST", self.url, token, json={"content": text, "intensity": self.intensity})
        status = str(_find_key(data, ["status", "state"]) or "").lower()
        out = None if status and status not in DONE else self._extract(data, text)
        return out or self._poll(data, text, token)

    # ------------------------------------------------------------------ API
    def humanize(self, text: str, salt: str = "") -> str:
        k = "t:" + hashlib.sha1(f"{self.intensity}\x00{text}".encode("utf-8")).hexdigest()
        with self.lock:
            if k in self.cache:
                return self.cache[k]
        n = _words(text)
        while True:
            token = self.pool.acquire(n)  # raises a "word limit" error when every key is full
            try:
                out = self._humanize_with(token, text).strip()
                break
            except KeyRejected as e:
                self.pool.release(token, n)
                self.pool.mark(token, e.status, str(e))  # skip this key from now on, try the next one
            except ZeroGPTError:
                self.pool.release(token, n)
                raise
        with self.lock:
            self.calls["humanize"] += 1
            self.words_used += n
            self.cache[k] = out
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f)
            os.replace(tmp, self.cache_file)
        return out

    def detect(self, text: str) -> dict:
        raise ZeroGPTError("toHuman has no detector; turn verification off")

    def test(self) -> str:
        """Humanize one short sentence (~25 words) with EVERY key and show the result per key."""
        sample = ("Remote sensing has emerged as a pivotal tool for environmental monitoring, enabling "
                  "researchers to analyze large-scale land cover dynamics with unprecedented precision.")
        parts = []
        for t in self.pool.tokens:
            label = self.pool.label(t)
            try:
                r = self.s.post(self.url, json={"content": sample, "intensity": self.intensity},
                                headers={"Authorization": f"Bearer {t}"}, timeout=self.timeout)
            except requests.RequestException as e:
                parts.append(f"### Key {label}  →  request failed\n{e}")
                continue
            data = _safe_json(r)
            body = json.dumps(data, indent=2)[:1500] if data else r.text[:1500]
            if r.status_code in (401, 403):
                self.pool.mark(t, "invalid", f"HTTP {r.status_code}")
            elif r.status_code == 402 or (r.status_code in (400, 422, 429) and _is_quota(data)):
                self.pool.mark(t, "exhausted", _msg(data))
            elif r.status_code < 400:
                self.pool.add_used(t, _words(sample))
                if self.pool._st(t)["status"] != "ok":
                    self.pool.mark(t, "ok", "")
            parts.append(f"### Key {label}  →  HTTP {r.status_code}\n```json\n{body}\n```")
        return f"{self.url}\n\n" + "\n\n".join(parts)


def _safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _msg(d) -> str:
    m = _find_key(d, ["message", "error", "detail", "errors"]) if isinstance(d, dict) else None
    return str(m or d)[:300]


def _is_quota(d) -> bool:
    m = json.dumps(d).lower() if d else ""
    return any(k in m for k in ("limit", "quota", "words left", "exceeded", "upgrade", "insufficient"))
