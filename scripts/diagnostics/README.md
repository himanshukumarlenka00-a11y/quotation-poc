# Diagnostics & measurement scripts

Read-only tools built 2026-09-01 to diagnose matching accuracy and measure any
change SAFELY against real data. See the top HANDOFF.md section for the full
findings. **Golden rule: judge a matcher change by CORRECTNESS (eyeball
request → picked product), never by not_found count alone.**

## The isolated-copy pattern (never touch the live prod DB)
Importing the app runs `init_db()` (db.py) + `_bootstrap_admin()` (auth.py), so
never point it at the live prod DB. Instead, on the server:

```
# one-time per run — make isolated copies in /tmp
ssh melange@192.168.0.146 'mkdir -p /tmp/qg_baseline &&
  cp /srv/quotegen-data/quotations.db /tmp/qg_baseline/ &&
  cp /srv/quotegen-data/semantic_index.npz /tmp/qg_baseline/ &&
  rm -rf /tmp/qg_app && mkdir -p /tmp/qg_app &&
  cp -r /opt/quotegen/app /tmp/qg_app/app'
# (to test a code change: scp the changed file into /tmp/qg_app/app/... and
#  clear __pycache__ before running)

# run a script against the copy, using the prod venv:
ssh melange@192.168.0.146 'cd /tmp/qg_app &&
  APP_DIR=/tmp/qg_app DATA_DIR=/tmp/qg_baseline /opt/quotegen/venv/bin/python - <ARGS>' < SCRIPT.py

# clean up afterwards
ssh melange@192.168.0.146 'rm -rf /tmp/qg_baseline /tmp/qg_app'
```

All scripts monkeypatch `app.routers.quotations._llm_chat` to "" so they measure
the deterministic matcher only (no LLM spend).

## Scripts
| File | What it does |
|---|---|
| `boq_rerun.py` | Replay a REAL saved client BOQ (`/srv/quotegen-data/boq_sources/*.xls[x]`) through the real matcher; prints matched vs not_found and a request→pick JSON for before/after diffing. THE definitive test for any matcher change. Arg: BOQ file path. |
| `baseline_harness.py` | Builds a regression set (currently-matching lines, must stay stable) + a should-fix set from the DB; snapshots picks so a later run flags anything a change altered. |
| `suggest_quality.py` | For a real BOQ, measures whether `suggest_products` (Find-panel candidates) surfaces the right product in its top-6 for findable not_found lines. (Result: already ~90%.) Arg: BOQ file path. |
| `classify_notfound.py` | Classifies not_found reasons from the stored not_found lists: bare label vs model-gate vs price. Needs `DATA_DIR`. |
| `master_audit.sql` | Master-data health: missing price/HSN/GST/cost/image/brand, dirty names, duplicate-name groups, per-catalogue. Run: `sqlite3 "file:.../quotations.db?mode=ro" < master_audit.sql`. |
| `master_dirty.py` | Shows the 591 dirty product names (newline/double-space) with a before→after cleanup preview. Read-only. |
| `master_clean.py` | Applies the whitespace-only name cleanup to `TARGET_DB` (an isolated copy!). SAFETY: skips any row whose non-whitespace chars would change. Tested (591/591, 0 unsafe); NOT yet applied to prod. |
| `test_llm_wiring.py` | Verifies the Claude/Groq `_llm_chat` wiring with a dummy key + faked call — no real API spend. |
