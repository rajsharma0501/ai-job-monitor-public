# Architecture

A single-file monitor. `job_monitor.py` holds one class, `JobMonitor`, and
everything below happens inside it. The other modules are tools that read what
it writes.

## One run cycle

`run_once()`:

1. Load config and environment.
2. Load `job_state.json`, expiring entries older than the configured window.
3. For each company: extract postings, filter by keyword and location, score
   what survives.
4. Append new matches to `history.json` and `jobs.db`.
5. Save state after each company, so an interrupted run keeps its progress.
6. Write the HTML report and a metrics record.

Companies are grouped by ATS and fetched in parallel across groups, which keeps
one slow board from stalling the rest.

## Extraction

Each ATS gets its own extractor, selected by the shape of the company's URL in
config. They talk to the platform's own endpoint rather than scraping HTML:

| ATS | How it is reached |
|---|---|
| Ashby | GraphQL POST |
| Greenhouse | REST GET |
| Lever | REST GET |
| Workday | POST with pagination, via a `workday://subdomain/wd_number/board` URI |
| SmartRecruiters | paginated GET |
| Workable | REST POST |
| Netflix / Eightfold | paginated GET, via a `netflix://` URI |
| Microsoft | paginated GET, via a `microsoft://` URI |
| Amazon | its own search endpoint |
| own careers site | REST GET against the site's own JSON, via a `custom://slug` URI |
| anything else | BeautifulSoup HTML scraping, as a fallback |

The custom URI schemes exist because those platforms need more than a URL — a
Workday board is identified by subdomain, tenant number *and* board name, and a
missing board name is the usual cause of a 422.

Some companies run no standard ATS board at all; their careers page is
JavaScript that fetches its listings from the company's own JSON endpoint.
`custom_boards.py` holds one small fetcher per such site, keyed by the slug in
its `custom://` URI. Calling the same JSON the page calls keeps these boards on
plain HTTP — a headless browser would cost a Chromium dependency and turn a
sub-second fetch into seconds per board, and would break on every restyle
rather than only on a real API change. Each fetcher receives its HTTP call as an
argument, so the monitor injects the instrumented client and these boards get
the same throttling, retries and circuit breaking as any other ATS.

## Scoring

A posting scores out of 100, combining seniority, AI/ML domain fit, role type,
location, and a company weighting. Matching uses word boundaries rather than
substrings, so a token like `ml` cannot match inside `html`.

Location matching uses the configured keywords; an empty filter accepts all
locations, and unknown locations remain available for manual review.

Before scoring, an exclusion list drops titles that are clearly not the target
role — security, non-ML engineering — so they never reach the scorer at all.

The weights and thresholds live in `job_monitor.py`. Read them there; they are
tuned by hand and change more often than this file does.

## Deduplication

For every API-based board — which is almost all of them — dedup is purely
hash-based, with no time component:

```
job_id = md5("{company}:{title}:{url}")
```

A job is new if and only if that id is absent from `job_state.json`. Once seen,
it stays for the configured expiry window.

**A second run on the same day finding nothing is correct**, not a bug. Nothing
is new unless a board actually posted something between runs.

Job age and last-run time *are* consulted, but only on the generic HTML
scraping path. For JSON API companies they are never called.

State format migrates on read: an older list-of-ids layout is converted to the
current dict form automatically, so an old `job_state.json` is safe to keep.

## Resilience

A board that fails repeatedly trips a circuit breaker, which suppresses it for
a cooling-off period rather than retrying every cycle. The breaker lives in
memory only — each run starts with every circuit closed.

Every request retries with exponential backoff, and requests to the same domain
are spaced out. That spacing is per domain, which is why work is parallelized
across boards rather than within one: overlapping two requests to the same ATS
buys nothing, because the throttle serializes them anyway.

## Instrumentation

`observability.py` owns everything that measures the run rather than doing it:
the logger, the per-domain throttle, the circuit breaker, the fetch-error list
and the metrics writer. It knows nothing about job boards.

Its shared collections are cleared in place and never rebound. A caller writing
`observability._circuit = {}` would rebind only its own name and leave the
recording functions mutating an object nobody else can see — errors would then
vanish with no crash and no log line. Use the `reset_*` helpers instead.

Each run records where its time went: one entry per phase, per-ATS-group totals,
and the slowest boards. Phases are wall-clock and overlap, so they do not sum to
the run total.

## Company discovery

`discovery.py` resolves a company name to a working board, through four layers
in order. Every layer is gated by a live call that rejects anything not
answering with jobs — **presence in a slug database is not proof a slug works**.

1. **Slug database** — a public company-to-ATS index, cached locally. Fast and
   frequently wrong.
2. **Probe** — brute-forces slug variants across platforms. Expensive, so
   opt-in behind a flag.
3. **Careers page** — fetches the company's own careers URLs concurrently,
   follows redirects, and reads the ATS off the final URL. This is the layer
   that recovers Workday board names, which the slug database omits.
4. **Web search** — last resort, frequently rate-limited.

Batch mode runs companies concurrently over a shared session. Do not redirect
`sys.stdout` inside a worker to quiet it — that is process-global and races.
Redirect once in the calling thread instead.

## The tools around it

| Module | Role |
|---|---|
| `job_monitor.py` | the monitor itself; extraction, scoring, dedup, the run cycle |
| `observability.py` | logging, per-domain throttle, circuit breaker, run metrics |
| `report_safety.py` | escape board text and restrict report links to HTTP and HTTPS |
| `discovery.py` | resolve a company to an ATS board; standalone, not imported |
| `custom_boards.py` | per-site fetchers for companies with no standard ATS board |
| `serve.py` | dashboard HTTP server, and the endpoints the pages call |
| `view_history.py` | regenerate the history page without running a scan |
| `observability.html` | per-run metrics, source health, log tail |

`serve.py` also exposes a run-trigger endpoint whose status payload reports
whether a run is in progress, its exit code, how many new jobs it found, and a
tail of the log on failure — which is what lets the dashboard show a red error
rather than silently doing nothing.
