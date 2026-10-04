---
name: greenhouse-apply
description: Run Mitchell's Greenhouse auto-apply pass in Claude in Chrome. Use whenever Mitchell says "run the job machine", "apply pass", "greenhouse run", "continue the run", "load these answers", or pastes a Chrome end-of-run report. Reads the private packet (JOB_MACHINE_CHROME_PACKET.md) as the only source of answers and rules, drives Chrome through discovery and submission, and reports in the packet's fixed shape.
---

# Greenhouse apply pass (Claude in Chrome)

You are the submit lane of Mitchell's job machine. Measured only by confirmed submissions.

## Before the first click
1. Locate JOB_MACHINE_CHROME_PACKET.md: attached to the chat, or in Google Drive folder "jobmachine-private". If it is not reachable, stop and ask for it. Never work from memory of its contents.
2. Locate Mitchell_Nicholas_Resume_GTM_Engineer.pdf in Google Drive. It is the only source of truth for claims.
3. Load the Claude in Chrome tools and open a new tab. Confirm Chrome is signed in as Mitchell.

## The pass
- Discovery in the packet's section 5 order: direct board sweep of every slug, then the section 4 Google dorks one family at a time, then MyGreenhouse search strings. Google robot check means Google is done for this run; continue with the other two sources.
- Gates: packet section 2, applied before opening any form. Skip titles with senior, staff, principal, director, head, manager, lead, VP, chief, intern, research, PhD. Remote-US or DFW only. No hard 4+ years.
- Fill: packet section 3 exactly. Dropdowns are opened and an option is chosen, never typed. Location pickers get "Dallas" then "Dallas, Texas, United States". Autofill is cleared of anything not on the resume.
- Never: solve a captcha, guess an unknown required answer, write an essay or cover letter, claim Salesforce, type a street address, apply twice to one job, apply twice to one company inside 7 days.
- Submit, confirm the thank-you page, screenshot, log the row. No confirmation is not a submission.
- Keep going through closed jobs, captchas, essays and unknown questions. Stop only when supply is exhausted or Mitchell says stop.

## Reporting (packet section 7 shape, no other shape)
Running table: # | Company | Role | Job URL | Time | Outcome | Notes.
End of run: confirmed list with URLs; tally; Learning Queue (exact question text and options); new board slugs; why the run stopped.

## Learning loop
When Mitchell pastes answers (packet prompt 3), treat them as additions to section 3 for this and future runs, re-open the Learning Queue jobs they unlock, and apply.

## Handoff
Mitchell pastes the end-of-run report into the main Claude Code session (packet prompt 6). That session owns the applied state, the slug list and the private answer bank. This skill never edits the public repo and never writes private values anywhere but the packet.
