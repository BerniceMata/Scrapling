# ROUTINE.md - the exact prompt the weekday Routine runs

Fired twice a weekday (7:05am and 1:05pm CT) into a fresh session on this
environment, model Sonnet. Everything below is one run. Stop at the first step
that fails loud; never skip a gate to keep going.

---

You are running the Job Machine v3 unattended apply loop for Mitchell Nicholas.
Repo: BerniceMata/Scrapling, branch `claude/job-machine-code-handoff-pjzij6`,
directory `jobmachine/`. Read `jobmachine/CLAUDE.md` first; its laws bind you.
Never fabricate; never solve a CAPTCHA; never submit anything the gates refuse.
If `data/graph-state.json` has `paused`, send the digest and stop.

1. RESTORE private state from Mitchell's Google Drive into `jobmachine/data/private/`
   and `jobmachine/data/` (these are gitignored, the clone has none of them).
   Drive file IDs are listed in `jobmachine/bootstrap_workbench.sh`:
   answers.json, resume_master_a.json, resume_master_b.json, tracker.csv,
   results.jsonl, manual-queue.md, slugs.json, graph-state.json, and the two
   master PDFs into `jobmachine/resumes/`. Missing results.jsonl or
   manual-queue.md is fine on a first run; a missing answers.json or master is not.
   `pip install -q playwright` (Chromium is already in /opt/pw-browsers).

2. DOCTOR: `python3 jobsctl.py doctor`. Any FAIL names the host; email the
   digest with that line and stop.

3. EXPAND (session step, needs no network change): run the six family dork
   strings from `data/private/dorks.txt` through WebSearch, each crossed with
   nothing / "remote" / "Dallas". Parse every result URL for
   `job-boards.greenhouse.io/<slug>/`, `jobs.lever.co/<slug>/`,
   `jobs.ashbyhq.com/<slug>/` and merge new slugs into `data/slugs.json` as
   `{"<slug>": {"source": "gh|lever|ashby", "slug": "<slug>"}}` (the company
   name is the slug until discover fills it). Report how many were new.

4. DISCOVER: `python3 jobsctl.py discover`. Report passed / NEW / board failures.

5. QUESTIONS: `python3 jobsctl.py questions`.

6. QUEUE: `python3 jobsctl.py queue --cap 60` (100 once Mitchell raises it).

7. SCREEN + ANSWER (session steps, Haiku subagents, one per queued job that has
   `free_short` labels or a score under 75):
   - Fit screen: give Haiku the cached JD (`data/jds/<key>.txt`) and the chosen
     master's summary and bullets (`data/private/resume_master_<a|b>.json`).
     It returns JSON only: `{"fit": 0-100, "hard_blockers": [], "note": ""}`.
     Hard blockers are things the JD requires that the master does not have
     (a license, 4+ hard years, a named tool at expert level, on-site in a
     non-Texas city). fit < 65 or any blocker: remove the job from
     `data/apply-queue.json` and append it to `data/private/manual-queue.md`
     with the blocker.
   - Short answers: for each `free_short` label, Haiku writes <= 90 words in
     Mitchell's voice from `answers.json` narratives + tools_truth + the JD.
     No em dashes. No number that is not in the master. Name the company's
     own mechanism from the JD. Then run
     `python3 -c "import json,guardrail as g; ..."` -> `guardrail.check_text`
     against the master. One regenerate on failure; a second failure sends the
     job to the manual queue. Save passing answers to
     `data/private/answers-<key with : as __>.json` as `{label: text}`.
   - Three check_text failures in one run: `python3 jobsctl.py pause "3 guardrail failures"` and stop.

8. APPLY: `python3 apply.py --queue data/apply-queue.json`. Read every line it
   prints. It pauses itself on 10+ captchas or a failure rate above 30%.

9. TRACK: `python3 jobsctl.py track`. Then append the new Applied rows to the
   Google Sheet "Job Applications Tracker — Mitchell"
   (`1wFkd4vwlVPeVOo_m9K6iaKUMyH8UmY6EU3gTWDSgnrM`) with the same columns.

10. PERSIST: upload back to Drive, replacing the same file IDs: tracker.csv,
    results.jsonl, manual-queue.md, slugs.json, graph-state.json. Never commit
    anything under data/private/ or resumes/ to git. Commit and push only code
    changes, if any.

11. DIGEST: `python3 jobsctl.py digest`. Then scan mitchelletnicholas@gmail.com
    for the last 24h: subjects matching "thank you for applying", "application
    received", "we received your application" (count them and reconcile against
    today's submitted count: list any company submitted today with no ack),
    and anything that reads like a recruiter reply or interview invite (list
    each with sender, company, and the ask). Email the digest to
    mitchelletnicholas@gmail.com with subject
    "Job Machine - <date> - <submitted today> submitted, <manual> manual, <interviews> replies".
    If an interview invite arrived, put it at the top.

Report in this session: one screen, the same digest. No decisions asked of
Mitchell except the manual queue, which he clears when he wants.
