#!/usr/bin/env python3
"""forms - read an ATS application form and decide, deterministically, what the
machine may fill, what Haiku may draft, and what sends the job to the manual
queue. Shared by jobsctl (questions / queue) and apply.py (the browser).

Greenhouse exposes every field through its job board API
(`?questions=true`), so a Greenhouse form is classified before any browser
opens. Lever and Ashby have no such API; apply.py reads their forms from the
DOM on first visit and hands the same shape back here.

A question is one of:
  KNOWN       identity / screening fact from the answer bank (name, email, ...)
  ENUM        select / radio whose options match an answers.json enum_map row
  EEO         voluntary self-identification, filled from the bank's stored
              values when present, otherwise left blank (never inferred)
  FILE_RESUME resume upload
  FILE_COVER  cover letter upload (treated as long-form)
  FREE_SHORT  free text the policy allows Haiku to draft (<= max_words)
  FREE_LONG   essay / cover letter / "describe in detail" -> manual queue
  CAPTCHA     a visible challenge -> manual queue, never solved
  UNKNOWN     required field nothing above can answer -> manual queue

Zero dependencies beyond the standard library.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JDS = DATA / "jds"
PRIVATE = DATA / "private"

# label regex -> (section, key) in answers.json. First match wins.
KNOWN_PATTERNS = [
    (r"^first ?name|given name", ("identity", "first_name")),
    (r"^last ?name|surname|family name", ("identity", "last_name")),
    (r"^(?:full |legal )?name$|first and last|your name", ("identity", "legal_name")),
    (r"e-?mail", ("identity", "email")),
    (r"phone|mobile", ("identity", "phone")),
    (r"linkedin", ("identity", "linkedin")),
    (r"street|address line", ("identity", "street_address")),
    (r"zip|postal", ("identity", "zip")),
    (r"^city|city of residence", ("identity", "city")),
    (r"location|where (?:are you|do you) (?:based|live|located)|current residence|city, state", ("identity", "location")),
    (r"current (?:company|employer)|most recent (?:company|employer)|employer name", ("screening", "current_company")),
    (r"current (?:title|role|position)|most recent (?:title|role)|job title", ("screening", "current_title")),
    (r"start date|when (?:can|could) you start|available to start|earliest start|notice period", ("screening", "start_date")),
    (r"salary|compensation|pay (?:range|expectation)|desired (?:pay|rate)", ("salary", "expectation_line")),
    (r"authorized to work|work authorization|legally (?:able|entitled|authorized)|eligible to work", ("screening", "authorized_to_work_us")),
    (r"sponsor", ("screening", "require_sponsorship")),
    (r"citizen", ("screening", "us_citizen")),
    (r"how did you hear|how did you find|where did you hear|referral source", ("screening", "how_heard")),
    (r"referr(?:ed|al) (?:by|name)|who referred", ("screening", "referred_by")),
    (r"over 18|18 years|at least 18", ("screening", "over_18")),
    (r"previously (?:worked|employed)|worked here before|former employee", ("screening", "worked_here_before")),
    (r"willing to travel|able to travel", ("screening", "willing_to_travel")),
]

EEO_PATTERNS = [
    (r"gender|sex\b", "gender"),
    (r"hispanic|latino|latinx", "hispanic_latino"),
    (r"race|ethnicity|ethnic", "race"),
    (r"veteran", "veteran"),
    (r"disabilit", "disability"),
]

NEVER_ANSWER_DEFAULT = ["social security", "ssn", "date of birth", "passport",
                        "driver's license", "bank", "routing", "credit card"]
LONG_FORM_DEFAULT = ["cover letter", "essay", "in detail", "describe a time",
                     "tell us about a time", "500 words", "300 words",
                     "writing sample", "portfolio", "case study", "paragraph"]

# Greenhouse field types -> coarse kinds the classifier reasons about.
GH_TYPES = {
    "input_text": "text", "textarea": "textarea", "input_file": "file",
    "multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
    "input_hidden": "hidden",
}


def load_bank() -> dict:
    p = PRIVATE / "answers.json"
    if not p.exists():
        raise SystemExit("answers.json missing - build the answer bank first (fail loud)")
    bank = json.loads(p.read_text())
    ident = bank.setdefault("identity", {})
    if "first_name" not in ident and ident.get("legal_name"):
        first, _, last = ident["legal_name"].partition(" ")
        ident["first_name"], ident["last_name"] = first, last
    return bank


def questions_path(key: str) -> Path:
    return JDS / (key.replace(":", "__") + ".questions.json")


# ---------------------------------------------------------------- greenhouse


def fetch_gh_questions(slug: str, job_id: str):
    """Pull the form definition from the job board API. None on failure."""
    from jobsctl import http_get  # same directory; lazy to avoid a cycle
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?questions=true"
    status, body = http_get(url)
    if status != 200:
        return None, f"HTTP {status} {body[:80] if status < 0 else ''}".strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None, "bad json"
    return normalize_gh(payload), None


def normalize_gh(payload: dict) -> dict:
    qs = []

    def add(group, q):
        for f in q.get("fields") or []:
            qs.append({
                "group": group,
                "label": (q.get("label") or "").strip(),
                "name": f.get("name") or "",
                "kind": GH_TYPES.get(f.get("type"), f.get("type") or "text"),
                "required": bool(q.get("required")),
                "values": [v.get("label", "") for v in (f.get("values") or [])],
            })

    for q in payload.get("questions") or []:
        add("question", q)
    for q in payload.get("location_questions") or []:
        add("location", q)
    for block in payload.get("compliance") or []:
        for q in block.get("questions") or []:
            add("eeo", q)
    for block in payload.get("demographic_questions") or []:
        for q in (block.get("questions") or []) if isinstance(block, dict) else []:
            add("demographic", q)
    return {
        "source": "gh",
        "apply_url": payload.get("absolute_url", ""),
        "title": payload.get("title", ""),
        "questions": qs,
        "captcha": None,  # only knowable from the DOM
    }


# ------------------------------------------------------------- classification


def _bank_get(bank: dict, section: str, key: str):
    v = (bank.get(section) or {}).get(key)
    if isinstance(v, str) and v.strip() and not v.startswith("FILL_ONLY"):
        return v
    return None


def pick_option(label: str, values: list[str], bank: dict):
    """Choose an option for a select/radio by the enum_map, then by known facts."""
    lab = label.lower()
    for pat, prefs in (bank.get("enum_map") or {}).items():
        if re.search(pat, lab, re.I):
            for pref in prefs:
                for v in values:
                    if pref.lower() == v.lower() or pref.lower() in v.lower():
                        return v
    # A known yes/no fact answered through a select.
    for pat, (section, key) in KNOWN_PATTERNS:
        if re.search(pat, lab, re.I):
            want = _bank_get(bank, section, key)
            if want:
                for v in values:
                    if v.lower() == want.lower() or v.lower().startswith(want.lower()[:3]):
                        return v
    return None


def classify(q: dict, bank: dict):
    """Return (cls, value). value is what to fill for KNOWN / ENUM / EEO."""
    lab = (q.get("label") or "").lower()
    name = (q.get("name") or "").lower()
    kind = q.get("kind") or "text"
    values = q.get("values") or []
    ft = bank.get("free_text") or {}

    if any(t in lab for t in (ft.get("never_answer") or NEVER_ANSWER_DEFAULT)):
        return "UNKNOWN", None
    if "captcha" in lab or "captcha" in name:
        return "CAPTCHA", None
    if kind == "hidden":
        return "SKIP", None
    if kind == "file":
        if "resume" in lab or "resume" in name or "cv" in lab.split():
            return "FILE_RESUME", None
        return "FILE_COVER", None

    if q.get("group") in ("eeo", "demographic") or any(re.search(p, lab) for p, _ in EEO_PATTERNS):
        eeo = (bank.get("screening") or {}).get("eeo_demographics") or {}
        for pat, k in EEO_PATTERNS:
            if re.search(pat, lab):
                want = eeo.get(k)
                if want and values:
                    for v in values:
                        if v.lower() == want.lower() or want.lower()[:8] in v.lower():
                            return "EEO", v
                    for v in values:  # decline option, always acceptable for voluntary fields
                        if re.search(r"decline|prefer not|do not wish", v, re.I):
                            return "EEO", v
                    return "EEO", None
                return "EEO", want
        return "EEO", None

    if kind in ("select", "multiselect", "radio", "checkboxgroup") and values:
        v = pick_option(q.get("label") or "", values, bank)
        return ("ENUM", v) if v else ("ENUM_UNMATCHED", None)

    if kind == "checkbox":  # single consent box
        if re.search(r"agree|consent|acknowledge|privacy|terms|certify|confirm", lab):
            return "ENUM", "checked"
        return "SKIP", None

    for pat, (section, key) in KNOWN_PATTERNS:
        if re.search(pat, lab, re.I) or (name and re.search(pat, name, re.I)):
            want = _bank_get(bank, section, key)
            return ("KNOWN", want) if want else ("UNKNOWN", None)

    if kind == "textarea" or any(m in lab for m in (ft.get("long_form_markers") or LONG_FORM_DEFAULT)):
        if any(m in lab for m in (ft.get("long_form_markers") or LONG_FORM_DEFAULT)):
            return "FREE_LONG", None
        return "FREE_SHORT", None
    if kind == "text" and re.search(r"why|what|how|describe|tell us|interest", lab):
        return "FREE_SHORT", None
    return "UNKNOWN", None


def plan_form(questions: list[dict], bank: dict) -> dict:
    """Split a form into fills, Haiku drafts and blockers."""
    fill, free_short, blockers, optional_skips = [], [], [], []
    for q in questions:
        cls, value = classify(q, bank)
        q = dict(q, cls=cls, value=value)
        req = q.get("required")
        if cls in ("KNOWN", "ENUM", "FILE_RESUME") or (cls == "EEO" and value):
            fill.append(q)
        elif cls == "EEO":
            optional_skips.append(q)  # voluntary, left blank
        elif cls == "FREE_SHORT":
            free_short.append(q)
        elif cls == "SKIP":
            continue
        elif req:
            blockers.append((q, {"FREE_LONG": "long-form answer required",
                                 "FILE_COVER": "cover letter upload required",
                                 "CAPTCHA": "captcha on form",
                                 "ENUM_UNMATCHED": "select option not in enum_map",
                                 "UNKNOWN": "required field the bank cannot answer"}.get(cls, cls)))
        else:
            optional_skips.append(q)
    return {"fill": fill, "free_short": free_short, "blockers": blockers,
            "optional_skips": optional_skips}


def describe(plan: dict) -> str:
    bits = [f"fill={len(plan['fill'])}", f"free_short={len(plan['free_short'])}",
            f"blockers={len(plan['blockers'])}"]
    if plan["blockers"]:
        bits.append("[" + "; ".join(f"{q['label'][:40]!r}: {why}" for q, why in plan["blockers"]) + "]")
    return " ".join(bits)
