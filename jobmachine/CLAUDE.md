# Job Machine — operating rules

End-to-end job application machine. Goal: signed offer before **September 24, 2026**.
Pipeline: CONFIG → DISCOVER → SCREEN → TAILOR → SUBMIT → OUTREACH → TRACK → FOLLOW-UP → INTERVIEW PREP.

## Locked config

- **Two tracks.** Master A "GTM Engineer" (GTM eng, growth ops, RevOps, AI ops, marketing ops). Master B "Healthcare GTM" (healthcare SDR/BDR, CS, implementation, onboarding, training & development, med/diagnostic inside sales).
- **Comp floor:** $70K minimum base; OTE counts if $105K+.
- **Location:** Remote-US, or DFW metro (Dallas, Irving, Fort Worth, Plano, Richardson and neighbors). Foreign-remote excluded. No sponsorship needed (US citizen).
- **Start:** ASAP. **Sniper over volume** — tailored applications only, no spray.
- **Auto-skip:** commission-only, door-to-door (non-medical), staffing-agency ghost posts, roles below the comp floor, titles requiring 4+ hard years.
- **Postings up to 60 days old are in play** — older postings get the outreach lane (find the poster, cold email / LinkedIn DM) instead of a cold application.
- **Applications go to the company's own ATS** (Greenhouse / Lever / Ashby / company careers page). LinkedIn Easy Apply is out of scope entirely. Indeed skipped.

## Kill criteria (Mitchell is the only one who can un-pause)

1. Any fabricated claim found in any output → lane pauses instantly.
2. Any duplicate application to the same company → lane pauses.
3. Any LinkedIn warning or captcha wall → that lane pauses.

## Truth rules (law)

- No invented tools, titles, metrics, or certifications. Zero Salesforce stays zero until Trailhead is actually completed — then it is one true line.
- "Deployed", not "in production", until tested. The $300K pipeline claim needs its breakdown written before any interview.
- Education line is always **"Bachelor of Science (B.S.), Healthcare Studies"** spelled out — bare "B.S." fails keyword screens (paid-for lesson).
- Resume truth base: the two master resumes in Google Drive (Sep 4, 2026 versions). Nothing beyond their claims may appear in any variant, answer, or cover letter.

## Shadow mode (law)

Nothing is ever submitted or sent without Mitchell's explicit approval:
- Applications: `prep` produces a review packet (filled-form screenshot + resume version + answers + cover letter). Submit happens only after Mitchell approves that packet.
- Outreach: emails are created as Gmail **drafts**; LinkedIn DMs as text. Mitchell sends everything himself.
- Automation adoption sequence: manual → criteria → gated automation → schedule. Nothing gets scheduled that hasn't run reliably by hand.

## Engineering rules

- Deterministic gates before model judgment: reality gate (location/years/comp) FIRST, then keyword coverage, then council judgment. HTTP 200s and coverage percentages can't be sweet-talked.
- Every component fails loud (`doctor`, per-board failure lists). Silence is the killer.
- Slug resolution false-merges ("boston" ≠ Boston Scientific): any `[VERIFY]`-flagged board's sample titles must be reviewed before its jobs are trusted.
- Seen/rejected state lives in `data/graph-state.json`; sweeps never re-surface prior rejects.
- Keys and credentials never travel through chat or git. Env vars only.
- **This repo is public.** Nothing personal is committed: resumes, phone, answer bank, tracker, and any scraped third-party contact info live in gitignored `data/private/`. Cloud copies: resumes + tracker in Mitchell's Google Drive.
- Reuse over rebuild: JobSpy for discovery widening, Scrapling (this repo) for fetching/crawling, Playwright for form pre-fill. Write glue, not wheels.

## Style rules

- No em dashes in anything Mitchell-voiced (cover letters, emails, DMs). It's an AI tell.
- Cover letters are story-driven (what he's building, why this company) — never template slop. Only when required or for 75+ scored roles.
- Faith stays out of client/employer-facing artifacts.

## Runbook

```
python3 jobmachine/jobsctl.py doctor            # verify all three ATS lanes
python3 jobmachine/jobsctl.py resolve jobmachine/companies.txt
python3 jobmachine/jobsctl.py discover          # -> data/jobs.csv (gated + ranked)
python3 jobmachine/jobsctl.py jd gh:headway:123 # fetch + cache one JD
python3 jobmachine/jobsctl.py screen data/private/resume_master_b.txt gh:headway:123
python3 jobmachine/jobsctl.py state             # run log / seen / rejected counts
```

Note: this cloud sandbox's egress policy blocks the ATS API hosts; sweeps run on
the Composio remote workbench or locally until the environment network policy is
widened (boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com).

Tracker of record: Google Sheet "Job Applications Tracker — Mitchell"
(mirrored locally at `data/private/tracker.csv`). Airtable is Hostra's — never used here.
