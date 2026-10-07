#!/usr/bin/env python3
"""
discovery.py — ATS Discovery Tool
==================================
Given a company name, identifies which ATS they use and returns the config.json
entry to paste in.

Usage:
    python discovery.py "Stripe"
    python discovery.py "Hugging Face"
    python discovery.py "Databricks" --verbose
    python discovery.py --batch "Stripe" "Figma" "Vercel"
    python discovery.py --batch "Razorpay" "Swiggy" --no-search  (slug DB only)

How it works:
    1. GitHub Slug DB — Feashliaa/job-board-aggregator (20K+ companies; cached 24h)
       Greenhouse/Lever/Ashby/Workday: slug found → done
    2. Internet search fallback — DuckDuckGo site-specific search per ATS
       Extracts slug from matching job board URLs in search results
       Adds ~2s delay per ATS probed (4 probes max)

Supported ATS: Greenhouse · Lever · Ashby · Workday (slug DB)
               Greenhouse · Ashby · Lever (web search)
"""

import json
import re
import sys
import time
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse, parse_qs
import requests

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False

TIMEOUT = 8
DELAY   = 0.4
SEARCH_DELAY = 2.5  # between DDG queries — be polite

HEADERS = {
    "User-Agent": "JobAlertBot/2.0 (ATS discovery; personal use)",
    "Accept":     "application/json, text/html, */*",
}
SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─── GITHUB SLUG DB ───────────────────────────────────────────────────────────
# Feashliaa/job-board-aggregator — flat JSON arrays of ATS slugs (20K+ companies)
_SLUG_DB_BASE = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data"
SLUG_DB_FILES = {
    "greenhouse": f"{_SLUG_DB_BASE}/greenhouse_companies.json",
    "lever":      f"{_SLUG_DB_BASE}/lever_companies.json",
    "ashby":      f"{_SLUG_DB_BASE}/ashby_companies.json",
    "workday":    f"{_SLUG_DB_BASE}/workday_companies.json",
}
SLUG_DB_TTL   = 86400  # 24 hours
SLUG_DB_CACHE = Path(__file__).parent / ".slug_db_cache"


# ─── DATA CLASS ──────────────────────────────────────────────────────────────

@dataclass
class Discovery:
    ats:          str
    token:        str
    api_url:      str
    job_count:    int    # -1 = not live-verified
    config_line:  str    # ready to paste into config.json


# ─── SLUG GENERATOR ──────────────────────────────────────────────────────────

def slugs(company: str) -> list:
    """Generate plausible ATS slug variants from a company name."""
    name = company.strip(" .")
    variants = [
        re.sub(r"[^a-z0-9]+", "",  name.lower()),
        re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
        re.sub(r"\s+",         "",  name.lower()),
        re.sub(r"\s+",        "-",  name.lower()),
    ]
    cleaned = re.sub(
        r"\b(inc\.?|llc\.?|corp\.?|ltd\.?|co\.?|the|group|technologies?|tech)\b",
        "", name, flags=re.IGNORECASE
    ).strip(" .")
    if cleaned and cleaned.lower() != name.lower():
        variants += [
            re.sub(r"[^a-z0-9]+", "",  cleaned.lower()),
            re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-"),
        ]
    seen, result = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result


# ─── SLUG DB ─────────────────────────────────────────────────────────────────

def _get_slug_db_text(ats: str) -> Optional[str]:
    """Return cached slug DB text, refreshing from GitHub if stale."""
    SLUG_DB_CACHE.mkdir(exist_ok=True)
    cache_file = SLUG_DB_CACHE / f"{ats}.json"

    if cache_file.exists():
        if time.time() - cache_file.stat().st_mtime < SLUG_DB_TTL:
            try:
                return cache_file.read_text(encoding="utf-8")
            except Exception:
                pass

    try:
        r = requests.get(SLUG_DB_FILES[ats], headers=HEADERS, timeout=15)
        if r.status_code == 200 and r.text.strip().startswith("["):
            cache_file.write_text(r.text, encoding="utf-8")
            return r.text
    except Exception:
        pass

    # Serve stale cache on network failure
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _config_line(company, ats, url):
    return json.dumps({"name": company, "url": url, "priority": "medium"})


def slug_db_lookup(company: str, verbose: bool = False) -> Optional[Discovery]:
    """Check Feashliaa/job-board-aggregator slug DB (cached 24h)."""
    candidates = slugs(company)
    if verbose:
        print(f"  📦 Slug DB: checking variants {candidates}")

    for ats in SLUG_DB_FILES:
        raw = _get_slug_db_text(ats)
        if raw is None:
            if verbose:
                print(f"     [{ats}] unavailable (network or cache miss)")
            continue
        text = raw.lower()

        for slug in [s.lower() for s in candidates]:

            # ── Greenhouse / Lever / Ashby: plain slug exact match ────────────
            if ats != "workday":
                if f'"{slug}"' not in text:
                    continue
                if verbose:
                    print(f"     [{ats}] ✓ {slug!r}")

                if ats == "greenhouse":
                    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
                    return Discovery("Greenhouse [SlugDB]", slug, url, -1,
                                     _config_line(company, ats, url))
                if ats == "lever":
                    api  = f"https://api.lever.co/v0/postings/{slug}"
                    url  = f"lever://{slug}"
                    return Discovery("Lever [SlugDB]", slug, api, -1,
                                     _config_line(company, ats, url))
                if ats == "ashby":
                    url = f"https://jobs.ashbyhq.com/{slug}/"
                    api = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
                    return Discovery("Ashby [SlugDB]", slug, api, -1,
                                     _config_line(company, ats, url))

            # ── Workday: entries are "subdomain|wdN|board" ───────────────────
            else:
                needle = f'"{slug}|'
                if needle not in text:
                    continue
                entries, pos = [], 0
                while True:
                    idx = text.find(needle, pos)
                    if idx == -1:
                        break
                    start = idx + 1
                    end   = text.index('"', start)
                    parts = text[start:end].split("|", 2)
                    if len(parts) == 3 and not parts[0].startswith("wd"):
                        entries.append(parts)
                    pos = end + 1
                if not entries:
                    continue
                # Prefer board names that look public-facing
                chosen = entries[0]
                for kw in ("external", "career", "job"):
                    match = next((p for p in entries if kw in p[2].lower()), None)
                    if match:
                        chosen = match
                        break
                sub, wd_str, board = chosen
                wd_num = int(wd_str.replace("wd", ""))
                api_url = f"https://{sub}.{wd_str}.myworkdayjobs.com/wday/cxs/{sub}/{board}/jobs"
                url = f"workday://{sub}/{wd_num}/{board}"
                if verbose:
                    print(f"     [workday] ✓ {sub}|{wd_str}|{board}")
                return Discovery("Workday [SlugDB]", sub, api_url, -1,
                                 _config_line(company, "workday", url))

    return None


# ─── INTERNET SEARCH LAYER ───────────────────────────────────────────────────
# Searches DuckDuckGo with site-specific queries, extracts slug from result URLs.
# Covers Greenhouse, Ashby, Lever. Workday skipped — complex URL structure
# means the slug DB is more reliable; probe manually for Workday misses.

_SEARCH_SPECS = [
    {
        "label":   "Greenhouse",
        "site_q":  "site:boards-api.greenhouse.io",
        "pattern": r"boards-api\.greenhouse\.io/v1/boards/([^/?#\"\s]+)/",
        "api_url": lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
        "cfg_url": lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
    },
    {
        "label":   "Ashby",
        "site_q":  "site:jobs.ashbyhq.com",
        "pattern": r"jobs\.ashbyhq\.com/([^/?#\"\s]+)",
        "api_url": lambda s: f"https://api.ashbyhq.com/posting-api/job-board/{s}",
        "cfg_url": lambda s: f"https://jobs.ashbyhq.com/{s}/",
    },
    {
        "label":   "Lever",
        "site_q":  "site:jobs.lever.co",
        "pattern": r"jobs\.lever\.co/([^/?#\"\s]+)",
        "api_url": lambda s: f"https://api.lever.co/v0/postings/{s}",
        "cfg_url": lambda s: f"lever://{s}",
    },
]


def _ddg_search(query: str) -> list:
    """POST to DuckDuckGo HTML endpoint, return list of result URLs."""
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=SEARCH_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        urls = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                qs   = parse_qs(urlparse(href).query)
                real = qs.get("uddg", [""])[0]
                if real:
                    urls.append(unquote(real))
            elif href.startswith("http"):
                urls.append(href)
        return urls[:10]
    except Exception:
        return []


def _slug_sane(slug: str, company: str) -> bool:
    """Rough sanity check: slug should share some prefix chars with company name."""
    cn = re.sub(r"[^a-z0-9]", "", company.lower())
    sn = re.sub(r"[^a-z0-9]", "", slug.lower())
    n  = min(3, len(cn), len(sn))
    if n == 0:
        return False
    return cn[:n] in sn or sn[:n] in cn


def internet_search_lookup(company: str, verbose: bool = False) -> Optional[Discovery]:
    """Search DuckDuckGo site-by-site to find an ATS slug."""
    if not _BS4:
        if verbose:
            print("  ⚠  beautifulsoup4 not installed — internet search skipped")
        return None

    for spec in _SEARCH_SPECS:
        query = f'"{company}" careers {spec["site_q"]}'
        if verbose:
            print(f"  🌐 [{spec['label']}] DDG: {query!r}")

        urls = _ddg_search(query)
        if verbose:
            print(f"     {len(urls)} result URLs")

        for url in urls:
            m = re.search(spec["pattern"], url, re.IGNORECASE)
            if not m:
                continue
            slug = m.group(1).rstrip("/")
            if not _slug_sane(slug, company):
                if verbose:
                    print(f"     slug {slug!r} looks unrelated to {company!r} — skip")
                continue
            if verbose:
                print(f"     [{spec['label']}] ✓ {slug!r}")
            api = spec["api_url"](slug)
            cfg = spec["cfg_url"](slug)
            return Discovery(f"{spec['label']} [Web]", slug, api, -1,
                             _config_line(company, spec["label"].lower(), cfg))

        time.sleep(SEARCH_DELAY)

    return None


# ─── DISCOVER ─────────────────────────────────────────────────────────────────

# Reused across every lookup: connection pooling turns a batch of dozens of
# board calls from one TCP+TLS handshake each into a handful.
SESSION = requests.Session()


# ─── LIVE VERIFICATION ───────────────────────────────────────────────────────
# A slug being present in the slug DB is not the same as a slug that works.
# Measured on a 19-company batch, 13 of the DB's "hits" returned 404/422 — wrong
# board name, renamed tenant, or a board that was taken down. Every candidate is
# now called before it is reported, and a board with zero jobs is not a hit.

def verify(ats: str, token: str, timeout: int = 8):
    """Call the board and return its job count, or None if it does not resolve."""
    try:
        if ats == "greenhouse":
            r = SESSION.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                             headers=HEADERS, timeout=timeout)
            return len(r.json().get("jobs", [])) if r.status_code == 200 else None
        if ats == "lever":
            r = SESSION.get(f"https://api.lever.co/v0/postings/{token}?mode=json",
                             headers=HEADERS, timeout=timeout)
            return len(r.json()) if r.status_code == 200 else None
        if ats == "ashby":
            q = {"operationName": "ApiJobBoardWithTeams",
                 "variables": {"organizationHostedJobsPageName": token},
                 "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) "
                          "{ jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: "
                          "$organizationHostedJobsPageName) { jobPostings { id } } }"}
            r = SESSION.post("https://jobs.ashbyhq.com/api/non-user-graphql",
                              json=q, headers=HEADERS, timeout=timeout)
            board = (r.json().get("data") or {}).get("jobBoard")
            return len(board["jobPostings"]) if board else None
        if ats == "smartrecruiters":
            r = SESSION.get(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1",
                headers=HEADERS, timeout=timeout)
            return r.json().get("totalFound") if r.status_code == 200 else None
        if ats == "workday":
            tenant, wd_num, board = token.split("/")
            r = SESSION.post(
                f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs",
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                headers={**HEADERS, "Content-Type": "application/json"}, timeout=timeout)
            return r.json().get("total") if r.status_code == 200 else None
    except Exception:
        return None
    return None


def _monitor_url(ats: str, token: str) -> str:
    """The url form job_monitor.py's config.json expects for this ATS."""
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{token}/"
    return f"{ats}://{token}"


# ─── CAREERS-PAGE RESOLUTION ─────────────────────────────────────────────────
# Search engines rate-limit and often return nothing at all. A company's own
# careers page is a more reliable oracle: it either redirects to the ATS or
# links to it, and unlike the slug DB it carries the Workday *board name* —
# the part whose absence makes a guessed Workday url 422.

_ATS_URL_PATTERNS = [
    ("workday", re.compile(
        r"https?://([\w-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:[\w-]+/)??([\w-]+)", re.I)),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)),
]

# Workday localised path segments that are never the board name.
_WD_LOCALE = re.compile(r"^(en|en-us|en-gb|de|fr|es|ja|zh)([-_][a-z]{2})?$", re.I)


def _ats_from_text(text, final_url):
    """Yield (ats, token) pairs found in a page's final URL or its HTML."""
    for haystack in (final_url, text):
        for ats, pat in _ATS_URL_PATTERNS:
            for m in pat.finditer(haystack or ""):
                if ats == "workday":
                    tenant, wd_num, board = m.group(1), m.group(2), m.group(3)
                    if _WD_LOCALE.match(board):
                        continue
                    yield ats, f"{tenant}/{wd_num}/{board}"
                else:
                    yield ats, m.group(1)


def careers_page_lookup(company: str, verbose: bool = False):
    """Fetch the company's careers page, follow redirects, read the ATS off it.

    The four candidate hostnames are fetched concurrently rather than in turn —
    three of them are usually dead, and waiting out their timeouts one after
    another dominated the runtime.
    """
    from concurrent.futures import ThreadPoolExecutor

    stem = re.sub(r"[^a-z0-9]", "", company.lower())
    urls = [f"https://{stem}.com/careers", f"https://careers.{stem}.com",
            f"https://jobs.{stem}.com", f"https://www.{stem}.com/careers"]

    def fetch(url):
        try:
            r = SESSION.get(url, headers=SEARCH_HEADERS, timeout=6, allow_redirects=True)
            return r if r.status_code == 200 else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = [r for r in pool.map(fetch, urls) if r is not None]

    seen = set()
    candidates = []
    for r in pages:
        if verbose:
            print(f"  🏠 {r.url}")
        for ats, token in _ats_from_text(r.text, r.url):
            if (ats, token) not in seen:
                seen.add((ats, token))
                candidates.append((ats, token))

    if not candidates:
        return None

    def check(c):
        ats, token = c
        count = verify(ats, token)
        return (ats, token, count) if count else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = [h for h in pool.map(check, candidates) if h]

    if verbose:
        for ats, token, count in hits:
            print(f"     [{ats}] {token} -> {count} jobs")
    if not hits:
        return None

    ats, token, count = max(hits, key=lambda h: h[2])
    api = _monitor_url(ats, token)
    return Discovery(f"{ats.title()} [Careers]", token, api, count,
                     _config_line(company, ats, api))


# ─── PROBE ───────────────────────────────────────────────────────────────────
# Once every candidate is verified anyway, guessing becomes cheap and safe:
# try each slug variant against all five ATSes in parallel and keep whatever
# actually answers. A wrong guess costs one 404, and no unverified guess can
# reach config.json. This finds companies the slug DB has never heard of.

_PROBE_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters")


def probe_lookup(company: str, verbose: bool = False):
    """Try slug variants against every non-Workday ATS, keep verified hits."""
    from concurrent.futures import ThreadPoolExecutor

    candidates = slugs(company)
    # SmartRecruiters ids are frequently CamelCase, e.g. PaloAltoNetworks2.
    camel = re.sub(r"[^A-Za-z0-9]", "", company)
    tasks = [(ats, slug) for ats in _PROBE_ATS for slug in candidates]
    tasks += [("smartrecruiters", camel)]

    def attempt(task):
        ats, slug = task
        count = verify(ats, slug, timeout=8)
        return (ats, slug, count) if count else None

    with ThreadPoolExecutor(max_workers=12) as pool:
        hits = [h for h in pool.map(attempt, tasks) if h]

    if verbose:
        for ats, slug, count in hits:
            print(f"     [{ats}] {slug} -> {count} jobs")
    if not hits:
        return None

    # Prefer the board with the most jobs; a 1-job board is usually an old stub.
    ats, slug, count = max(hits, key=lambda h: h[2])
    api = _monitor_url(ats, slug)
    return Discovery(f"{ats.title()} [Probe]", slug, api, count,
                     _config_line(company, ats, api))


def discover(company: str, verbose: bool = False,
             no_search: bool = False, probe: bool = False) -> Optional[Discovery]:
    print(f"\n🔍 {company!r}")

    def confirmed(result, label):
        """Report a candidate only if its board actually answers with jobs."""
        if not result:
            return None
        ats = result.ats.split(" [")[0].lower()
        token = result.token
        if ats == "workday" and "/" not in token:
            # Slug DB stores the tenant without the board name; unusable as-is.
            if verbose:
                print(f"  ⚠  {label}: workday tenant {token!r} has no board name")
            return None
        count = verify(ats, token)
        if not count:
            print(f"  ⚠  {label} slug {token!r} did not resolve — discarded")
            return None
        result.job_count = count
        print(f"  {label} ({ats}) — {count} jobs live")
        return result

    # Layer 1: GitHub slug DB (fast, cached 24h) — verified before it is trusted
    r = confirmed(slug_db_lookup(company, verbose=verbose), "📦 Slug DB hit")
    if r:
        return r

    # Layer 2: probe every ATS with slug variants; only verified hits survive.
    # ~20 calls per company, so opt in with --probe when cheaper layers miss.
    if probe:
        r = probe_lookup(company, verbose=verbose)
        if r:
            print(f"  🎯 Probe hit ({r.ats.split(' [')[0]}) — {r.job_count} jobs live")
            return r

    # Layer 3: the company's own careers page, followed to wherever it lands
    r = careers_page_lookup(company, verbose=verbose)
    if r:
        print(f"  🏠 Careers-page hit ({r.ats.split(' [')[0]}) — {r.job_count} jobs live")
        return r

    # Layer 4: Internet search (DuckDuckGo, site-specific)
    if no_search:
        print(f"  ⏭  No verified board (--no-search, skipping web search)")
        return None

    print(f"  🌐 Searching web...")
    r = confirmed(internet_search_lookup(company, verbose=verbose), "🌐 Web hit")
    if r:
        return r

    return None


# ─── OUTPUT ──────────────────────────────────────────────────────────────────

def print_result(company, result):
    sep = "─" * 60
    if result:
        label  = result.ats.replace(" [SlugDB]", "").replace(" [Web]", "")
        source = "GitHub slug DB" if "[SlugDB]" in result.ats else "web search"
        cnt    = str(result.job_count) if result.job_count >= 0 else "not verified"
        print(f"\n{sep}")
        print(f"✅  ATS      : {label}  ({source})")
        print(f"    Slug    : {result.token}")
        print(f"    Endpoint: {result.api_url}")
        print(f"    Jobs    : {cnt}")
        print(f"\n    📋  Paste into config.json companies array:\n")
        print(f"       {result.config_line}")
        print(f"{sep}\n")
    else:
        print(f"\n{sep}")
        print(f"❌  ATS not found for {company!r}")
        print()
        print("   Possible reasons:")
        print("   • Custom career site (no public API)")
        print("   • ATS not supported: iCIMS, Taleo, SAP SuccessFactors, SmartRecruiters")
        print("   • Workday company not in slug DB (try looking up their career URL manually)")
        print()
        print("   Try: --verbose  to see each step")
        print("   Manual: Open careers page → DevTools → Network → XHR → find ATS domain")
        print(f"{sep}\n")


def batch_discover(companies, verbose=False, no_search=False, probe=False):
    """Look up every company concurrently and print one summary table.

    Per-company progress lines are suppressed: with workers running in parallel
    they interleave into noise, and redirecting stdout per thread is unsafe
    because stdout is process-global.
    """
    import io
    from contextlib import redirect_stdout, nullcontext
    from concurrent.futures import ThreadPoolExecutor

    def one(company):
        return company, discover(company, verbose=verbose,
                                 no_search=no_search, probe=probe)

    # Redirect once, in this thread, around the whole pool. Swapping stdout
    # inside each worker would race: sys.stdout is process-global, not per-thread.
    sink = nullcontext() if verbose else redirect_stdout(io.StringIO())
    with sink:
        with ThreadPoolExecutor(max_workers=10) as pool:
            rows = list(pool.map(one, companies))

    bar = "=" * 78
    print("\n" + bar + "\nSUMMARY\n" + bar)
    print(f"{'Company':<24} {'ATS':<16} {'Slug':<26} {'Source'}")
    print(f"{'-'*24} {'-'*16} {'-'*26} {'-'*16}")
    for c, r in rows:
        if r:
            label  = r.ats.split(" [")[0]
            source = ("SlugDB" if "[SlugDB]" in r.ats
                      else "Careers" if "[Careers]" in r.ats
                      else "Probe" if "[Probe]" in r.ats else "Web")
            print(f"{c:<24} {label:<16} {r.token:<26} {source} ({r.job_count} jobs)")
        else:
            print(f"{c:<24} {'NOT FOUND':<16} {'-':<26}")
    print()
    for c, r in rows:
        if r:
            print(f"  {r.config_line}")
    print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Discover ATS for any company (slug DB then web search)."
    )
    p.add_argument("company", nargs="?", help="Company name to look up")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Show each lookup step")
    p.add_argument("--batch", nargs="+", metavar="COMPANY",
                   help="Look up multiple companies")
    p.add_argument("--no-search", action="store_true",
                   help="Only check slug DB, skip internet search (fast, no DDG)")
    p.add_argument("--probe", action="store_true",
                   help="Also brute-force slug variants against every ATS "
                        "(slow: ~20 calls per company)")
    args = p.parse_args()

    if args.batch:
        batch_discover(args.batch, verbose=args.verbose,
                       no_search=args.no_search, probe=args.probe)
        return 0
    if not args.company:
        p.print_help()
        return 1

    r = discover(args.company, verbose=args.verbose,
                 no_search=args.no_search, probe=args.probe)
    print_result(args.company, r)
    return 0 if r else 1


if __name__ == "__main__":
    sys.exit(main())
