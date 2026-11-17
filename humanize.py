"""Command-line version:  python humanize.py paper.pdf  [--target 10] [--passes 2] [--attempts 3] [--no-verify]"""
import argparse
import json
import logging
import os
import sys

from core.config import load
from core.engine import Engine, Settings
from core.zerogpt import ZeroGPTClient, ZeroGPTError


def main():
    run = load()["run"]
    ap = argparse.ArgumentParser(description="Humanize a PDF/DOCX/TXT/MD with the ZeroGPT API (same format out).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--target", type=float, default=run["target"] * 100, help="max %% AI (default %(default)s)")
    ap.add_argument("--attempts", type=int, default=run["max_attempts"])
    ap.add_argument("--passes", type=int, default=run["passes"])
    ap.add_argument("--no-verify", action="store_true", help="humanize only, skip ZeroGPT detector checks")
    ap.add_argument("--test", action="store_true", help="only show raw API responses for a sample")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        client = ZeroGPTClient(os.environ.get("ZEROGPT_API_KEY", ""), load()["api"],
                               cache_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".zerogpt_cache.json"))
        if a.test:
            print(client.test())
            return
        s = Settings(target=a.target / 100, max_attempts=a.attempts, passes=a.passes, verify=not a.no_verify,
                     skip_human=run["skip_human"], min_words=run["min_words"], workers=run["workers"])
        root, ext = os.path.splitext(a.input)
        out, summary, _ = Engine(client, s).run(a.input, a.output, report_path=f"{root}_report.json")
    except ZeroGPTError as e:
        sys.exit(f"Error: {e}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
