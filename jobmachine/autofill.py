#!/usr/bin/env python3
"""autofill - Playwright pre-fill for Greenhouse/Lever/Ashby application forms.

SHADOW MODE IS LAW:
  default run  -> fill fields, attach resume, save a screenshot, DO NOT submit.
  --approved   -> submit, save confirmation screenshot. Only after Mitchell has
                  approved this exact job's packet (jobsctl prep <key>).

Requires: pip install playwright ; a Chromium (set PLAYWRIGHT_BROWSERS_PATH or
CHROMIUM_PATH). Runs wherever the network allows reaching the ATS form hosts.

Usage:
  python3 autofill.py <apply_url> --resume resumes/Master_B.pdf [--approved]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHOTS = ROOT / "data" / "screenshots"

FIELD_PATTERNS = {
    # answer-bank key path -> label regex (case-insensitive)
    ("identity", "legal_name"): r"^(full |legal )?name$|first and last",
    ("identity", "email"): r"e-?mail",
    ("identity", "phone"): r"phone",
    ("identity", "location"): r"location|city|current residence",
    ("identity", "linkedin"): r"linkedin",
    ("screening", "authorized_to_work_us"): r"authorized to work|work authorization|legally (?:able|entitled) to work",
    ("screening", "require_sponsorship"): r"sponsor",
    ("screening", "start_date"): r"start date|when (?:can|could) you start|available to start",
}
FIRST_LAST = {"first": r"first ?name", "last": r"last ?name"}


def load_bank() -> dict:
    p = ROOT / "data" / "private" / "answers.json"
    if not p.exists():
        sys.exit("answers.json missing - build the answer bank first (fail loud)")
    return json.loads(p.read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--resume", required=True, help="PDF to attach")
    ap.add_argument("--approved", action="store_true",
                    help="Mitchell approved this packet: actually submit")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    resume = Path(args.resume)
    if not resume.exists():
        sys.exit(f"resume not found: {resume} (fail loud)")
    bank = load_bank()
    from playwright.sync_api import sync_playwright  # import late: optional dep

    SHOTS.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^a-z0-9]+", "-", args.url.lower())[-60:]

    with sync_playwright() as pw:
        launch = {"headless": not args.headed}
        if os.environ.get("CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = pw.chromium.launch(**launch)
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        filled, skipped = [], []

        def fill_by_label(pattern: str, value: str) -> bool:
            try:
                loc = page.get_by_label(re.compile(pattern, re.I)).first
                tag_name = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "select":
                    loc.select_option(label=re.compile(re.escape(value), re.I))
                else:
                    loc.fill(value)
                return True
            except Exception:
                return False

        # name (single field, or first/last split)
        name = bank["identity"]["legal_name"]
        if not fill_by_label(FIELD_PATTERNS[("identity", "legal_name")], name):
            first, last = name.split(" ", 1)
            fill_by_label(FIRST_LAST["first"], first)
            fill_by_label(FIRST_LAST["last"], last)
        filled.append("name")

        for (section, key), pattern in FIELD_PATTERNS.items():
            if key == "legal_name":
                continue
            value = bank[section][key]
            (filled if fill_by_label(pattern, value) else skipped).append(key)

        # resume attachment
        try:
            page.set_input_files("input[type=file]", str(resume))
            filled.append("resume")
            page.wait_for_timeout(3000)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"resume ({type(e).__name__})")

        shot = SHOTS / f"prefill-{tag}.png"
        page.screenshot(path=str(shot), full_page=True)
        print(f"filled: {filled}")
        print(f"needs human: {skipped}  <- EEO/demographics and free-text always do")
        print(f"screenshot: {shot}")

        if args.approved:
            btn = page.get_by_role(
                "button", name=re.compile(r"submit", re.I)).first
            btn.click()
            page.wait_for_timeout(5000)
            done = SHOTS / f"submitted-{tag}.png"
            page.screenshot(path=str(done), full_page=True)
            print(f"SUBMITTED. confirmation screenshot: {done}")
        else:
            print("SHADOW MODE: not submitted. Review the screenshot, complete "
                  "free-text/EEO fields, then re-run with --approved or submit "
                  "by hand from the same page.")
        browser.close()


if __name__ == "__main__":
    main()
