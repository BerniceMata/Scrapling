#!/usr/bin/env python3
"""apply - the browser lane. Consumes data/apply-queue.json and submits.

Per job: open the apply page headless, read the form from the DOM, classify
every field with forms.py, fill what the answer bank and the drafted short
answers cover, attach the chosen master resume, refuse anything the gates
cannot clear (manual queue, never guessed), submit, verify a confirmation,
screenshot before and after, append results.jsonl, and mark the key applied in
graph-state.json so it can never be submitted twice.

CAPTCHA is detected and never solved: the job goes to the manual queue.

  --shadow   fill and screenshot, do not submit (run 1)
  --limit N  stop after N jobs
  --key K    only this key

Auto-pauses (written to graph-state.json, only Mitchell clears them):
  10+ captcha hits in a run; confirmation failure rate > 30% after 10 attempts.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import forms  # noqa: E402

DATA = ROOT / "data"
PRIVATE = DATA / "private"
SHOTS = DATA / "screenshots"
RESULTS = PRIVATE / "results.jsonl"
STATE_FILE = DATA / "graph-state.json"
MANUAL_QUEUE = PRIVATE / "manual-queue.md"

CONFIRM_RE = re.compile(
    r"thank you for applying|thanks for applying|application (?:has been |was )?(?:submitted|received|sent)"
    r"|we(?:'ve| have) received your application|successfully submitted|application complete"
    r"|your application has been", re.I)
VALIDATION_RE = re.compile(r"is required|required field|please (?:fill|complete|select|enter)|invalid (?:email|phone)", re.I)
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha/api2/anchor']", "iframe[title*='reCAPTCHA']",
    "iframe[src*='hcaptcha']", ".g-recaptcha", ".h-captcha", "[data-sitekey]",
    "iframe[src*='turnstile']",
]
SUBMIT_RE = re.compile(r"submit application|submit|apply now|send application", re.I)

FIELD_HARVEST_JS = r"""
() => {
  const txt = s => (s || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return txt(l.textContent); }
    const al = el.getAttribute('aria-label'); if (al) return txt(al);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.textContent : ''; }).join(' '); if (txt(t)) return txt(t); }
    const p = el.closest('label'); if (p) return txt(p.textContent);
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg.textContent); }
    let wrap = el.parentElement;
    for (let i = 0; i < 4 && wrap; i++, wrap = wrap.parentElement) {
      const l = wrap.querySelector('label, legend, [class*="label" i]');
      if (l && txt(l.textContent)) return txt(l.textContent);
    }
    return txt(el.getAttribute('placeholder')) || txt(el.name || '');
  };
  const out = [], seen = new Set();
  const root = document.querySelector('form') || document.body;
  const els = root.querySelectorAll('input, select, textarea, [role=combobox]');
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (el.closest('[aria-hidden="true"]')) continue;
    const name = el.getAttribute('name') || '';
    if (type === 'radio' || type === 'checkbox') {
      const gname = name || el.id;
      const key = 'grp:' + gname;
      if (seen.has(key)) continue;
      seen.add(key);
      const group = name ? [...root.querySelectorAll(`input[type=${type}][name="${CSS.escape(name)}"]`)] : [el];
      const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
      let glabel = legend ? txt(legend.textContent) : '';
      if (!glabel && group.length > 1) {
        let wrap = el.parentElement;
        for (let i = 0; i < 5 && wrap; i++, wrap = wrap.parentElement) {
          const l = wrap.querySelector(':scope > label, :scope > legend, :scope > div > label, [class*="label" i]');
          if (l && !l.contains(el) && txt(l.textContent)) { glabel = txt(l.textContent); break; }
        }
      }
      const single = group.length === 1;
      out.push({ sel: name ? `input[type=${type}][name="${name}"]` : ('#' + CSS.escape(el.id)),
                 kind: single ? 'checkbox' : (type === 'radio' ? 'radio' : 'checkboxgroup'),
                 label: single ? labelOf(el) : glabel,
                 required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(glabel)),
                 values: single ? [] : group.map(g => labelOf(g)), name: gname });
      continue;
    }
    const tag = el.tagName.toLowerCase();
    let kind = type === 'file' ? 'file' : tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea'
             : (el.getAttribute('role') === 'combobox' ? 'combobox' : 'text');
    let values = [];
    if (kind === 'select') values = [...el.options].map(o => txt(o.textContent)).filter(v => v && !/^(select|choose|--|please)/i.test(v));
    const lab = labelOf(el);
    const sel = el.id ? '#' + CSS.escape(el.id) : (name ? `${tag}[name="${name}"]` : null);
    if (!sel) continue;
    if (seen.has(sel)) continue; seen.add(sel);
    out.push({ sel, kind, label: lab, name,
               required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(lab)),
               values });
  }
  return out;
}
"""


class JobTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise JobTimeout()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def resume_for(master: str) -> Path:
    names = {"a": ["Mitchell_Nicholas_Resume_GTM_Engineer.pdf", "Mitchell_Nicholas_GTM_Engineer.pdf"],
             "b": ["Mitchell_Nicholas_Resume_Healthcare_GTM.pdf", "Mitchell_Nicholas_Healthcare_GTM.pdf"]}
    for d in (ROOT / "resumes", PRIVATE / "out"):
        for n in names[master]:
            if (d / n).exists():
                return d / n
    sys.exit(f"resume for master {master.upper()} missing in resumes/ or data/private/out/ (fail loud)")


def chromium_path():
    if os.environ.get("CHROMIUM_PATH"):
        return os.environ["CHROMIUM_PATH"]
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pat in ("chromium-*/chrome-linux*/chrome", "chromium_headless_shell-*/chrome-linux*/headless_shell", "chromium/chrome"):
        hits = sorted(glob.glob(os.path.join(base, pat)))
        if hits:
            return hits[-1]
    return None


def drafted_answers(key: str) -> dict:
    """Haiku-drafted, guardrail-checked short answers: data/private/answers-<key>.json {label: text}."""
    p = PRIVATE / f"answers-{key.replace(':', '__')}.json"
    return load_json(p, {})


def _match_answer(label: str, answers: dict):
    f = forms.re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
    for k, v in answers.items():
        fk = forms.re.sub(r"[^a-z0-9]+", " ", k.lower()).strip()
        if fk == f or fk in f or f in fk:
            return v
    return None


def record(res: dict):
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as f:
        f.write(json.dumps(res) + "\n")


def mark_applied(key, job):
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}, "applied": {}, "manual": {}})
    state.setdefault("applied", {})[key] = {
        "company": job["company"], "title": job["title"], "at": now_iso(), "master": job["master"]}
    save_json(STATE_FILE, state)


def to_manual(job, reasons):
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}, "applied": {}, "manual": {}})
    state.setdefault("manual", {})[job["key"]] = {"at": now_iso(), "reasons": reasons}
    save_json(STATE_FILE, state)
    MANUAL_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with MANUAL_QUEUE.open("a") as f:
        f.write(f"- [{job.get('score', '')}] {job['company']} - {job['title']}  {job['url']}\n")
        for why in reasons:
            f.write(f"    - {why}\n")


def set_pause(reason):
    state = load_json(STATE_FILE, {})
    state["paused"] = f"{reason} at {now_iso()}"
    save_json(STATE_FILE, state)


# ------------------------------------------------------------------- browser


def detect_captcha(page) -> bool:
    for sel in CAPTCHA_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def combobox_options(page, sel) -> list[str]:
    try:
        page.click(sel, timeout=3000)
        page.wait_for_timeout(400)
        opts = page.locator("[role=option]")
        vals = [t.strip() for t in opts.all_inner_texts() if t.strip()]
        page.keyboard.press("Escape")
        return vals[:60]
    except Exception:  # noqa: BLE001
        return []


def fill_field(page, q, value, resume: Path):
    sel, kind = q["sel"], q["kind"]
    if kind == "file":
        page.set_input_files(sel, str(resume))
        page.wait_for_timeout(2500)
    elif kind in ("text", "textarea"):
        page.fill(sel, value)
    elif kind == "select":
        page.select_option(sel, label=value)
    elif kind == "combobox":
        page.click(sel)
        page.fill(sel, value)
        page.wait_for_timeout(600)
        opt = page.locator("[role=option]", has_text=value).first
        if opt.count():
            opt.click()
        else:
            page.keyboard.press("Enter")
    elif kind in ("radio", "checkboxgroup"):
        idx = q["values"].index(value)
        page.locator(sel).nth(idx).check(force=True)
    elif kind == "checkbox":
        page.locator(sel).first.check(force=True)
    page.wait_for_timeout(150)


def run_job(pw_browser, job, bank, shadow: bool, resume: Path) -> dict:
    key = job["key"]
    tag = re.sub(r"[^a-z0-9]+", "-", key.lower())
    res = {"key": key, "company": job["company"], "title": job["title"], "url": job["url"],
           "master": job["master"], "bucket": job.get("bucket", ""), "score": job.get("score", ""),
           "location": job.get("location", ""), "comp": job.get("comp", ""), "ts": now_iso()}
    context = pw_browser.new_context(viewport={"width": 1280, "height": 1800},
                                     user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"))
    page = context.new_page()
    page.set_default_timeout(15000)
    try:
        page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        # Greenhouse job pages hide the form behind "Apply" until clicked.
        if job["source"] == "gh" and page.locator("form").count() == 0:
            btn = page.get_by_role("button", name=re.compile(r"^apply", re.I)).first
            if btn.count():
                btn.click()
                page.wait_for_timeout(1500)
            elif "#app" not in page.url:
                page.goto(job["url"].split("#")[0] + "#app", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)

        if detect_captcha(page):
            res.update(outcome="captcha", reason="captcha checkbox visible on load")
            return res

        fields = page.evaluate(FIELD_HARVEST_JS)
        if not fields:
            res.update(outcome="failed", reason="no form fields found")
            return res

        answers = drafted_answers(key)
        plan = forms.plan_form(fields, bank)

        # Comboboxes carry no options in the DOM until opened; resolve required
        # ones that nothing matched by opening them and classifying again.
        resolved_blockers = []
        for q, why in plan["blockers"]:
            if q["kind"] == "combobox":
                vals = combobox_options(page, q["sel"])
                if vals:
                    q2 = dict(q, values=vals, kind="select")
                    cls, value = forms.classify(q2, bank)
                    if value:
                        plan["fill"].append(dict(q2, kind="combobox", cls=cls, value=value))
                        continue
            resolved_blockers.append((q, why))
        plan["blockers"] = resolved_blockers

        for q in plan["free_short"]:
            text = _match_answer(q["label"], answers)
            if text:
                plan["fill"].append(dict(q, cls="FREE_SHORT", value=text))
            elif q.get("required"):
                plan["blockers"].append((q, "short answer not drafted"))

        if plan["blockers"]:
            reasons = [f"{q['label'][:60]}: {why}" for q, why in plan["blockers"]]
            res.update(outcome="manual", reason="; ".join(reasons))
            to_manual(job, reasons)
            learn = [forms.learning_record(q, job, why) for q, why in plan.get("learnable", [])]
            if learn:
                forms.add_learning(PRIVATE / "learning-queue.json", learn)
                res["learning"] = [r["normalized_question"] for r in learn]
            return res

        filled, errors = [], []
        for q in plan["fill"]:
            try:
                fill_field(page, q, q.get("value"), resume)
                filled.append(q["label"][:30])
            except Exception as e:  # noqa: BLE001
                errors.append(f"{q['label'][:30]} ({type(e).__name__})")
        if errors:
            req_err = [e for e in errors if any(q["label"][:30] in e and q.get("required") for q in plan["fill"])]
            if req_err:
                res.update(outcome="failed", reason="could not fill required: " + "; ".join(req_err))
                return res

        SHOTS.mkdir(parents=True, exist_ok=True)
        pre = SHOTS / f"prefill-{tag}.png"
        page.screenshot(path=str(pre), full_page=True)
        res["shot"] = str(pre)
        res["filled"] = filled

        if shadow:
            res.update(outcome="shadow", reason="shadow mode, not submitted")
            return res

        if detect_captcha(page):
            res.update(outcome="captcha", reason="captcha appeared before submit")
            return res

        btn = page.locator("button[type=submit], input[type=submit]").first
        if not btn.count():
            btn = page.get_by_role("button", name=SUBMIT_RE).first
        if not btn.count():
            res.update(outcome="failed", reason="no submit button")
            return res
        btn.click()

        deadline = time.time() + 25
        body = ""
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            try:
                body = page.inner_text("body")
            except Exception:  # noqa: BLE001
                body = ""
            if CONFIRM_RE.search(body) or re.search(r"confirmation|thank|submitted", page.url, re.I):
                break
            if detect_captcha(page):
                res.update(outcome="captcha", reason="captcha challenge after submit")
                return res
        post = SHOTS / f"submitted-{tag}.png"
        page.screenshot(path=str(post), full_page=True)
        res["shot_after"] = str(post)
        if CONFIRM_RE.search(body) or re.search(r"confirmation|thank", page.url, re.I):
            res.update(outcome="submitted", reason="confirmation text seen")
            mark_applied(key, job)
        else:
            msgs = []
            try:
                msgs = [t.strip() for t in page.locator("[class*=error], [role=alert], [aria-live]").all_inner_texts() if t.strip()]
            except Exception:  # noqa: BLE001
                pass
            res.update(outcome="failed", reason="no confirmation; " + ("; ".join(msgs)[:300] or
                       ("validation: " + "; ".join(VALIDATION_RE.findall(body)[:5]) if VALIDATION_RE.search(body) else "unknown")))
        return res
    finally:
        context.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(DATA / "apply-queue.json"))
    ap.add_argument("--shadow", action="store_true", help="fill + screenshot, never submit")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--key")
    ap.add_argument("--job-timeout", type=int, default=90)
    args = ap.parse_args()

    state = load_json(STATE_FILE, {})
    if state.get("paused"):
        sys.exit(f"LANE PAUSED: {state['paused']} - only Mitchell un-pauses (jobsctl resume)")
    queue = load_json(args.queue, [])
    if args.key:
        queue = [j for j in queue if j["key"] == args.key]
    if args.limit:
        queue = queue[:args.limit]
    if not queue:
        sys.exit("queue is empty - run: jobsctl.py queue")
    bank = forms.load_bank()
    resumes = {m: resume_for(m) for m in {j["master"] for j in queue}}

    from playwright.sync_api import sync_playwright
    launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    exe = chromium_path()
    if exe:
        launch["executable_path"] = exe

    counts = {"submitted": 0, "shadow": 0, "manual": 0, "captcha": 0, "failed": 0}
    attempts = 0
    signal.signal(signal.SIGALRM, _alarm)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        for job in queue:
            applied = load_json(STATE_FILE, {}).get("applied", {})
            if job["key"] in applied:
                print(f"skip {job['key']}: already applied")
                continue
            signal.alarm(args.job_timeout)
            try:
                res = run_job(browser, job, bank, args.shadow, resumes[job["master"]])
            except JobTimeout:
                res = {"key": job["key"], "company": job["company"], "title": job["title"],
                       "url": job["url"], "master": job["master"], "ts": now_iso(),
                       "outcome": "failed", "reason": f"timeout after {args.job_timeout}s"}
            except Exception as e:  # noqa: BLE001
                res = {"key": job["key"], "company": job["company"], "title": job["title"],
                       "url": job["url"], "master": job["master"], "ts": now_iso(),
                       "outcome": "failed", "reason": f"{type(e).__name__}: {str(e)[:200]}"}
            finally:
                signal.alarm(0)
            record(res)
            o = res["outcome"]
            counts[o] = counts.get(o, 0) + 1
            attempts += o in ("submitted", "failed")
            print(f"{o:9s} {job['company'][:28]:28s} {job['title'][:40]:40s} {res.get('reason', '')[:70]}")
            if counts["captcha"] >= 10:
                set_pause("10+ captcha hits in one run")
                print("LANE PAUSED: 10+ captcha hits")
                break
            if attempts >= 10 and counts["failed"] / attempts > 0.30:
                set_pause(f"confirmation failure rate {counts['failed']}/{attempts}")
                print("LANE PAUSED: failure rate above 30%")
                break
        browser.close()
    print(f"\napply: {counts}  -> {RESULTS}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
