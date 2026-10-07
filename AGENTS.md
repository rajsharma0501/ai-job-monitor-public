# ai-job-monitor

Polls company career boards across several ATS platforms, scores postings for
fit, and surfaces matches on a local dashboard. Single-purpose tool for one
person's job search — not a service.

Alerting is off. There is no cloud cron and no notification channel; output is
the local dashboard and the log.

## Run it

```bash
pip install -r requirements.txt      # plain pip — this project does not use uv
python job_monitor.py --dry-run --once   # test cycle, writes nothing
python job_monitor.py --once             # one real cycle
python serve.py                          # dashboard at http://localhost:8765
```

`PYTHONUTF8=1` is required on Windows — the console encoder cannot handle the
emoji in log output.

## Where to look

| Question | File |
|---|---|
| Extractors, scoring, dedup, data flow | `docs/architecture.md` |
| Commands, state files, dashboard, adding a company | `docs/operations.md` |
| Why a choice was made, and what is known broken | `docs/decisions.md` |
| Where a new fact belongs, and how to update these docs | `docs/writing-docs.md` |
| Feature list and quick start | `README.md` |

## Rules

- **Never commit job-search data.** `config.json` and its backups hold the
  company list and filter criteria; `history.json`, `jobs.db` and
  `job_state.json` hold every job ever matched. All are gitignored. This repo
  has a public remote — check `git status` before staging.
- **These docs describe the mechanism, not the target list.** Company names,
  score distributions, and filter criteria live in `config.json`, which is
  deliberately not in the repo. Do not copy them into a doc.
- **Verify a slug before adding a company.** Presence in the slug database is
  not proof a board answers; `discovery.py` gates every layer behind a live
  call for that reason.
- **Never state a count.** Company totals and job counts change every run.
  Read `config.json` or the dashboard instead of writing a number down.
- **Plain `python`, not `uv run`** — unlike the resume project, this one uses
  `pip` and `requirements.txt`.

## Before committing

```bash
python tools/docs_lint.py
```

Checks that these docs still describe the code: referenced paths exist, the
module inventory matches both ways, this file stays within its budget, and no
doc states a count that will rot.
