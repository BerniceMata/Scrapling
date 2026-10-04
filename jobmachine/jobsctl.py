#!/usr/bin/env python3
"""jobsctl - Mitchell's job machine: discovery + screening lane.

Zero dependencies beyond the Python 3.10+ standard library, so it runs
anywhere: this repo's sandbox, a remote workbench, or a laptop.

Commands:
  doctor                     live-probe all three ATS lanes; every failure loud
  resolve <companies.txt>    slug-guess each company against all three APIs -> data/slugs.json
  discover                   pull postings from resolved boards, gate, rank -> data/jobs.csv
  jd <job-key>               fetch + cache one job description -> data/jds/
  screen <resume.txt> <job-key>   deterministic ATS keyword coverage + reality gate
  state                      show run log / seen / rejected counts

Job keys look like  gh:headway:12345  /  lever:acme:uuid  /  ashby:ramp:uuid

Hard-won rules baked in (see CLAUDE.md):
  - go to the ATS source, not the front door
  - score the JD, never the title (screen runs the reality gate FIRST)
  - slug resolution false-merges: low-count boards carry sample titles for review
  - exact strings matter ("Bachelor of Science" vs "B.S.")
  - every component fails loud
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import html
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JDS = DATA / "jds"
SLUGS_FILE = DATA / "slugs.json"
STATE_FILE = DATA / "graph-state.json"
JOBS_CSV = DATA / "jobs.csv"
PRIVATE = DATA / "private"  # gitignored: masters, answer bank, tracker, tailored output

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 25
MAX_AGE_DAYS = 60
MIN_COVERAGE = 60  # percent; below this = rewrite before submitting

# ---------------------------------------------------------------- config gates

TITLE_INCLUDE = re.compile(
    r"(sdr|bdr|sales development|business development"
    r"|account executive|account manager|inside sales"
    r"|sales (?:rep|representative|associate|specialist|coordinator)"
    r"|customer success|client success|customer experience"
    r"|implementation|onboarding|enablement"
    r"|revenue operations|rev ?ops|sales operations|sales ops"
    r"|go[- ]?to[- ]?market|\bgtm\b|revenue systems|gtm systems"
    r"|growth (?:ops|operations|associate|analyst|marketing|engineer)"
    r"|marketing operations|marketing ops|marketing engineer"
    r"|ai (?:operations|ops|engineer|automation)|applied ai|agent engineer"
    r"|forward[- ]deployed|solutions? (?:engineer|architect|consultant|associate|specialist)"
    r"|email deliverability|deliverability|email infrastructure"
    r"|lifecycle marketing|marketing automation|campaign operations"
    r"|business operations|bizops"
    r"|training (?:and|&) development"
    r"|(?:patient|care|clinical|program) coordinator|genetic counseling assistant)",
    re.I,
)
TITLE_EXCLUDE = re.compile(
    r"(senior|\bsr\.?\b|staff|principal|director|vice president|\bvp\b"
    r"|head of|chief|intern(ship)?\b|\bii\b|\biii\b|\biv\b"
    r"|\bmanager\b|\blead\b|research|\bphd\b|nurse|\brn\b|licensed|\bnp\b)",
    re.I,
)

TX_OK = re.compile(
    r"(dallas|irving|fort worth|plano|richardson|addison|frisco|arlington"
    r"|grapevine|southlake|midlothian|dfw|texas|\btx\b|austin)",
    re.I,
)
REMOTE_RE = re.compile(r"remote|anywhere|distributed|work from home", re.I)
FOREIGN_RE = re.compile(
    r"(canada|emea|apac|\buk\b|united kingdom|europe|australia|india|latam"
    r"|mexico|germany|france|ireland|philippines|brazil|poland|spain"
    r"|netherlands|singapore|japan|israel|ontario|toronto|mississauga"
    r"|vancouver|montreal|london\b|dublin|berlin|paris|amsterdam)",
    re.I,
)
US_RE = re.compile(r"(\bus\b|\bu\.s\.|usa|united states|north america|\bna\b)", re.I)

HEALTHCARE_HINTS = re.compile(
    r"(health|medical|clinic|care|patient|pharma|bio|genomic|diagnostic"
    r"|therap|dental|nurse|provider|payer|oncolog)",
    re.I,
)

# ------------------------------------------------------------------- plumbing


def _ctx() -> ssl.SSLContext:
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and Path(bundle).exists():
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


def http_get(url: str) -> tuple[int, str]:
    """Return (status, body). status<0 means transport failure. Loud, never silent."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # noqa: BLE001 - report the class, keep sweeping
        return -1, f"{type(e).__name__}: {e}"


def get_json(url: str):
    status, body = http_get(url)
    if status != 200:
        return status, None
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return -2, None


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True))


# ------------------------------------------------------------------ ATS lanes


def gh_list(slug):
    return get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")


def lever_list(slug):
    return get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")


def ashby_list(slug):
    return get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    )


def source_jobs(source: str, slug: str, payload) -> list[dict]:
    """Normalize a board payload to [{id,title,location,url,apply_url,posted,comp}]."""
    out = []
    if source == "gh":
        for j in payload.get("jobs", []):
            out.append(
                {
                    "id": str(j.get("id")),
                    "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("name", ""),
                    "url": j.get("absolute_url", ""),
                    "apply_url": j.get("absolute_url", ""),
                    "posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
                    "comp": "",
                }
            )
    elif source == "lever":
        for j in payload if isinstance(payload, list) else []:
            loc = (j.get("categories") or {}).get("location", "") or ""
            wp = j.get("workplaceType") or ""
            sal = j.get("salaryRange") or {}
            comp = ""
            if sal.get("min") and sal.get("max"):
                comp = f"${int(sal['min'])//1000}-{int(sal['max'])//1000}K"
            posted = ""
            if j.get("createdAt"):
                posted = datetime.fromtimestamp(
                    j["createdAt"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            out.append(
                {
                    "id": str(j.get("id")),
                    "title": j.get("text", ""),
                    "location": f"{loc} {wp}".strip(),
                    "url": j.get("hostedUrl", ""),
                    "apply_url": j.get("applyUrl", j.get("hostedUrl", "")),
                    "posted": posted,
                    "comp": comp,
                }
            )
    elif source == "ashby":
        for j in payload.get("jobs", []):
            locs = [j.get("location", "")] + [
                s.get("location", "") for s in j.get("secondaryLocations", []) or []
            ]
            if j.get("isRemote"):
                locs.append("Remote")
            comp = ""
            c = j.get("compensation") or {}
            if c.get("compensationTierSummary"):
                comp = c["compensationTierSummary"]
            out.append(
                {
                    "id": str(j.get("id")),
                    "title": j.get("title", ""),
                    "location": "; ".join(x for x in locs if x),
                    "url": j.get("jobUrl", ""),
                    "apply_url": j.get("applyUrl", j.get("jobUrl", "")),
                    "posted": (j.get("publishedAt") or "")[:10],
                    "comp": comp,
                }
            )
    return out


def fetch_jd(source: str, slug: str, job_id: str) -> dict | None:
    """Fetch one job's full description; returns {title,location,text} or None."""
    if source == "gh":
        st, d = get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
        )
        if d:
            return {
                "title": d.get("title", ""),
                "location": (d.get("location") or {}).get("name", ""),
                "text": strip_html(d.get("content", "")),
            }
    elif source == "lever":
        st, d = get_json(f"https://api.lever.co/v0/postings/{slug}/{job_id}")
        if d:
            parts = [d.get("descriptionPlain") or strip_html(d.get("description", ""))]
            for lst in d.get("lists", []) or []:
                parts.append(lst.get("text", ""))
                parts.append(strip_html(lst.get("content", "")))
            parts.append(d.get("additionalPlain") or "")
            return {
                "title": d.get("text", ""),
                "location": (d.get("categories") or {}).get("location", ""),
                "text": "\n".join(p for p in parts if p),
            }
    elif source == "ashby":
        st, d = ashby_list(slug)
        if d:
            for j in d.get("jobs", []):
                if str(j.get("id")) == job_id:
                    return {
                        "title": j.get("title", ""),
                        "location": j.get("location", ""),
                        "text": strip_html(
                            j.get("descriptionHtml", "") or j.get("descriptionPlain", "")
                        ),
                    }
    return None


# -------------------------------------------------------------------- resolve

SLUG_OVERRIDES = {
    # verified or obvious non-guessable slugs; extend as they're confirmed
    "bill.com": ["bill"],
    "square/block": ["block"],
    "block": ["block"],
    "at&t business": ["att"],
    "the trade desk": ["thetradedesk"],
    "google cloud": ["google"],
    "aws": ["amazon"],
}


def slug_candidates(company: str) -> list[str]:
    key = company.strip().lower()
    if key in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[key]
    base = re.sub(r"[^a-z0-9 ]", "", key)
    words = base.split()
    cands = []
    joined = "".join(words)
    dashed = "-".join(words)
    for c in (joined, dashed):
        if c and c not in cands:
            cands.append(c)
    # first word alone is a notorious false-merge source ("boston" != Boston
    # Scientific); keep it last and let the sample-title check flag it.
    if len(words) > 1 and words[0] not in cands and len(words[0]) > 3:
        cands.append(words[0])
    return cands


def _probe_board(company: str, source: str, slug: str):
    fn = {"gh": gh_list, "lever": lever_list, "ashby": ashby_list}[source]
    status, payload = fn(slug)
    if status != 200 or payload is None:
        return None
    jobs = source_jobs(source, slug, payload)
    if not jobs:
        return None
    norm = re.sub(r"[^a-z0-9]", "", company.lower())
    suspicious = slug != norm and (len(jobs) < 5 or len(slug) <= 6)
    return {
        "company": company,
        "source": source,
        "slug": slug,
        "job_count": len(jobs),
        "sample_titles": [j["title"] for j in jobs[:3]],
        "verify": suspicious,
        "resolved_at": now_iso(),
    }


def cmd_resolve(args):
    companies = [
        ln.strip()
        for ln in Path(args.companies).read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    existing = load_json(SLUGS_FILE, {}) if not args.fresh else {}
    todo = [c for c in companies if c not in existing]
    print(f"resolve: {len(companies)} companies, {len(todo)} unresolved, "
          f"{len(existing)} cached")
    results = dict(existing)
    misses, errors = [], []

    def work(company):
        for source in ("gh", "lever", "ashby"):
            for slug in slug_candidates(company):
                hit = _probe_board(company, source, slug)
                if hit:
                    return company, hit
                time.sleep(0.05)
        return company, None

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (company, hit) in enumerate(ex.map(work, todo), 1):
            if hit:
                results[company] = hit
                flag = "  [VERIFY]" if hit["verify"] else ""
                print(f"  [{i}/{len(todo)}] {company}: {hit['source']}:{hit['slug']} "
                      f"({hit['job_count']} jobs){flag}")
            else:
                misses.append(company)
                print(f"  [{i}/{len(todo)}] {company}: no board found")
    save_json(SLUGS_FILE, results)
    verify = [c for c, v in results.items() if v.get("verify")]
    print(f"\nresolved {len(results)}/{len(companies)}; misses={len(misses)}; "
          f"needs-verify={len(verify)}")
    if verify:
        print("VERIFY sample titles before trusting these boards:")
        for c in verify:
            v = results[c]
            print(f"  {c} -> {v['source']}:{v['slug']}: {v['sample_titles']}")
    if misses:
        print("no modern-ATS board (Workday/custom portal lane):")
        for c in misses:
            print(f"  {c}")


# ------------------------------------------------------------------- discover


def bucket(title: str) -> str:
    """Six job families from the Greenhouse Dorks doc, most specific first."""
    t = title.lower()
    for pat, name in [
        (r"gtm engineer|gtm systems|revenue systems|growth engineer|marketing engineer", "F1 GTM Eng"),
        (r"forward[- ]deployed|solutions? (?:engineer|architect)|ai engineer|applied ai"
         r"|ai automation|agent engineer|ai (?:operations|ops)", "F2 AI/FDE"),
        (r"deliverability|email infrastructure|lifecycle marketing|marketing automation"
         r"|campaign operations", "F3 Lifecycle"),
        (r"revenue operations|rev ?ops|sales operations|sales ops|marketing op"
         r"|gtm|go[- ]?to[- ]?market|business operations|bizops|growth", "F4 RevOps"),
        (r"(?:patient|care|clinical|program) coordinator|genetic counseling", "F5 HC Admin"),
        (r"sdr|bdr|sales development|business development", "SDR/BDR"),
        (r"implementation|onboarding", "Implementation"),
        (r"customer success|client success|customer experience", "CS"),
        (r"enablement|training", "Enablement"),
        (r"solutions? (?:consultant|associate|specialist)", "Solutions"),
        (r"account executive", "AE"),
        (r"account manager|inside sales|sales", "Sales"),
    ]:
        if re.search(pat, t):
            return name
    return "Other"


# Family 6 (health-tech GTM) is family 1/4 at a healthcare company: it is
# scored through HEALTHCARE_HINTS in prior_score, not as a separate bucket.
BUCKET_WEIGHT = {
    "F1 GTM Eng": 12, "F2 AI/FDE": 11, "F3 Lifecycle": 10, "F4 RevOps": 10,
    "SDR/BDR": 8, "Implementation": 7, "CS": 7, "Solutions": 7, "Enablement": 6,
    "Sales": 5, "AE": 4, "F5 HC Admin": 3, "Other": 0,
}
OPS_BUCKETS = {"F1 GTM Eng", "F2 AI/FDE", "F3 Lifecycle", "F4 RevOps", "Solutions"}


def comp_floor_ok(comp: str) -> bool | None:
    """None = unknown; True/False vs $70K base floor (rough parse of the max)."""
    nums = [int(n) for n in re.findall(r"\$?(\d{2,3})[kK]", comp or "")]
    nums += [int(n.replace(",", "")) // 1000 for n in re.findall(r"\$(\d{2,3},\d{3})", comp or "")]
    if not nums:
        return None
    return max(nums) >= 70


def location_ok(loc: str) -> bool:
    if TX_OK.search(loc):
        return True
    if REMOTE_RE.search(loc):
        if FOREIGN_RE.search(loc) and not US_RE.search(loc):
            return False
        return True
    return False


def age_days(posted: str) -> int | None:
    try:
        d = datetime.strptime(posted, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except (ValueError, TypeError):
        return None


def prior_score(company: str, title: str, loc: str, comp: str) -> int:
    s = 50 + BUCKET_WEIGHT[bucket(title)]
    if HEALTHCARE_HINTS.search(company):
        s += 8
    ok = comp_floor_ok(comp)
    if ok:
        s += 5
    if TX_OK.search(loc):
        s += 5
    return min(s, 98)


def cmd_discover(args):
    slugs = load_json(SLUGS_FILE, {})
    if not slugs:
        sys.exit("discover: no data/slugs.json - run resolve first (fail loud)")
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}})
    run_ts = now_iso()
    drops = {"rejected_cache": 0, "title": 0, "location": 0, "age": 0, "comp": 0}
    rows, board_fail = [], []
    seen_before = set(state["seen"])

    def sweep(item):
        company, meta = item
        fn = {"gh": gh_list, "lever": lever_list, "ashby": ashby_list}[meta["source"]]
        status, payload = fn(meta["slug"])
        if status != 200 or payload is None:
            return company, meta, None
        return company, meta, source_jobs(meta["source"], meta["slug"], payload)

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for company, meta, jobs in ex.map(sweep, sorted(slugs.items())):
            if jobs is None:
                board_fail.append(f"{company} ({meta['source']}:{meta['slug']})")
                continue
            for j in jobs:
                key = f"{meta['source']}:{meta['slug']}:{j['id']}"
                # manual/judgment rejects only (jobsctl reject <key>); deterministic
                # gate drops are re-derived each run for free, not persisted per key
                if key in state["rejected"]:
                    drops["rejected_cache"] += 1
                    continue
                if not TITLE_INCLUDE.search(j["title"]) or TITLE_EXCLUDE.search(j["title"]):
                    drops["title"] += 1
                    continue
                if not location_ok(j["location"]):
                    drops["location"] += 1
                    continue
                a = age_days(j["posted"])
                if a is not None and a > MAX_AGE_DAYS:
                    drops["age"] += 1
                    continue
                if comp_floor_ok(j["comp"]) is False:
                    drops["comp"] += 1
                    continue
                new = key not in state["seen"]
                ent = state["seen"].setdefault(key, {"first_seen": run_ts})
                ent["last_seen"] = run_ts
                ent["title"] = j["title"]
                rows.append(
                    {
                        "key": key, "company": company, "title": j["title"],
                        "bucket": bucket(j["title"]), "location": j["location"],
                        "comp": j["comp"], "posted": j["posted"],
                        "age_days": "" if a is None else a,
                        "score": prior_score(company, j["title"], j["location"], j["comp"]),
                        "new": "NEW" if new else "",
                        "url": j["url"],
                    }
                )

    gone = [k for k in seen_before
            if state["seen"][k].get("last_seen") != run_ts
            and not state["seen"][k].get("closed")]
    for k in gone:
        state["seen"][k]["closed"] = run_ts

    rows.sort(key=lambda r: -r["score"])
    JOBS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["key", "company", "title", "bucket", "location", "comp",
                            "posted", "age_days", "score", "new", "url"])
        w.writeheader()
        w.writerows(rows)

    new_count = sum(1 for r in rows if r["new"])
    state["runs"].append(
        {"at": run_ts, "boards": len(slugs), "board_failures": board_fail,
         "passed": len(rows), "new": new_count, "drops": drops,
         "newly_closed": len(gone)}
    )
    save_json(STATE_FILE, state)
    print(f"discover: {len(rows)} passed ({new_count} NEW), drops={drops}, "
          f"newly closed={len(gone)}")
    if board_fail:
        print(f"BOARD FAILURES ({len(board_fail)}) - loud, not silent:")
        for b in board_fail:
            print(f"  {b}")
    print(f"wrote {JOBS_CSV}")


# --------------------------------------------------------------------- screen

# canonical term -> regex variants. Exact-string lesson: "B.S." is invisible to
# a "Bachelor's degree" keyword screen; both directions are listed explicitly.
LEXICON: dict[str, str] = {
    "bachelor's degree": r"bachelor'?s? degree|bachelor of science|bachelor of arts|\bb\.?s\.?\b|\bb\.?a\.?\b",
    "salesforce": r"salesforce|sfdc",
    "hubspot": r"hubspot",
    "crm": r"\bcrm\b",
    "gohighlevel": r"gohighlevel|go high level|highlevel",
    "airtable": r"airtable",
    "excel/sheets": r"\bexcel\b|google sheets|spreadsheet",
    "sql": r"\bsql\b",
    "python": r"\bpython\b",
    "javascript": r"javascript|node\.?js",
    "apis": r"\bapis?\b|webhook|rest\b",
    "automation": r"automat(e|ion|ed)",
    "ai tools": r"\bai\b|artificial intelligence|\bllm\b|chatgpt|claude\b|machine learning",
    "dashboards/reporting": r"dashboards?|reporting|reports\b",
    "analytics": r"analytics|data[- ]driven|\bkpis?\b|metrics",
    "a/b testing": r"a/?b test",
    "saas": r"\bsaas\b|software[- ]as[- ]a[- ]service",
    "b2b": r"\bb2b\b|business[- ]to[- ]business",
    "outbound prospecting": r"outbound|prospect(ing|s)?",
    "cold calling": r"cold[- ]call(ing|s)?|\bdials?\b",
    "cold email": r"cold email|email outreach|email campaign",
    "pipeline": r"pipeline",
    "quota": r"quota",
    "lead generation": r"lead gen(eration)?|\bleads\b",
    "lead qualification": r"qualif(y|ication|ied)",
    "discovery calls": r"discovery call|discovery meeting",
    "objection handling": r"objection",
    "closing": r"clos(e|ing) deals|closed[- ]won|closing",
    "negotiation": r"negotiat",
    "account management": r"account management|book of business",
    "customer success": r"customer success|client success",
    "onboarding": r"onboarding",
    "implementation": r"implementation",
    "renewals/retention": r"renewal|retention|churn",
    "stakeholder communication": r"stakeholders?",
    "cross-functional": r"cross[- ]functional",
    "project management": r"project management|program management",
    "communication skills": r"communication skills|written and verbal|verbal and written",
    "presentation": r"presentation|presenting|demos?\b",
    "revenue operations": r"revenue operations|rev ?ops",
    "sales operations": r"sales operations|sales ops",
    "gtm": r"go[- ]?to[- ]?market|\bgtm\b",
    "marketing automation": r"marketing automation|marketo|pardot|mailchimp|email marketing",
    "sales engagement tools": r"outreach\.io|salesloft|apollo\.io|\bgong\b|zoominfo|sales navigator",
    "healthcare": r"health ?care|health system|healthtech|digital health",
    "clinical": r"clinical|clinician",
    "providers": r"providers?\b|physicians?|\bdoctors?\b",
    "payers": r"payers?\b|health plans?",
    "patients": r"patients?\b",
    "ehr/emr": r"\behr\b|\bemr\b|epic\b|cerner",
    "hipaa": r"hipaa",
    "medical terminology": r"medical terminology",
    "startup pace": r"startup|fast[- ]paced|ambigu(ity|ous)|scrappy",
    "remote collaboration": r"remote|distributed team",
    "slack": r"\bslack\b",
    "travel": r"travel\b",
}

YEARS_RE = re.compile(r"(\d+)\s*(?:\+|-\d+)?\s*(?:or more\s*)?years?", re.I)
SPONSOR_RE = re.compile(r"sponsor(ship)?", re.I)
ONSITE_RE = re.compile(r"on[- ]?site|in[- ]?office|hybrid", re.I)


def load_jd(key: str, refresh: bool = False) -> dict:
    source, slug, job_id = key.split(":", 2)
    cache = JDS / f"{key.replace(':', '__')}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    jd = fetch_jd(source, slug, job_id)
    if not jd:
        sys.exit(f"jd: could not fetch {key} (fail loud - is the posting still live?)")
    jd["key"] = key
    jd["fetched_at"] = now_iso()
    save_json(cache, jd)
    return jd


def cmd_jd(args):
    jd = load_jd(args.key, refresh=args.refresh)
    print(f"{jd['title']}  |  {jd['location']}  |  cached {jd['fetched_at']}")
    print(jd["text"][:2000])


def cmd_screen(args):
    resume = Path(args.resume).read_text().lower()
    jd = load_jd(args.key)
    text = jd["text"]
    tl = text.lower()

    # Gate 1 - REALITY GATE FIRST (the Gusto 91->48 lesson)
    problems = []
    years = [int(m.group(1)) for m in YEARS_RE.finditer(text) if int(m.group(1)) <= 15]
    hard_years = max(years) if years else None
    if hard_years and hard_years >= 4:
        problems.append(f"experience ask up to {hard_years} years - check if it's a hard requirement")
    if ONSITE_RE.search(text) and not TX_OK.search(text):
        problems.append("onsite/hybrid language with no TX city - verify location before tailoring")
    comp_nums = [int(n) for n in re.findall(r"\$(\d{2,3})[kK,]", text)]
    if comp_nums and max(comp_nums) < 70:
        problems.append(f"posted comp tops out at ${max(comp_nums)}K < $70K floor")
    if SPONSOR_RE.search(text):
        problems.append("sponsorship language present (fine - US citizen - but confirm the question wording)")

    # Gate 2 - deterministic keyword coverage
    in_jd = {term: pat for term, pat in LEXICON.items() if re.search(pat, tl)}
    matched = {t for t, pat in in_jd.items() if re.search(pat, resume)}
    missing = sorted(set(in_jd) - matched)
    coverage = round(100 * len(matched) / len(in_jd)) if in_jd else 0

    print(f"SCREEN {args.key}: {jd['title']} | {jd['location']}")
    print(f"\n[reality gate] {'CLEAR' if not problems else 'FLAGS:'}")
    for p in problems:
        print(f"  ! {p}")
    print(f"\n[ats coverage] {coverage}% ({len(matched)}/{len(in_jd)} JD-relevant terms in resume)")
    if missing:
        print("  missing (add only where TRUTHFUL):")
        for t in missing:
            print(f"    - {t}")
    verdict = "PASS" if coverage >= MIN_COVERAGE and not problems else \
              "REWRITE" if coverage < MIN_COVERAGE else "REVIEW FLAGS"
    print(f"\nverdict: {verdict} (floor {MIN_COVERAGE}%)")
    print("next gates (run the application-council skill): recruiter 7-second skim, "
          "hiring-manager pain match, truth gate")
    sys.exit(0 if verdict == "PASS" else 1)


# --------------------------------------------------------------------- doctor

DOCTOR_PROBES = [
    ("greenhouse", gh_list, "stripe"),
    ("lever", lever_list, "palantir"),
    ("ashby", ashby_list, "ramp"),
]


def cmd_doctor(args):
    failures = 0
    for name, fn, slug in DOCTOR_PROBES:
        status, payload = fn(slug)
        if status == 200 and payload:
            n = len(source_jobs({"greenhouse": "gh", "lever": "lever", "ashby": "ashby"}[name], slug, payload))
            print(f"  {name:<12} OK   ({slug}: {n} postings)")
        else:
            failures += 1
            host = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co",
                    "ashby": "api.ashbyhq.com"}[name]
            why = ("DENIED HOST - add it under the environment's Allowed domains"
                   if status in (-1, 403, 407) else "lane is DOWN")
            print(f"  {name:<12} FAIL (status={status}) {host}: {why}")
    slugs = load_json(SLUGS_FILE, {})
    print(f"  boards resolved: {len(slugs)}"
          + ("" if slugs else "  (run resolve)"))
    state = load_json(STATE_FILE, {"runs": []})
    if state["runs"]:
        last = state["runs"][-1]
        print(f"  last sweep: {last['at']} - {last['passed']} passed, "
              f"{len(last.get('board_failures', []))} board failures")
    sys.exit(1 if failures else 0)


def cmd_tailor(args):
    """Emit the tailoring brief for one job: master + JD + the rules.

    The rewrite itself is done by the model reading this brief (Claude in this
    session, or any LLM), not by an API call from here - jobmachine holds no
    model key. The brief is written so the output can be pasted straight to
    data/private/tailored/<key>.json and then run through `check`.
    """
    key = args.key
    master_path = PRIVATE / ("resume_master_%s.json" % args.master.lower())
    if not master_path.exists():
        print("! missing %s" % master_path)
        return
    jd_path = JDS / (key.replace(":", "__") + ".txt")
    jd = jd_path.read_text()[:12000] if jd_path.exists() else ""
    if not jd:
        print("! no cached JD for %s - run: jobsctl.py jd %s" % (key, key))
        return
    rows = {r["key"]: r for r in csv.DictReader(JOBS_CSV.open())} if JOBS_CSV.exists() else {}
    row = rows.get(key, {})
    out_dir = PRIVATE / "tailored"
    out_dir.mkdir(parents=True, exist_ok=True)
    brief = out_dir / (key.replace(":", "__") + ".brief.md")
    brief.write_text(TAILOR_BRIEF.format(
        company=row.get("company", "?"), title=row.get("title", "?"),
        location=row.get("location", "?"), master=args.master.upper(),
        master_path=master_path, jd=jd,
        out_path=out_dir / (key.replace(":", "__") + ".json"),
    ))
    print("wrote %s" % brief)
    print("next: produce the tailored JSON, then")
    print("  python3 guardrail.py %s %s" % (master_path, out_dir / (key.replace(':', '__') + '.json')))


TAILOR_BRIEF = """# Tailoring brief: {company} - {title}

Location: {location}
Master (truth base): {master} at {master_path}
Write the tailored resume as JSON to: {out_path}

## Rules (guardrail.py enforces these; violations block the submit)

ALLOWED:
- Reorder work entries, bullets, and skill groups for this role.
- Rewrite bullet and summary phrasing to use this JD's vocabulary for the SAME
  work that is already described in the master.
- Surface skills that already appear ANYWHERE in the master, including ones
  currently buried inside a bullet or a job summary.
- Drop bullets or entries that are irrelevant to this role.

FORBIDDEN:
- Adding an employer, title, school, degree, or project not in the master.
- Changing any date, including extending one to close a gap.
- Changing or inflating any number, percentage, or dollar figure. If the master
  says 45+, the output says 45+. Numbers may not move between employers.
- Adding a skill the master never mentions, even if the JD asks for it.
- Em dashes or en dashes anywhere. Use a comma, colon, or period.
- Abbreviating the degree. It stays "Bachelor of Science (B.S.)" with
  "Healthcare Studies" spelled out.

Keep the same JSON shape as the master, including the "location" object under
basics. Output JSON only.

## Job description

{jd}
"""


def cmd_check(args):
    """Run the no-fabrication guardrail on a tailored resume."""
    sys.path.insert(0, str(ROOT))
    import guardrail
    base = load_json(Path(args.master), None)
    tailored = load_json(Path(args.tailored), None)
    if base is None or tailored is None:
        print("! could not read both files")
        return
    violations = guardrail.check(base, tailored)
    if not violations:
        print("GUARDRAIL CLEAN: %s traces entirely to %s" % (args.tailored, args.master))
        return
    print("GUARDRAIL FAILED: %d violation(s) - do NOT submit this version\n" % len(violations))
    print(guardrail.summarize(violations, limit=100))


def cmd_render(args):
    """Render a tailored resume to an ATS-parseable .docx."""
    sys.path.insert(0, str(ROOT))
    import guardrail
    import render_docx
    base = load_json(Path(args.master), None)
    tailored = load_json(Path(args.tailored), None)
    if base is None or tailored is None:
        print("! could not read both files")
        return
    violations = guardrail.check(base, tailored)
    if violations and not args.force:
        print("REFUSING TO RENDER: %d guardrail violation(s)\n" % len(violations))
        print(guardrail.summarize(violations, limit=100))
        print("\nFix the tailored JSON, or pass --force to render anyway (not advised).")
        return
    if violations:
        print("! rendering with %d unresolved violation(s) because --force was passed"
              % len(violations))
    path = render_docx.render(tailored, args.out)
    print("wrote %s" % path)


def cmd_prep(args):
    """Build a review packet for one job: reality gate, coverage, answers, next steps.

    Shadow mode: this produces the packet Mitchell reviews. Nothing submits here.
    """
    jd = load_jd(args.key)
    text, tl = jd["text"], jd["text"].lower()
    row = {}
    if JOBS_CSV.exists():
        for r in csv.DictReader(JOBS_CSV.open()):
            if r["key"] == args.key:
                row = r
                break

    healthcareish = bool(HEALTHCARE_HINTS.search(row.get("company", ""))
                         or re.search(r"health|clinic|patient|provider|payer", tl))
    # role bucket beats industry: ops/technical roles always get Master A
    # (the ShiftKey lesson: healthcare company + RevOps title = A, not B)
    ops_bucket = bucket(jd["title"]) in OPS_BUCKETS
    master = "a" if ops_bucket else ("b" if healthcareish else "a")
    resume_path = args.resume
    if not resume_path:
        hits = sorted((DATA / "private").glob(f"resume_master_{master}*.txt"))
        resume_path = str(hits[0]) if hits else None

    lines = [f"# Application packet: {row.get('company', '?')} - {jd['title']}",
             f"key: {args.key}",
             f"location: {jd['location']}   comp: {row.get('comp', 'not posted')}",
             f"posting: {row.get('url', '(run discover for url)')}", ""]

    # reality gate
    problems = []
    years = [int(m.group(1)) for m in YEARS_RE.finditer(text) if int(m.group(1)) <= 15]
    if years and max(years) >= 4:
        problems.append(f"experience ask up to {max(years)} years - verify hard requirement")
    if ONSITE_RE.search(text) and not TX_OK.search(text):
        problems.append("onsite/hybrid language, no TX city - verify location")
    lines.append("## Reality gate")
    lines += [f"- ! {p}" for p in problems] or ["- CLEAR"]

    # coverage
    lines.append(f"\n## Resume: Master {master.upper()}  ({resume_path or 'RESUME FILE MISSING'})")
    if resume_path and Path(resume_path).exists():
        resume = Path(resume_path).read_text().lower()
        in_jd = {t: p for t, p in LEXICON.items() if re.search(p, tl)}
        matched = {t for t, p in in_jd.items() if re.search(p, resume)}
        missing = sorted(set(in_jd) - matched)
        pct = round(100 * len(matched) / len(in_jd)) if in_jd else 0
        lines.append(f"- ATS coverage {pct}% ({len(matched)}/{len(in_jd)})")
        if missing:
            lines.append(f"- missing (add only where truthful): {', '.join(missing)}")

    # answers
    ans_file = DATA / "private" / "answers.json"
    lines.append("\n## Standard answers (from bank)")
    if ans_file.exists():
        bank = json.loads(ans_file.read_text())
        ident, scr = bank["identity"], bank["screening"]
        lines += [
            f"- name: {ident['legal_name']}  email: {ident['email']}  phone: {ident['phone']}",
            f"- location: {ident['location']}  linkedin: {ident['linkedin']}",
            f"- authorized to work in US: {scr['authorized_to_work_us']}  sponsorship: {scr['require_sponsorship']}",
            f"- start: {scr['start_date']}",
            f"- salary line: {bank['salary']['expectation_line']}",
            "- EEO/demographic questions: MITCHELL_DECIDES (never auto-answered)",
        ]
    else:
        lines.append("- ANSWER BANK MISSING (data/private/answers.json)")

    lines += [
        "\n## Cover letter",
        "- Only if required or score 75+. Story-driven: the problem this company",
        "  solves, the unique thing they do, and what Mitchell is building. No em",
        "  dashes. Never template slop.",
        "\n## Truth gate reminders",
        "- No Salesforce claim. HubSpot = 1 year. Degree spelled out in full.",
        "- Council gates (skim, pain match) run in chat before submit.",
        "\n## Submit",
        "- SHADOW MODE: Mitchell approves this packet, then submission happens",
        f"- apply here: {row.get('url', jd.get('key'))}",
    ]

    out = DATA / "packets" / f"{args.key.replace(':', '__')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {out}")


def cmd_digest(args):
    """Compose the daily brief: scoreboard, new qualifiers, approvals, follow-ups.

    Output is markdown on stdout (and data/digest-latest.md). The batch-approve
    flow consumes the 'awaiting approval' keys; Mitchell replies 'approve'.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}})
    last_digest = state.get("last_digest", "1970-01-01T00:00:00Z")
    rows = list(csv.DictReader(JOBS_CSV.open())) if JOBS_CSV.exists() else []
    tracker_file = DATA / "private" / "tracker.csv"
    tracker = list(csv.DictReader(tracker_file.open())) if tracker_file.exists() else []

    applied = [t for t in tracker if t.get("status") == "Applied"]
    followups = [t for t in applied
                 if t.get("followup_due") and t["followup_due"] <= today]
    packets = {p.stem.replace("__", ":", 2) for p in (DATA / "packets").glob("*.md")}
    applied_companies = {t["company"].lower() for t in applied}
    awaiting = sorted(
        (r for r in rows if r["key"] in packets
         and r["company"].lower() not in applied_companies),
        key=lambda r: -int(r["score"]))
    fresh = sorted(
        (r for r in rows
         if state["seen"].get(r["key"], {}).get("first_seen", "") > last_digest),
        key=lambda r: -int(r["score"]))

    lines = [f"# Job Machine digest - {today}", ""]
    lines.append(f"## Scoreboard (deadline: signed offer by 2026-11-14)")
    lines.append(f"- applied: {len(applied)}   follow-ups due: {len(followups)}   "
                 f"open leads tracked: {len(rows)}")
    rs = _results_summary()
    lines.append(f"- machine: submitted today {rs['submitted_today']} (total {rs['submitted_total']})   "
                 f"failed {rs['failed_today']}   captcha {rs['captcha_today']}   "
                 f"manual {rs['manual_today']}   shadow {rs['shadow_today']}")
    if state.get("paused"):
        lines.append(f"- LANE PAUSED: {state['paused']}  (only Mitchell un-pauses: jobsctl resume)")
    if MANUAL_QUEUE.exists():
        mq = MANUAL_QUEUE.read_text().splitlines()
        heads = [l for l in mq if l.startswith("## ")]
        if heads:
            lines.append(f"- manual queue: {heads[-1][3:]}  -> data/private/manual-queue.md")
    if followups:
        lines.append("\n## Follow-ups due (5-7 business days, no reply)")
        for t in followups:
            lines.append(f"- {t['company']} - {t['role']} (applied {t['applied_date']})"
                         f" -> draft follow-up ready for your send")
    if awaiting:
        lines.append("\n## Awaiting your approval (packets ready)")
        for r in awaiting[:15]:
            lines.append(f"- [{r['score']}] {r['company']} - {r['title']} "
                         f"({r['comp'] or 'comp not posted'})  key={r['key']}")
    if fresh:
        lines.append(f"\n## New qualifiers since last digest ({len(fresh)})")
        for r in fresh[:args.top]:
            lines.append(f"- [{r['score']}] {r['company']} - {r['title']} | "
                         f"{r['location'][:40]} | {r['comp'] or 'comp not posted'}")
        if len(fresh) > args.top:
            lines.append(f"- ... and {len(fresh) - args.top} more in data/jobs.csv")
    if state["runs"]:
        last = state["runs"][-1]
        fails = last.get("board_failures", [])
        if fails:
            lines.append(f"\n## LOUD: {len(fails)} board failures last sweep")
            lines += [f"- {b}" for b in fails]
        lines.append(f"\n_last sweep {last['at']}: {last['passed']} passed, "
                     f"{last['new']} new, drops {last['drops']}_")
    out = "\n".join(lines) + "\n"
    (DATA / "digest-latest.md").write_text(out)
    print(out)
    if not args.dry_run:
        state["last_digest"] = now_iso()
        save_json(STATE_FILE, state)


def cmd_reject(args):
    """Judgment reject: never show this posting again (survives every sweep)."""
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}})
    state["rejected"][args.key] = {"reason": args.reason or "manual", "at": now_iso()}
    save_json(STATE_FILE, state)
    print(f"rejected {args.key} ({args.reason or 'manual'})")


def cmd_state(args):
    state = load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}})
    open_seen = {k: v for k, v in state["seen"].items() if not v.get("closed")}
    print(f"runs: {len(state['runs'])}  seen(open): {len(open_seen)}  "
          f"seen(closed): {len(state['seen']) - len(open_seen)}  "
          f"rejected(cached): {len(state['rejected'])}")
    for r in state["runs"][-5:]:
        print(f"  {r['at']}: passed={r['passed']} new={r['new']} drops={r['drops']}")



# ----------------------------------------------------- questions / queue / track

COMPANY_CAP, COMPANY_WINDOW_DAYS = 3, 7
RESULTS = PRIVATE / "results.jsonl"
QUEUE_FILE = DATA / "apply-queue.json"
MANUAL_QUEUE = PRIVATE / "manual-queue.md"


def _parse_key(key: str):
    source, slug, job_id = key.split(":", 2)
    return source, slug, job_id


def pick_master(company: str, title: str) -> str:
    """Ops/technical bucket -> A regardless of industry; coordinator -> B;
    otherwise healthcare company -> B, else A."""
    b = bucket(title)
    if b in OPS_BUCKETS:
        return "a"
    if b == "F5 HC Admin":
        return "b"
    return "b" if HEALTHCARE_HINTS.search(company or "") else "a"


def apply_url_for(source: str, slug: str, job_id: str, fallback: str) -> str:
    if source == "lever":
        return f"https://jobs.lever.co/{slug}/{job_id}/apply"
    if source == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}/{job_id}/application"
    if source == "gh":
        return f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"
    return fallback


def cmd_questions(args):
    """Greenhouse: read every form field from the job board API before any browser opens."""
    import forms
    rows = list(csv.DictReader(JOBS_CSV.open())) if JOBS_CSV.exists() else []
    keys = [args.key] if args.key else [r["key"] for r in rows if r["key"].startswith("gh:")]
    todo = [k for k in keys if args.refresh or not forms.questions_path(k).exists()]
    cached = len(keys) - len(todo)
    if args.limit:
        todo = todo[:args.limit]

    def work(k):
        _, slug, job_id = _parse_key(k)
        payload, err = forms.fetch_gh_questions(slug, job_id)
        return k, payload, err

    done, failed = 0, []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for k, payload, err in ex.map(work, todo):
            if payload is None:
                failed.append(f"{k}: {err}")
                continue
            payload["key"] = k
            payload["fetched_at"] = now_iso()
            path = forms.questions_path(k)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=1))
            done += 1
    print(f"questions: fetched={done} cached={cached} failed={len(failed)} "
          f"(lever/ashby forms are read from the DOM by apply.py)")
    for f in failed[:20]:
        print(f"  ! {f}")
    if failed and len(failed) == len(todo) and todo:
        print("  every fetch failed - if the status is 403/-1 the host is denied by the "
              "environment network policy (boards-api.greenhouse.io)")


def _load_state():
    return load_json(STATE_FILE, {"runs": [], "seen": {}, "rejected": {}, "applied": {}, "manual": {}})


def cmd_queue(args):
    """Deterministic apply queue. No model. Everything refused lands in the manual queue with a reason."""
    import forms
    from collections import Counter
    bank = forms.load_bank()
    state = _load_state()
    if state.get("paused"):
        sys.exit(f"LANE PAUSED: {state['paused']} - only Mitchell un-pauses (jobsctl resume)")
    rows = list(csv.DictReader(JOBS_CSV.open())) if JOBS_CSV.exists() else []
    rows.sort(key=lambda r: -int(r["score"] or 0))
    applied = state.setdefault("applied", {})
    manual_state = state.setdefault("manual", {})
    now = datetime.now(timezone.utc)
    recent = Counter()
    for v in applied.values():
        try:
            at = datetime.fromisoformat(v["at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if (now - at).days < COMPANY_WINDOW_DAYS:
            recent[(v.get("company") or "").lower()] += 1

    out, manual, skipped = [], [], Counter()
    for r in rows:
        key = r["key"]
        source, slug, job_id = _parse_key(key)
        company = r["company"]
        if key in applied:
            skipped["applied"] += 1
            continue
        if key in state["rejected"]:
            skipped["rejected"] += 1
            continue
        if key in manual_state and not args.retry_manual and key not in state.get("requeue", []):
            skipped["manual_already"] += 1
            continue
        if recent[company.lower()] >= COMPANY_CAP:
            skipped["company_cap"] += 1
            continue
        if r.get("age_days") and int(r["age_days"]) > MAX_AGE_DAYS:
            skipped["age"] += 1
            continue
        if comp_floor_ok(r.get("comp", "")) is False:
            skipped["comp"] += 1
            continue
        qpath = forms.questions_path(key)
        questions = json.loads(qpath.read_text()) if qpath.exists() else None
        plan = forms.plan_form(questions["questions"], bank) if questions else None
        if plan and plan["blockers"]:
            reasons = [f"{q['label'][:60]}: {why}" for q, why in plan["blockers"]]
            manual.append((r, reasons))
            manual_state[key] = {"at": now_iso(), "reasons": reasons}
            learn = [forms.learning_record(q, {"company": company, "title": r["title"], "url": r["url"]}, why)
                     for q, why in plan.get("learnable", [])]
            if learn:
                forms.add_learning(PRIVATE / "learning-queue.json", learn)
            continue
        out.append({
            "key": key, "source": source, "slug": slug, "job_id": job_id,
            "company": company, "title": r["title"], "bucket": r["bucket"],
            "score": int(r["score"] or 0), "location": r["location"], "comp": r["comp"],
            "master": pick_master(company, r["title"]),
            "url": (questions or {}).get("apply_url") or apply_url_for(source, slug, job_id, r["url"]),
            "questions": str(qpath) if questions else None,
            "free_short": [q["label"] for q in plan["free_short"]] if plan else [],
        })
        recent[company.lower()] += 1
        if len(out) >= args.cap:
            break

    state["requeue"] = [k for k in state.get("requeue", []) if k not in {q["key"] for q in out}]
    QUEUE_FILE.write_text(json.dumps(out, indent=1))
    if manual:
        MANUAL_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with MANUAL_QUEUE.open("a") as f:
            f.write(f"\n## {now.strftime('%Y-%m-%d %H:%M')} UTC - refused by the gates ({len(manual)})\n")
            for r, reasons in manual:
                f.write(f"- [{r['score']}] {r['company']} - {r['title']}  {r['url']}\n")
                for why in reasons:
                    f.write(f"    - {why}\n")
    save_json(STATE_FILE, state)
    needs_draft = sum(1 for q in out if q["free_short"])
    print(f"queue: {len(out)} queued (cap {args.cap}), {needs_draft} need a short answer drafted, "
          f"{len(manual)} to manual queue, skipped={dict(skipped)}")
    print(f"wrote {QUEUE_FILE}" + (f" and appended {MANUAL_QUEUE}" if manual else ""))


def _business_days_after(start: datetime, n: int) -> str:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.strftime("%Y-%m-%d")


def cmd_track(args):
    """Mirror submitted results into the tracker CSV (the Sheet is synced by the session)."""
    tracker = PRIVATE / "tracker.csv"
    rows = list(csv.DictReader(tracker.open())) if tracker.exists() else []
    fields = list(rows[0].keys()) if rows else [
        "date_added", "company", "role", "bucket", "location", "comp", "score", "status",
        "resume_version", "next_action", "applied_date", "followup_due"]
    if "key" not in fields:
        fields.append("key")
    have = {r.get("key") for r in rows if r.get("key")}
    have_pairs = {(r["company"].lower(), r["role"].lower()) for r in rows}
    added = 0
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if not line.strip():
                continue
            res = json.loads(line)
            if res.get("outcome") != "submitted" or res["key"] in have:
                continue
            if (res["company"].lower(), res["title"].lower()) in have_pairs:
                continue
            at = datetime.fromisoformat(res["ts"].replace("Z", "+00:00"))
            rows.append({
                "date_added": at.strftime("%Y-%m-%d"), "company": res["company"],
                "role": res["title"], "bucket": res.get("bucket", ""),
                "location": res.get("location", ""), "comp": res.get("comp", ""),
                "score": res.get("score", ""), "status": "Applied",
                "resume_version": "Master " + res.get("master", "a").upper(),
                "next_action": "follow up if no reply", "applied_date": at.strftime("%Y-%m-%d"),
                "followup_due": _business_days_after(at, 6), "key": res["key"],
            })
            have.add(res["key"])
            added += 1
    with tracker.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"track: {added} new Applied rows -> {tracker} ({len(rows)} total)")


def cmd_pause(args):
    state = _load_state()
    state["paused"] = f"{args.reason or 'manual'} at {now_iso()}"
    save_json(STATE_FILE, state)
    print(f"LANE PAUSED: {state['paused']}")


def cmd_resume(args):
    """Mitchell-only by law. The code cannot tell who is typing; the law can."""
    state = _load_state()
    was = state.pop("paused", None)
    save_json(STATE_FILE, state)
    print(f"lane resumed (was: {was})")


def _results_summary():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"submitted_today": 0, "submitted_total": 0, "failed_today": 0,
           "captcha_today": 0, "manual_today": 0, "shadow_today": 0}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            is_today = r.get("ts", "").startswith(today)
            o = r.get("outcome")
            if o == "submitted":
                out["submitted_total"] += 1
                out["submitted_today"] += is_today
            elif is_today and o in ("failed", "captcha", "manual", "shadow"):
                out[f"{o}_today"] += 1
    return out



def cmd_slugs_merge(args):
    """Merge dork-discovered boards ({slug: {source, slug}}) into data/slugs.json."""
    slugs = load_json(SLUGS_FILE, {})
    new = load_json(Path(args.file), {})
    known = {(m["source"], m["slug"].lower()) for m in slugs.values()}
    added = 0
    for name, meta in new.items():
        if (meta["source"], meta["slug"].lower()) in known:
            continue
        slugs[name] = {"source": meta["source"], "slug": meta["slug"], "via": "expand"}
        known.add((meta["source"], meta["slug"].lower()))
        added += 1
    save_json(SLUGS_FILE, slugs)
    print(f"slugs-merge: +{added} boards (now {len(slugs)}) -> {SLUGS_FILE}")



def cmd_learn(args):
    """The learning interview. No flags: print deduplicated unknown questions.
    --answer "N=value" (N from the printout) or "normalized question=value":
    promote into answers.json["learned"] and release manual-queue jobs whose
    blockers were only these questions, so the next `queue` re-evaluates them."""
    import forms
    lq = PRIVATE / "learning-queue.json"
    items = json.loads(lq.read_text()) if lq.exists() else []
    if not items:
        print("learning queue is empty")
        return
    items.sort(key=lambda d: -d.get("count", 1))
    if not args.answer:
        print(f"{len(items)} unknown question(s). Answer with: jobsctl.py learn --answer \"N=value\"")
        for i, d in enumerate(items, 1):
            opts = f"  options: {d['options'][:8]}" if d.get("options") else ""
            print(f"{i:3d}. [{d.get('count',1)}x] {d['original_question'][:90]}{opts}")
            print(f"      e.g. {d.get('company','')} / {d.get('role','')[:40]}")
        return
    bank = forms.load_bank()
    learned = bank.setdefault("learned", [])
    state = _load_state()
    promoted = 0
    for spec in args.answer:
        key, _, value = spec.partition("=")
        key, value = key.strip(), value.strip()
        if not value:
            print(f"! no value in {spec!r}"); continue
        target = None
        if key.isdigit() and 1 <= int(key) <= len(items):
            target = items[int(key) - 1]
        else:
            nk = forms.normalize_question(key)
            target = next((d for d in items if d["normalized_question"] == nk), None)
        if target is None:
            print(f"! no learning item matches {key!r}"); continue
        toks = [t for t in target["normalized_question"].split() if len(t) > 2][:6]
        pattern = r".*".join(re.escape(t) for t in toks) if toks else re.escape(target["original_question"])
        learned.append({"normalized": target["normalized_question"], "patterns": [pattern],
                        "value": value, "original": target["original_question"],
                        "learned_at": now_iso()})
        items.remove(target)
        promoted += 1
        # release manual-queue jobs that were blocked (at least in part) by this question
        for k, m in list(state.get("manual", {}).items()):
            if any(target["original_question"][:40].lower() in r.lower() for r in m.get("reasons", [])):
                state["manual"].pop(k, None)
                state.setdefault("requeue", []).append(k)
        print(f"learned: {target['original_question'][:70]!r} -> {value!r}")
    (PRIVATE / "answers.json").write_text(json.dumps(bank, indent=1) + "\n")
    lq.write_text(json.dumps(items, indent=1))
    save_json(STATE_FILE, state)
    print(f"promoted {promoted}; {len(items)} still unknown; "
          f"{len(state.get('requeue', []))} job(s) released for re-queue (run: jobsctl.py queue)")


# ----------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(prog="jobsctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    r = sub.add_parser("resolve")
    r.add_argument("companies")
    r.add_argument("--workers", type=int, default=12)
    r.add_argument("--fresh", action="store_true",
                   help="ignore cached slugs.json and re-resolve everything")
    r.set_defaults(fn=cmd_resolve)

    d = sub.add_parser("discover")
    d.add_argument("--workers", type=int, default=12)
    d.set_defaults(fn=cmd_discover)

    j = sub.add_parser("jd")
    j.add_argument("key")
    j.add_argument("--refresh", action="store_true")
    j.set_defaults(fn=cmd_jd)

    s = sub.add_parser("screen")
    s.add_argument("resume")
    s.add_argument("key")
    s.set_defaults(fn=cmd_screen)

    dg = sub.add_parser("digest")
    dg.add_argument("--top", type=int, default=20)
    dg.add_argument("--dry-run", action="store_true",
                    help="don't advance the last_digest marker")
    dg.set_defaults(fn=cmd_digest)

    tl = sub.add_parser("tailor", help="emit the tailoring brief for one job")
    tl.add_argument("key")
    tl.add_argument("--master", default="b", choices=["a", "b", "A", "B"])
    tl.set_defaults(fn=cmd_tailor)

    ck = sub.add_parser("check", help="run the no-fabrication guardrail")
    ck.add_argument("master")
    ck.add_argument("tailored")
    ck.set_defaults(fn=cmd_check)

    rd = sub.add_parser("render", help="guardrail, then render an ATS-safe docx")
    rd.add_argument("master")
    rd.add_argument("tailored")
    rd.add_argument("out")
    rd.add_argument("--force", action="store_true",
                    help="render despite guardrail violations (not advised)")
    rd.set_defaults(fn=cmd_render)

    pr = sub.add_parser("prep")
    pr.add_argument("key")
    pr.add_argument("--resume", help="override resume text file for coverage check")
    pr.set_defaults(fn=cmd_prep)

    rj = sub.add_parser("reject")
    rj.add_argument("key")
    rj.add_argument("reason", nargs="?")
    rj.set_defaults(fn=cmd_reject)

    sub.add_parser("state").set_defaults(fn=cmd_state)

    qs = sub.add_parser("questions", help="Greenhouse form fields via API -> data/jds/*.questions.json")
    qs.add_argument("key", nargs="?")
    qs.add_argument("--refresh", action="store_true")
    qs.add_argument("--limit", type=int, default=0)
    qs.add_argument("--workers", type=int, default=8)
    qs.set_defaults(fn=cmd_questions)

    qu = sub.add_parser("queue", help="deterministic apply queue -> data/apply-queue.json")
    qu.add_argument("--cap", type=int, default=60)
    qu.add_argument("--retry-manual", action="store_true",
                    help="re-evaluate jobs previously sent to the manual queue")
    qu.set_defaults(fn=cmd_queue)

    tr = sub.add_parser("track", help="results.jsonl -> tracker.csv Applied rows")
    tr.set_defaults(fn=cmd_track)

    sm = sub.add_parser("slugs-merge", help="merge expand-batch json into data/slugs.json")
    sm.add_argument("file")
    sm.set_defaults(fn=cmd_slugs_merge)

    ln = sub.add_parser("learn", help="unknown-question interview; --answer N=value promotes + requeues")
    ln.add_argument("--answer", action="append", help='"N=value" or "normalized question=value"; repeatable')
    ln.set_defaults(fn=cmd_learn)

    pa = sub.add_parser("pause")
    pa.add_argument("reason", nargs="?")
    pa.set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="Mitchell only").set_defaults(fn=cmd_resume)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
