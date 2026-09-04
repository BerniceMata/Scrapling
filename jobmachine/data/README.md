# Data artifacts

This repo is public, and this cloud sandbox's egress policy blocks the ATS API
hosts, so sweep artifacts are not committed here. They live in two places:

1. **Composio remote workbench** (execution home): `/mnt/files/jobmachine/data/`
   — where `resolve` and `discover` actually run.
2. **Mitchell's Google Drive** (cloud home, uploaded after each sweep):
   - `jobmachine-slugs.json` — resolved board map (file id `1uNVMgpmTcgc8YS7sB1mAgdhdf-5_rwMD`)
   - `jobmachine-jobs.csv` — latest gated + ranked sweep (file id `1qtRtRXe0Wx1Gnj7K5zDtayS303yUd4UO`)
   - `jobmachine-graph-state.json` — run log + seen sets (file id `1V_qbXDjI2SQlPaQo_1RyuRhIfUnznEJ2`)

Sweep of Sep 4, 2026: 270 companies, 128 boards resolved, 10,165 postings read,
317 passed gates. md5 (first 8): slugs afa30f5f · jobs 83e2f47b · state bddc3fa8.

**To regenerate from scratch on any machine with open network** (~5 min, free
public APIs, no auth):

```
python3 jobsctl.py doctor
python3 jobsctl.py resolve companies.txt --workers 20
python3 jobsctl.py discover --workers 20
```

`data/private/` (gitignored): Mitchell's resume texts, tracker.csv mirror,
answer bank. Cloud home for those is his Google Drive / the tracker Sheet.
