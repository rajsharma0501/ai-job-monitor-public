# AI Job Monitor

Polls company career boards across several ATS platforms, scores postings for
seniority and AI/ML domain fit, and surfaces matches on a local dashboard.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Runs locally, notifies nothing.** There is no cloud cron, no Telegram, and no
email. Output is the dashboard and the log — you look, it does not push.

## Features

- **Scoring (0–100)** — seniority, AI/ML domain fit, role type, location, and
  company weighting, combined per posting. Word-boundary matching, not
  substring, so "ml" does not match "html".
- **API-based extraction** — Ashby, Greenhouse, Lever, Workday, SmartRecruiters
  and Workable through their own endpoints, plus custom handlers for Amazon,
  Netflix and Microsoft. HTML scraping is the fallback, not the default.
- **Stable deduplication** — a job is identified by a hash of company, title and
  URL, so re-running the same day correctly finds nothing new.
- **Circuit breaker** — a board that fails repeatedly is suppressed for a
  cooling-off period rather than retried every cycle.
- **Retry with backoff and per-domain rate limiting** on every request.
- **Local dashboards** — a filterable job history, and an observability view
  with per-run metrics, source health, and a log tail.
- **ATS discovery** — `discovery.py` resolves a company name to a working board
  URL, verifying every candidate against the live endpoint.

## Setup

```bash
pip install -r requirements.txt
```

Python 3.11 or newer. Three dependencies: `requests`, `beautifulsoup4`, `lxml`.
This project uses plain `pip` and `requirements.txt` — not `uv`.

Copy a config into place and add the companies you want to watch. `config.json`
is gitignored, along with all job data, because it holds your own search
criteria.

```bash
python -c "import shutil; shutil.copyfile('config.template.json', 'config.json')"
```

The template deliberately starts with an empty company list. Use discovery to
verify a board, then add an entry to `companies` with `name`, `url`, `priority`
(such as `medium`), and `enabled` (true). Set your own location and keyword
filters before scanning.

Scoring uses hand-written rules, not a trained model or an LLM. This project
demonstrates API integration, concurrent collection, durable deduplication,
failure recovery, and operational visibility for a personal workflow.

The dashboard listens only on the local computer. It has no authentication
and should not be exposed as a hosted service. Keep configuration, logs,
reports, and job history private; use synthetic data for public screenshots.

On Windows, set `PYTHONUTF8=1` — the console encoder cannot handle the emoji in
log output.

## Usage

```bash
python job_monitor.py --dry-run --once   # test cycle, writes nothing
python job_monitor.py --once             # one real cycle
python job_monitor.py                    # continuous, default interval
python job_monitor.py --interval 15      # continuous, every 15 minutes
python job_monitor.py --validate-slugs   # probe every configured board, report dead ones

python serve.py                          # dashboard at http://localhost:8765
python discovery.py "Company Name"       # find a company's ATS
python view_history.py --no-open         # regenerate the history page only

python -m unittest discover              # run the tests
```

## Documentation

| Question | File |
|---|---|
| Extractors, scoring, dedup, data flow | `docs/architecture.md` |
| Commands, state files, adding a company | `docs/operations.md` |
| Why a choice was made, and what is known broken | `docs/decisions.md` |
| How to update these docs | `docs/writing-docs.md` |

## Troubleshooting

- **A board returns 404** — the ATS slug is wrong or the company migrated. Run
  `discovery.py` for that company; do not guess a slug.
- **A board returns 422** — usually a Workday URL missing its board name.
  `discovery.py`'s careers-page layer recovers these.
- **No new jobs on a second run** — expected. Deduplication is hash-based, so
  nothing is new unless a board actually posted something.
- **Unicode errors on Windows** — set `PYTHONUTF8=1`.

See `docs/decisions.md` for boards with known permanent failures.

## License

MIT — see `LICENSE`.
