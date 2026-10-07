# Operations

How to run the monitor, where its state lives, and how to add a company.

## Setup

```bash
pip install -r requirements.txt
```

Python 3.11+. Plain `pip` and `requirements.txt` — this project does not use
`uv`. On Windows set `PYTHONUTF8=1`; the console encoder cannot handle the
emoji in log output.

## Running

```bash
python job_monitor.py --dry-run --once   # test cycle, writes nothing
python job_monitor.py --once             # one real cycle
python job_monitor.py                    # continuous, default interval
python job_monitor.py --interval 15      # continuous, every 15 minutes
python job_monitor.py --validate-slugs   # probe every configured board, report dead ones
python job_monitor.py --config other.json
```

There is no alerting. A run writes to the report, the log, and the metrics
file, and that is all.

## Dashboards

```bash
python serve.py     # http://localhost:8765
```

- `/` — job history, filterable, with a run trigger
- `/obs` — per-run metrics, source health, parked companies, log tail

To run it in the background on Windows:

```powershell
Start-Process python -ArgumentList "serve.py" -WindowStyle Hidden
```

Stop it by finding the listener on its port (`netstat -ano | findstr :8765`)
and killing that PID.

`view_history.py --no-open` regenerates the history page without opening a
browser or running a scan.

## State and data

None of these are in the repo — all are gitignored, because they hold your own
search criteria and history.

| File | Holds |
|---|---|
| `config.json` | companies, keywords, filters, expiry windows |
| `job_state.json` | dedup state, keyed by job hash |
| `history.json` | append-only log of every matched job |
| `jobs.db` | SQLite mirror of the history |
| `logs/metrics.jsonl` | one record per run — duration, counts, open circuits |
| `logs/job_monitor.log` | rotating log; warnings and above also go to console |
| `reports/history.html` | every job ever matched, filterable |
| `reports/jobs_latest.html` | new jobs from the most recent run only |

Backups of any of these (`*.bak`, `config.json.*`) are gitignored too — the
base rule for `config.json` does not cover its own backups, which is a mistake
worth not repeating.

## Adding a company

1. **Check it hires where you are looking.** A board with no relevant roles is
   noise every run.
2. **Resolve the ATS** with `discovery.py "Company Name"`. Never paste a slug
   that has not answered a live call — a slug present in the public database is
   frequently dead.
3. **Add it to `companies[]`** in `config.json`.

If the company is on an unsupported platform — iCIMS, Oracle Taleo, SAP
SuccessFactors, Avature, Phenom People — put it in
`unsupported_ats.companies[]` instead of deleting it, so it is not
re-researched later. It must stay out of `companies[]`: the run loop reads a
URL from every entry there unconditionally, so a placeholder would misattribute
fetch errors to the wrong company.

## Parking a company

Set `"enabled": false` with a `"disabled_reason"` and it is skipped at the top
of the run loop. Re-enabling is a one-word change.

Parked and unsupported companies are emitted to the metrics file each run and
feed the dashboard's parked view. They are deliberately excluded from the
"never reported a job" list — a disabled board cannot report a job, and listing
it there reads as a scraper failure rather than a choice.
