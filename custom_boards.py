#!/usr/bin/env python3
"""Extractors for career sites that have no standard ATS board.

Some companies run their own careers front-end instead of a Greenhouse/Lever/
Ashby/Workday board. Their pages are JavaScript-rendered, so fetching the HTML
returns an empty shell and the generic scraper finds nothing — which is why
these companies sat disabled in config.json.

They are not actually unreachable. Each page's JavaScript calls a plain JSON
endpoint to get its listings, and that endpoint can be called directly. This
module holds one small function per site, each hitting that endpoint with
urllib and normalising the result.

Why not a headless browser
--------------------------
Driving real Chromium would work, but it costs a ~150MB dependency, turns a
200ms fetch into 5-15 seconds per board, and re-breaks every time a site
restyles its markup. Calling the same JSON the page calls is faster, has no
dependency beyond the standard library, and only breaks if the company changes
its API — which is rarer, and fails loudly with a 404 rather than silently
returning an empty list.

Finding a new site's endpoint
-----------------------------
Open the careers page in a browser with devtools on the Network tab, filter to
XHR/Fetch, and look for the request returning the listings as JSON. Confirm it
works unauthenticated (`curl` it with just a User-Agent), then add a function
here and register it in BOARDS.

Usage:
    python custom_boards.py              # fetch every board, summarise
    python custom_boards.py atlassian    # fetch one, list India roles
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

TIMEOUT = 30
HEADERS = {
    # Several of these endpoints return a stub or 403 for a default urllib agent.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json",
}


def _get_json(url: str) -> dict | list:
    """Default fetcher: plain urllib, so this module runs with no project imports."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


# Every fetcher takes its HTTP call as an argument rather than calling urllib
# directly. Standalone runs get the default above; job_monitor passes its own
# instrumented version so these boards get the same polite delays, retries,
# circuit breaking and error recording as every other ATS.


def fetch_atlassian(get_json=_get_json) -> list[dict]:
    """Atlassian — own careers front-end over an iCIMS portal."""
    postings = get_json("https://www.atlassian.com/endpoint/careers/listings")
    return [
        {
            "title": posting.get("title", ""),
            # Multi-location posts list every office; join them so an India
            # office is still visible when it is not the first entry.
            "location": " / ".join(posting.get("locations") or []),
            "url": (posting.get("portalJobPost") or {}).get("portalUrl", ""),
        }
        for posting in postings
    ]


def fetch_intuit(get_json=_get_json) -> list[dict]:
    """Intuit — Radancy TalentBrew, which returns rendered HTML inside JSON."""
    from bs4 import BeautifulSoup  # already a project dependency

    jobs, page = [], 1
    while page <= 10:  # ~480 roles at 100/page; the cap stops a paging loop
        url = (
            "https://jobs.intuit.com/search-jobs/results?ActiveFacetID=0"
            f"&CurrentPage={page}&RecordsPerPage=100&Distance=50&RadiusUnitType=0"
            "&SearchType=5&SortCriteria=0&SortDirection=0"
            "&SearchResultsModuleName=Search+Results&SearchFiltersModuleName=Search+Filters"
        )
        soup = BeautifulSoup(get_json(url).get("results", ""), "html.parser")
        anchors = soup.select('li a[href*="/job/"]')
        if not anchors:
            break
        for anchor in anchors:
            location = anchor.select_one(".job-location")
            jobs.append({
                "title": (anchor.select_one("h2") or anchor).get_text(" ", strip=True),
                "location": location.get_text(strip=True) if location else "",
                "url": "https://jobs.intuit.com" + anchor.get("href", ""),
            })
        page += 1
    return jobs


# Registered boards. The key is what a config.json entry would name:
# "custom://atlassian".
BOARDS = {
    "atlassian": fetch_atlassian,
    "intuit": fetch_intuit,
}


def fetch(slug: str, get_json=_get_json) -> list[dict]:
    """Fetch one registered board by slug. Raises KeyError for unknown slugs.

    Pass `get_json` to route the HTTP call through a caller's own client.
    """
    return BOARDS[slug](get_json)


_INDIA_CITIES = (
    "bangalore|bengaluru|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|"
    "kolkata|ahmedabad|jaipur|kochi|coimbatore|indore|chandigarh|mysuru|delhi"
)


def looks_indian(location: str) -> bool:
    """True if a location string names India or a major Indian city.

    Kept here rather than reused from job_monitor so this module can be run
    standalone; "Indiana"/"Indianapolis" are excluded explicitly because
    \bindia\b would otherwise not catch them but a substring check would.
    """
    text = (location or "").lower()
    if re.search(r"\bindiana(polis)?\b", text):
        return False
    return bool(re.search(r"\bindia\b", text)) or bool(re.search(rf"\b({_INDIA_CITIES})\b", text))


def main(argv: list[str]) -> int:
    slugs = argv[1:] or sorted(BOARDS)
    for slug in slugs:
        if slug not in BOARDS:
            print(f"{slug}: unknown board (known: {', '.join(sorted(BOARDS))})")
            continue
        try:
            jobs = fetch(slug)
        except Exception as exc:
            print(f"{slug:12} FAILED  {type(exc).__name__}: {exc}")
            continue
        indian = [job for job in jobs if looks_indian(job["location"])]
        print(f"{slug:12} {len(jobs):>4} roles, {len(indian):>3} in India")
        if len(slugs) == 1:
            for job in indian:
                print(f"    - {job['title'][:66]:68} [{job['location'][:36]}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
