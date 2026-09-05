#!/usr/bin/env bash
# Rebuild the jobmachine working copy on a fresh (or wiped) remote workbench.
# The workbench's /mnt/files mount is NOT reliably persistent across sandbox
# recycles (learned Sep 5: it came back empty), so every run starts from the
# cloud homes: code from GitHub, artifacts from Google Drive, tracker from the
# Sheet. Run this first in any new sandbox; it is idempotent.
#
# Drive artifact IDs (jobmachine-*, owner mitchell5139):
#   slugs.json        1uNVMgpmTcgc8YS7sB1mAgdhdf-5_rwMD
#   graph-state.json  1V_qbXDjI2SQlPaQo_1RyuRhIfUnznEJ2
#   resume PDF (B)    1UjhL9H4BS76lwoU0AgAw9zWHBx4pRnHX
#   resume PDF (A)    1DF6nhyTuuJZD43cy0PqQp1d0FsxTsgp8
# Drive downloads require the Composio GOOGLEDRIVE_DOWNLOAD_FILE tool (returns
# an s3url) - run those from the workbench Jupyter helper, not plain bash.
# answers.json and tracker.csv are private: restore from the Claude session's
# local copy or the Google Sheet, never from this public repo.
set -euo pipefail
BR="claude/job-machine-code-handoff-pjzij6"
RAW="https://raw.githubusercontent.com/BerniceMata/Scrapling/$BR/jobmachine"
ROOT="${1:-/mnt/files/jobmachine}"
mkdir -p "$ROOT/data/private" "$ROOT/data/jds" "$ROOT/data/packets" "$ROOT/resumes"
cd "$ROOT"
for f in jobsctl.py autofill.py companies.txt; do
  curl -sS -f "$RAW/$f" -o "$f"
done
# Browser for autofill (ephemeral, ~40s):
#   pip install -q playwright && \
#   PLAYWRIGHT_BROWSERS_PATH=$HOME/pw-browsers python3 -m playwright install chromium --only-shell
python3 jobsctl.py doctor || true
echo "bootstrap done in $ROOT - restore slugs/graph-state/resumes from Drive next"
