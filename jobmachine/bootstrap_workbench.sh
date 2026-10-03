#!/usr/bin/env bash
# Rebuild the jobmachine working copy on a fresh (or wiped) remote workbench.
# The workbench's /mnt/files mount is NOT reliably persistent across sandbox
# recycles (learned Sep 5: it came back empty), so every run starts from the
# cloud homes: code from GitHub, artifacts from Google Drive, tracker from the
# Sheet. Run this first in any new sandbox; it is idempotent.
#
# Drive artifact IDs (owner mitchell5139). The scheduled session restores these
# into data/ and data/private/ at the start of every run and persists the
# mutable ones back at the end. Private files never enter this public repo.
#   tracker Sheet            1wFkd4vwlVPeVOo_m9K6iaKUMyH8UmY6EU3gTWDSgnrM  (Google Sheet)
#   resume PDF (A, Sep 15)   192wqXYFtggxVXOA1QxDmGHXOFC27ZO0Q  -> resumes/Mitchell_Nicholas_Resume_GTM_Engineer.pdf
#   resume PDF (B, Sep 5)    1C3cPCGO725iPWak_xM-6M3d0zGEo503b  -> resumes/Mitchell_Nicholas_Resume_Healthcare_GTM.pdf
#   slugs.json               1uNVMgpmTcgc8YS7sB1mAgdhdf-5_rwMD  (mutable)
#   graph-state.json         1V_qbXDjI2SQlPaQo_1RyuRhIfUnznEJ2  (mutable)
#   answers.json, resume_master_a.json, resume_master_b.json, tracker.csv,
#   results.jsonl, manual-queue.md: uploaded to the "jobmachine-private" Drive
#   folder by the first persist; their IDs are appended below by that run.
# Drive downloads: Google Drive MCP download_file_content in-session, or the
# Composio GOOGLEDRIVE_DOWNLOAD_FILE tool from the workbench helper.
set -euo pipefail
BR="claude/job-machine-code-handoff-pjzij6"
RAW="https://raw.githubusercontent.com/BerniceMata/Scrapling/$BR/jobmachine"
ROOT="${1:-/mnt/files/jobmachine}"
mkdir -p "$ROOT/data/private" "$ROOT/data/jds" "$ROOT/data/packets" "$ROOT/resumes"
cd "$ROOT"
for f in jobsctl.py forms.py apply.py guardrail.py render_pdf.py companies.txt; do
  curl -sS -f "$RAW/$f" -o "$f"
done
# Browser for autofill (ephemeral, ~40s):
#   pip install -q playwright && \
#   PLAYWRIGHT_BROWSERS_PATH=$HOME/pw-browsers python3 -m playwright install chromium --only-shell
python3 jobsctl.py doctor || true
echo "bootstrap done in $ROOT - restore slugs/graph-state/resumes from Drive next"
