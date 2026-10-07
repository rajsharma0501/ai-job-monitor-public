#!/usr/bin/env python3
"""
AI Job Monitor v2.0 - Intelligent role-based job tracking with freshness detection
Features: Rule-based scoring, state expiration, deduplication, local dashboards
License: MIT
"""

# Force UTF-8 output on Windows (cp1252 terminal crashes on emoji prints)
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import requests
from bs4 import BeautifulSoup
import json
import hashlib
import re
import random
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta
import time
import os
from pathlib import Path
from collections import defaultdict
import sys

# ── Cross-cutting instrumentation ────────────────────────────────
# Logging, rate limiting, circuit breaking and metrics live in observability.py.
# Imported by name for readability; the shared collections there are cleared in
# place rather than rebound, so these bindings stay valid for the process life.
import custom_boards
from report_safety import safe_report_job
from observability import (
    log,
    _polite_sleep, _last_request, _rate_lock,
    _CB_THRESHOLD, _CB_COOLDOWN, _circuit,
    _cb_is_open, _cb_record_failure, _cb_record_success, reset_circuit,
    _fetch_errors, _fetch_errors_lock, _record_fetch_error, reset_fetch_errors,
    _timed, _append_metrics, _METRICS_FILE, _SLOW_COMPANY_COUNT,
)

# ── India location sets (used by SmartRecruiters structured location check) ──
_INDIA_COUNTRY_CODES = {'in', 'ind'}
_INDIA_CITIES = {
    'hyderabad', 'bangalore', 'bengaluru', 'delhi', 'new delhi',
    'gurgaon', 'gurugram', 'noida', 'mumbai', 'pune', 'chennai',
    'kolkata', 'vijayawada', 'ahmedabad', 'secunderabad',
}
_INDIA_REGIONS = {
    'ka', 'mh', 'up', 'ts', 'ap', 'tn', 'dl', 'hr', 'wb',
    'karnataka', 'maharashtra', 'uttar pradesh', 'telangana',
    'andhra pradesh', 'tamil nadu', 'haryana', 'ncr', 'nct',
}

class JobMonitor:
    _config_logged = False

    def __init__(self, config_file='config.json', env_file=None, dry_run=False):
        """Initialize monitor with config from file + environment variables"""
        self.load_env_file(env_file)
        self.dry_run = dry_run
        # Load base config
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        
        self.state_file = Path('job_state.json')
        self.request_delay = float(self.config.get('request_delay', 1.5))
        self.max_retries = int(self.config.get('max_retries', 3))
        self.bot_identity = self.config.get('bot_identity', False)
        
        # Configuration
        self.state_expiry_days = self.config.get('state_expiry_days', 90)
        self.max_job_age_days = self.config.get('max_job_age_days', 2)
        self.location_filter = [kw.lower() for kw in self.config.get('location_filter', [])]
        kw_cfg = self.config.get('keywords', {})
        self.kw_seniority = [k.lower() for k in kw_cfg.get('seniority',
            ['principal', 'staff', 'senior staff', 'lead', 'senior', 'director', 'head of'])]
        self.kw_domain = [k.lower() for k in kw_cfg.get('domain',
            ['machine learning', 'ml', 'mlops', 'llm', 'agent', 'deep learning',
             'reinforcement learning', 'generative ai', 'foundation model', 'ai/ml', 'ai'])]
        self.kw_role = [k.lower() for k in kw_cfg.get('role',
            ['engineer', 'scientist', 'architect', 'researcher'])]
        # Exclusion keywords: any match drops the job before scoring (Tier 1)
        self.kw_exclude = [k.lower() for k in kw_cfg.get('exclude', [
            'threat researcher', 'threat intelligence', 'threat hunting',
            'security researcher', 'penetration tester', 'penetration testing',
            'malware analyst', 'malware researcher', 'forensic',
            'soc analyst', 'incident response', 'red team', 'vulnerability researcher',
            'sales engineer', 'sales manager', 'talent acquisition',
        ])]

        
        JobMonitor._config_logged = True
        
        self._state_lock = threading.Lock()
        self._raw_counts_lock = threading.Lock()
        self._raw_counts = {}          # company -> postings returned by its board this run
        self.state = self.load_state()
        self.last_run_at = None
        meta = self.state.get('__meta__', {}) if isinstance(self.state, dict) else {}
        if isinstance(meta, dict):
            last_run = meta.get('last_run_at')
            if last_run:
                try:
                    self.last_run_at = datetime.fromisoformat(last_run)
                except ValueError:
                    self.last_run_at = None

    def load_env_file(self, path=None):
        """Load local env vars from .env without overriding existing env"""
        if path is None:
            path = Path.home() / '.env.ai-job-alert'
        env_path = Path(path)
        if not env_path.exists():
            return

        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        except Exception as e:
            print(f"⚠ Warning: Could not load .env: {e}")
        
    def load_state(self):
        """Load previous job IDs with auto-expiration of old entries"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)

                cleaned_state = self.clean_old_state(state)
                old_count = sum(len(v) if isinstance(v, (list, dict)) else 0 for v in state.values())
                new_count = sum(len(v) if isinstance(v, (list, dict)) else 0 for v in cleaned_state.values())
                if old_count > new_count:
                    print(f"ℹ Cleaned {old_count - new_count} expired state entries (>{self.state_expiry_days} days old)")

                return cleaned_state
            except Exception as e:
                # A corrupt state file would otherwise reset tracking to empty and make
                # every job at every company look new on the next run.
                print(f"⚠ Warning: Could not load state: {e}")
                log.error('State load failed (%s) — trying backup', e)
                backup = self.state_file.with_suffix('.json.bak')
                if backup.exists():
                    try:
                        with open(backup, 'r', encoding='utf-8') as f:
                            recovered = self.clean_old_state(json.load(f))
                        print(f"✓ Recovered state from {backup.name}")
                        log.warning('Recovered state from backup %s', backup.name)
                        return recovered
                    except Exception as e2:
                        log.error('Backup state also unreadable: %s', e2)
                return {}
        return {}

    def clean_old_state(self, state):
        """Remove entries older than state_expiry_days"""
        cutoff = datetime.now() - timedelta(days=self.state_expiry_days)
        cleaned = {}
        
        for company, entries in state.items():
            if company == '__meta__':
                cleaned[company] = entries
                continue
            if isinstance(entries, list):
                cleaned[company] = entries
            elif isinstance(entries, dict):
                valid_entries = {}
                for job_id, metadata in entries.items():
                    try:
                        first_seen_str = metadata.get('first_seen', '')
                        if first_seen_str:
                            first_seen = datetime.fromisoformat(first_seen_str)
                            if first_seen > cutoff:
                                valid_entries[job_id] = metadata
                        else:
                            valid_entries[job_id] = metadata
                    except Exception:
                        valid_entries[job_id] = metadata

                if valid_entries:
                    cleaned[company] = valid_entries
        
        return cleaned
    
    def save_state(self):
        """Persist job IDs and metadata atomically (thread-safe).

        Writes to a temp file and swaps it in with os.replace, which is atomic on
        both Windows and POSIX. A crash mid-write therefore leaves the previous
        state intact instead of a truncated file, and the prior version is kept as
        a .bak for load_state() to fall back on.
        """
        with self._state_lock:
            try:
                if isinstance(self.state, dict):
                    self.state.setdefault('__meta__', {})
                    if self.last_run_at:
                        self.state['__meta__']['last_run_at'] = self.last_run_at.isoformat()

                tmp_file = self.state_file.with_suffix('.json.tmp')
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(self.state, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                if self.state_file.exists():
                    try:
                        os.replace(self.state_file, self.state_file.with_suffix('.json.bak'))
                    except OSError as e:
                        log.warning('Could not roll state backup: %s', e)

                os.replace(tmp_file, self.state_file)
            except Exception as e:
                print(f"⚠ Warning: Could not save state: {e}")
                log.error('State save failed: %s', e)
    
    def calculate_job_score(self, job_title, company_priority='medium', location=''):
        """
        Score job relevance from 0-100
        80+ = URGENT
        60-79 = HIGH
        40-59 = MEDIUM
        <40 = LOW
        """
        score = 0
        title_lower = job_title.lower()
        
        # SENIORITY (30 points max) — bonus points for specific levels
        _seniority_points = {'principal': 30, 'senior staff': 28, 'staff': 28,
                             'lead': 20, 'senior': 15, 'director': 25, 'head of': 25}
        for kw in self.kw_seniority:
            if kw in title_lower:
                score += _seniority_points.get(kw, 15)
                break
        
        # DOMAIN FIT (40 points max) — bonus points for specific domains
        _domain_points = {
            'agent': 25, 'llm': 20, 'reinforcement learning': 20, 'rl engineer': 20,
            'mlops': 15, 'ml platform': 18, 'ai platform': 18, 'data platform': 15,
            'machine learning infrastructure': 18, 'ml infrastructure': 18,
            'ai infrastructure': 18, 'copilot': 20, 'generative ai': 15,
            'foundation model': 18, 'model training': 12, 'distributed systems': 10,
            'compiler': 15,
        }
        domain_score = 0
        for kw in self.kw_domain:
            if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
                domain_score = max(domain_score, _domain_points.get(kw, 15))
        score += domain_score
        
        # ROLE TYPE (20 points max)
        _role_points = {'engineer': 20, 'scientist': 18, 'architect': 15, 'researcher': 12}
        for kw in self.kw_role:
            if kw in title_lower:
                score += _role_points.get(kw, 15)
                break
        
        # LOCATION BONUS (10 points max) — check both title and location field
        loc_text = f"{title_lower} {location.lower()}"
        if 'hyderabad' in loc_text or 'chennai' in loc_text:
            score += 10
        elif 'remote' in loc_text or 'india' in loc_text:
            score += 5
        elif 'bengaluru' in loc_text or 'bangalore' in loc_text:
            score += 7
        
        # Company brand (minor adjustment, max ±5)
        if company_priority == 'high':
            score += 5
        
        return min(score, 100)
    
    def get_priority_level(self, score):
        """Convert score to priority tier"""
        if score >= 80:
            return 'URGENT'
        elif score >= 60:
            return 'HIGH'
        elif score >= 40:
            return 'MEDIUM'
        else:
            return 'LOW'
    
    def _user_agent(self):
        if self.bot_identity:
            return 'JobAlertBot/2.0 (personal use; https://github.com/rajsharma0501/ai-job-monitor-public)'
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

    def _http_request(self, method, url, **kwargs):
        """HTTP request with circuit breaker, polite delay, retry and exponential backoff"""
        domain = urlparse(url).netloc
        if _cb_is_open(domain):
            return None  # silent skip while circuit is open

        _polite_sleep(url, self.request_delay)
        headers = {'User-Agent': self._user_agent()}
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
        else:
            headers['Accept'] = 'application/json, text/html, */*'

        do_request = requests.post if method == 'POST' else requests.get
        for attempt in range(self.max_retries):
            try:
                r = do_request(url, headers=headers, timeout=15, **kwargs)
                if r.status_code == 429:
                    wait = int(r.headers.get('Retry-After', 2 ** attempt * 5))
                    log.warning('Rate-limited on %s — waiting %ds', url, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                _cb_record_success(domain)
                return r
            except requests.RequestException as e:
                is_4xx = (
                    isinstance(e, requests.HTTPError) and
                    e.response is not None and
                    400 <= e.response.status_code < 500
                )
                if is_4xx:
                    # Client error (wrong slug/URL) — not a server failure; don't retry or trip circuit
                    _record_fetch_error(url, str(e), '4xx')
                    log.warning('  ⚠ Error %s %s: %s', method, url, e)
                    return None
                if attempt == self.max_retries - 1:
                    _cb_record_failure(domain)
                    _record_fetch_error(url, str(e), 'network')
                    log.warning('  ⚠ Error %s %s: %s', method, url, e)
                    return None
                backoff = (2 ** attempt) + random.uniform(0, 1)
                log.warning('Attempt %d failed for %s (%s) — retry in %.1fs', attempt + 1, url, e, backoff)
                time.sleep(backoff)
        return None

    def _http_get(self, url, **kwargs):
        return self._http_request('GET', url, **kwargs)

    def _http_post(self, url, **kwargs):
        return self._http_request('POST', url, **kwargs)

    def fetch_page(self, url):
        """Fetch page HTML with retry+backoff"""
        r = self._http_get(url)
        return r.text if r else None
    
    def _process_postings(self, company, postings, title_key='title', link_fn=None, location_fn=None):
        """Shared loop: filter by criteria+location, deduplicate, and build job dicts."""
        # Raw board size before any filtering. A board that fetched fine but returned
        # nothing looks identical to a quiet board unless we track this across runs.
        with self._raw_counts_lock:
            self._raw_counts[company] = len(postings)
        jobs, seen = [], set()
        for posting in postings:
            title = posting.get(title_key, '').strip()
            link = link_fn(posting) if link_fn else ''
            location = location_fn(posting) if location_fn else ''
            if not title or not self.matches_criteria(title):
                continue
            if not self.matches_location(location):
                continue
            dedup_key = (title.lower(), link)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            job_id = hashlib.md5(f"{company}:{title}:{link}".encode()).hexdigest()
            jobs.append({'id': job_id, 'company': company, 'title': title,
                         'url': link, 'location': location or 'Unknown',
                         'found_at': datetime.now().isoformat()})
        return jobs

    def extract_jobs_ashby(self, company, url):
        """Extract jobs from Ashby GraphQL API"""
        slug = url.rstrip('/').split('/')[-1]
        query = {
            'operationName': 'ApiJobBoardWithTeams',
            'variables': {'organizationHostedJobsPageName': slug},
            'query': 'query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { jobPostings { id title locationName } } }'
        }
        resp = self._http_post('https://jobs.ashbyhq.com/api/non-user-graphql', json=query)
        if not resp:
            return []
        postings = resp.json().get('data', {}).get('jobBoard', {}).get('jobPostings', [])

        return self._process_postings(company, postings,
            link_fn=lambda p: f"https://jobs.ashbyhq.com/{slug}/{p.get('id', '')}",
            location_fn=lambda p: p.get('locationName', ''))

    def extract_jobs_greenhouse(self, company, url):
        """Extract jobs from Greenhouse public API.

        Accepts either the full board URL
        (https://boards-api.greenhouse.io/v1/boards/<slug>/jobs) or the
        greenhouse://<slug> shorthand used by the other ATS entries in config.
        """
        if url.startswith('greenhouse://'):
            slug = url[len('greenhouse://'):].strip('/').split('/')[0]
        else:
            parts = url.rstrip('/').split('/boards/')
            slug = parts[-1].split('/')[0] if len(parts) > 1 else url.rstrip('/').split('/')[-1]
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        resp = self._http_get(api_url)
        if not resp:
            return []
        postings = resp.json().get('jobs', [])

        return self._process_postings(company, postings,
            link_fn=lambda p: p.get('absolute_url', ''),
            location_fn=lambda p: (p['location']['name'] if isinstance(p.get('location'), dict) else str(p.get('location', ''))))

    def extract_jobs_custom(self, company, url):
        """Extract jobs from a company that runs its own careers site.

        custom://<slug> dispatches to custom_boards, which holds one small
        fetcher per site that calls the JSON endpoint the site's own JavaScript
        calls. See custom_boards.py for how to add a new one.
        """
        slug = url[len('custom://'):].strip('/').split('/')[0]

        def get_json(api_url):
            # Route the fetcher's HTTP through _http_request so these boards get
            # the same rate limiting, retries, circuit breaking and error
            # recording as every other ATS. A None response means the request
            # failed or the circuit is open; raising here unwinds the fetcher,
            # which may be midway through paginating.
            resp = self._http_get(api_url)
            if resp is None:
                raise RuntimeError(f'fetch failed: {api_url}')
            return resp.json()

        try:
            postings = custom_boards.fetch(slug, get_json=get_json)
        except KeyError:
            log.warning('  ⚠ Unknown custom board %r for %s — check custom_boards.BOARDS',
                        slug, company)
            return []
        except Exception as e:
            # A custom site changing its API shape must not take down the run.
            log.warning('  ⚠ Error fetching custom board %s for %s: %s', slug, company, e)
            return []

        return self._process_postings(company, postings,
            link_fn=lambda p: p.get('url', ''),
            location_fn=lambda p: p.get('location', ''))

    def extract_jobs_amazon(self, company, url):
        """Extract jobs from Amazon jobs JSON API"""
        api_url = 'https://www.amazon.jobs/en/search.json?base_query=machine+learning+engineer&loc_query=India&job_count=20&result_limit=20'
        resp = self._http_get(api_url)
        if not resp:
            return []
        postings = resp.json().get('jobs', [])

        return self._process_postings(company, postings,
            link_fn=lambda p: 'https://www.amazon.jobs' + p.get('job_path', ''),
            location_fn=lambda p: p.get('location', ''))

    # Workday boards run to thousands of postings and page 20 at a time, so walking one
    # end to end costs minutes. This caps a pathological board that never goes stale.
    _WORKDAY_MAX_PAGES = 40

    def _workday_page_is_stale(self, postings):
        """True when every posting on this page is older than max_job_age_days.

        Workday's postedOn reads like 'Posted Today' / 'Posted 4 Days Ago'. The
        dateless forms ('Today', 'Yesterday') parse to None and count as fresh.
        """
        if not self.max_job_age_days:
            return False
        window = timedelta(days=self.max_job_age_days)
        for posting in postings:
            age = self.get_relative_age(posting.get('postedOn', ''))
            if age is None or age <= window:
                return False
        return True

    def extract_jobs_workday(self, company, url):
        """Extract jobs from Workday CXS POST API with pagination"""
        _, rest = url.split('workday://', 1)
        parts = rest.split('/')
        subdomain, wd_num, board = parts[0], parts[1], parts[2]
        api_url = f"https://{subdomain}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{subdomain}/{board}/jobs"
        base_url = f"https://{subdomain}.wd{wd_num}.myworkdayjobs.com/en-US/{board}"

        all_postings = []
        limit, offset, pages = 20, 0, 0
        while pages < self._WORKDAY_MAX_PAGES:
            resp = self._http_post(api_url, json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""})
            if not resp:
                break
            data = resp.json()
            batch = data.get('jobPostings', [])
            if not batch:
                break
            all_postings.extend(batch)
            pages += 1
            offset += limit
            # Results are newest-first, so once a whole page predates the freshness
            # window everything after it is older still.
            if self._workday_page_is_stale(batch):
                break
            # Workday reports the real total only on the first page and 0 thereafter,
            # so only trust it when non-zero — otherwise this exits after one page.
            total = data.get('total', 0)
            if total and offset >= total:
                break

        return self._process_postings(company, all_postings,
            link_fn=lambda p: base_url + p.get('externalPath', ''),
            location_fn=lambda p: p.get('locationsText', ''))

    def extract_jobs_netflix(self, company, url):
        """Extract jobs from Netflix Eightfold AI API with pagination"""
        base = 'https://explore.jobs.netflix.net/api/apply/v2/jobs'
        num, start = 20, 0
        all_postings = []
        while True:
            resp = self._http_get(base, params={'domain': 'netflix.com', 'start': start, 'num': num,
                                                'query': 'machine learning engineer data scientist'})
            if not resp:
                break
            batch = resp.json().get('positions', [])
            all_postings.extend(batch)
            start += num
            if len(batch) < num:
                break

        return self._process_postings(company, all_postings, title_key='name',
            link_fn=lambda p: p.get('canonicalPositionUrl', f"https://jobs.netflix.com/jobs/{p.get('id', '')}"),
            location_fn=lambda p: p.get('normalizedLocation', '') or ', '.join(p.get('locations', [])))

    def extract_jobs_lever(self, company, url):
        """Extract jobs from Lever public API"""
        slug = url.split('lever://', 1)[-1].strip('/')
        api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        resp = self._http_get(api_url)
        if not resp:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return self._process_postings(company, data, title_key='text',
            link_fn=lambda p: p.get('hostedUrl', ''),
            location_fn=lambda p: p.get('categories', {}).get('location', ''))

    def extract_jobs_workable(self, company, url):
        """Extract jobs from Workable v3 POST API"""
        slug = url.split('workable://', 1)[-1].strip('/')
        api_url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
        payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        resp = self._http_post(api_url, json=payload)
        if not resp:
            return []
        postings = resp.json().get('results', [])
        return self._process_postings(company, postings,
            link_fn=lambda p: f"https://apply.workable.com/{slug}/j/{p.get('shortcode', p.get('id', ''))}/",
            location_fn=lambda p: p.get('location', {}).get('city', '') if isinstance(p.get('location'), dict) else str(p.get('location', '')))

    def extract_jobs_smartrecruiters(self, company, url):
        """Extract jobs from SmartRecruiters public API with pagination"""
        slug = url.split('smartrecruiters://', 1)[-1].strip('/')
        base_api = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        all_postings = []
        limit, offset = 100, 0
        while True:
            resp = self._http_get(base_api, params={'limit': limit, 'offset': offset})
            if not resp:
                break
            data = resp.json()
            batch = data.get('content', [])
            all_postings.extend(batch)
            offset += limit
            if offset >= data.get('totalFound', 0) or not batch:
                break

        def _sr_location(p):
            loc = p.get('location')
            if not isinstance(loc, dict):
                return ''
            city = loc.get('city', '').lower()
            region = loc.get('region', '').lower()
            country = loc.get('country', '').lower()
            full = loc.get('fullLocation', loc.get('city', ''))
            is_india = (country in _INDIA_COUNTRY_CODES or
                        city in _INDIA_CITIES or
                        region in _INDIA_REGIONS)
            parts = [full]
            if is_india and 'india' not in full.lower():
                parts.append('india')
            if loc.get('remote'):
                parts.append('remote')
            return ' '.join(parts)

        return self._process_postings(company, all_postings, title_key='name',
            link_fn=lambda p: f"https://careers.smartrecruiters.com/{slug}/{p.get('id', '')}",
            location_fn=_sr_location)

    def extract_jobs_microsoft(self, company, url):
        """Extract jobs from Microsoft apply.careers API with pagination"""
        base = 'https://apply.careers.microsoft.com/api/pcsx/search'
        start, page_size = 0, 20
        all_postings = []
        while True:
            resp = self._http_get(base, params={
                'domain': 'microsoft.com', 'query': 'machine learning',
                'start': start,
            })
            if not resp:
                break
            payload = resp.json().get('data', {})
            batch = payload.get('positions', [])
            total = payload.get('count', 0)
            all_postings.extend(batch)
            start += len(batch)
            if start >= total or not batch:
                break

        return self._process_postings(company, all_postings,
            title_key='name',
            link_fn=lambda p: f"https://apply.careers.microsoft.com{p.get('positionUrl', '/careers/job/' + str(p.get('id', '')))}",
            location_fn=lambda p: ', '.join(p.get('locations', [])))

    # URL pattern → extractor method name
    _EXTRACTORS = {
        'jobs.ashbyhq.com':       'extract_jobs_ashby',
        'boards-api.greenhouse.io': 'extract_jobs_greenhouse',
        'greenhouse://':          'extract_jobs_greenhouse',
        'amazon.jobs':            'extract_jobs_amazon',
        'workday://':             'extract_jobs_workday',
        'netflix://':             'extract_jobs_netflix',
        'microsoft://':           'extract_jobs_microsoft',
        'lever://':               'extract_jobs_lever',
        'workable://':            'extract_jobs_workable',
        'smartrecruiters://':     'extract_jobs_smartrecruiters',
        'custom://':              'extract_jobs_custom',
    }

    def extract_jobs(self, company, url, html):
        """Route to the right extractor, or fall back to generic HTML scraping"""
        for pattern, method_name in self._EXTRACTORS.items():
            if pattern in url:
                return getattr(self, method_name)(company, url)

        if not html:
            return []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            jobs = []
            seen = set()
            
            job_elements = soup.find_all(['a', 'div', 'li'], 
                                         class_=lambda x: x and any(kw in str(x).lower() 
                                         for kw in ['job', 'position', 'role', 'career', 'opening']))
            
            for elem in job_elements[:50]:
                title_elem = elem.find(['h2', 'h3', 'h4', 'span', 'a'])
                if not title_elem:
                    title_elem = elem
                
                title = title_elem.get_text(strip=True)
                
                if len(title) < 10 or len(title) > 200:
                    continue
                
                link = elem.get('href') if elem.name == 'a' else None
                if not link:
                    link_elem = elem.find('a')
                    if link_elem:
                        link = link_elem.get('href')
                
                if self.matches_criteria(title):
                    if self.is_job_too_old(elem.get_text(" ", strip=True)):
                        continue
                    if link and not link.startswith('http'):
                        link = urljoin(url, link)
                    
                    job_id = hashlib.md5(f"{company}:{title}:{link}".encode()).hexdigest()

                    dedup_key = (title.lower(), (link or url))
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    
                    jobs.append({
                        'id': job_id,
                        'company': company,
                        'title': title,
                        'url': link or url,
                        'location': 'Unknown',
                        'found_at': datetime.now().isoformat()
                    })
            
            return jobs
        except Exception as e:
            print(f"  ⚠ Error extracting jobs: {e}")
            return []

    def get_relative_age(self, text):
        """Return timedelta for text like '3 days ago' or '5 hours ago'"""
        # The optional '+' catches Workday's "Posted 30+ Days Ago"; without it that
        # string parses as unknown age and the posting counts as fresh.
        match = re.search(r"(\d+)\+?\s+(day|hour|minute)", text.lower())
        if not match:
            return None

        try:
            value = int(match.group(1))
        except ValueError:
            return None

        unit = match.group(2)
        if unit.startswith('day'):
            return timedelta(days=value)
        if unit.startswith('hour'):
            return timedelta(hours=value)
        return timedelta(minutes=value)

    def is_job_too_old(self, text):
        """Return True if posting looks older than max_job_age_days or before last run"""
        age = self.get_relative_age(text)
        if not age:
            return False

        if self.max_job_age_days and age > timedelta(days=self.max_job_age_days):
            return True

        if self.last_run_at:
            posted_at = datetime.now() - age
            if posted_at <= self.last_run_at:
                return True

        return False
    
    def matches_criteria(self, title):
        """Filter using configured keywords.

        A title is kept if it names a domain we care about, OR if it is a
        senior engineering title of any flavour ("Staff Engineer", "Member of
        Technical Staff"). Indian offices at infrastructure companies routinely
        post the second kind with no domain word in the title, so requiring a
        domain match hid real Staff/Principal roles.

        The second branch is broad by design, so keywords.exclude carries the
        weight of keeping hardware, silicon and compliance roles out.
        """
        title_lower = title.lower()

        # Tier 1: drop jobs containing any exclusion keyword (whole-word match)
        if any(re.search(r'\b' + re.escape(kw) + r'\b', title_lower) for kw in self.kw_exclude):
            return False

        # Tier 2: whole-word matching to prevent substring false positives (e.g. agent vs agentic)
        has_seniority = any(re.search(r'\b' + re.escape(kw) + r'\b', title_lower) for kw in self.kw_seniority)
        has_domain = any(re.search(r'\b' + re.escape(kw) + r'\b', title_lower) for kw in self.kw_domain)
        has_role = any(re.search(r'\b' + re.escape(kw) + r'\b', title_lower) for kw in self.kw_role)

        # A domain keyword is required. Dropping it (domain OR seniority+role)
        # was tried on 2026-08-30 and admitted 1297 of 1613 jobs in one run —
        # backend, frontend, mobile, security, support — to rescue a handful of
        # bare "Staff Engineer" posts. The domain list carries the recall
        # instead: add terms there when a real role is being missed.
        return has_domain and (has_seniority or has_role)

    # Word-boundary India detector — checked FIRST; always overrides USA check.
    # Uses \b so "Indianapolis" does NOT match \bindia\b.
    _INDIA_RE = re.compile(
        r'\bindia\b'
        r'|\bbangalore\b|\bbengaluru\b|\bhyderabad\b|\bchennai\b'
        r'|\bpune\b|\bmumbai\b|\bdelhi\b|\bgurugram\b|\bgurgaon\b|\bnoida\b'
        r'|\bkolkata\b|\bahmedabad\b|\bmohali\b|\bsecunderabad\b'
        r'|\btelangana\b|\bkarnataka\b|\bmaharashtra\b',
        re.IGNORECASE,
    )

    # Regex that identifies US-only locations. Only reached after India check passes.
    _USA_RE = re.compile(
        r'\busa\b'
        r'|\bunited\s+states\b'
        r'|\bus\b'          # "Remote, US" / "CA, US" / "US Remote"
        r'|^us[-/]'         # Workday prefix: US-CA-Remote, US-NY-City-Remote
        # US full state names
        r'|\b(california|texas|massachusetts|new\s+jersey|virginia|'
        r'north\s+carolina|south\s+carolina|illinois|florida|pennsylvania|ohio|michigan|'
        r'minnesota|arizona|tennessee|colorado|oregon|nevada|georgia|'
        r'connecticut|maryland|missouri|wisconsin|washington\s+d\.?c\.?)\b'
        # Common unambiguous US tech-hub city names
        r'|\b(san\s+francisco|los\s+angeles|new\s+york\s+city|'
        r'mountain\s+view|santa\s+clara|palo\s+alto|menlo\s+park|cupertino|'
        r'redwood\s+city|sunnyvale|san\s+jose|san\s+diego|bellevue|'
        r'austin,\s*tx|boston,\s*ma|chicago,\s*il|seattle,\s*wa|'
        r'new\s+york,\s*ny|denver,\s*co|portland,\s*or|phoenix,\s*az|'
        r'miami,\s*fl|atlanta,\s*ga|dallas,\s*tx|houston,\s*tx|'
        r'washington,\s*dc|mclean,\s*va)\b',
        re.IGNORECASE | re.MULTILINE,
    )

    def matches_location(self, location):
        """Match configured locations; keep unknown locations for manual review."""
        if not self.location_filter or not location or location.lower() == 'unknown':
            return True
        return any(re.search(r'\b' + re.escape(keyword) + r'\b', location, re.I)
                   for keyword in self.location_filter)

    def check_company(self, company_data):
        """Check one company with intelligent scoring and freshness tracking"""
        company = company_data['name']
        url = company_data['url']
        company_priority = company_data.get('priority', 'medium')
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking {company}...")
        
        # Dedicated ATS extractors call their own API and ignore `html`; only the generic
        # BeautifulSoup fallback in extract_jobs() needs the page. Fetching it for a URL that
        # a dedicated extractor handles doubles the request count for no benefit.
        needs_html = not any(pattern in url for pattern in self._EXTRACTORS)
        html = self.fetch_page(url) if needs_html else None
        jobs = self.extract_jobs(company, url, html)
        
        # Get current state (migrate old format to new if needed) — lock for thread safety
        with self._state_lock:
            company_state = dict(self.state.get(company, {}))
            if isinstance(company_state, list):
                print(f"  ℹ Migrating {company} state to new format")
                company_state = {
                    job_id: {
                        'first_seen': datetime.now().isoformat(),
                        'last_seen': datetime.now().isoformat(),
                        'title': 'Unknown'
                    }
                    for job_id in company_state
                }

        # Find NEW jobs (compare against snapshot — no lock needed here)
        new_jobs = []
        now_iso = datetime.now().isoformat()
        for job in jobs:
            job_id = job['id']
            if job_id not in company_state:
                new_jobs.append(job)
                score = self.calculate_job_score(job['title'], company_priority, job.get('location', ''))
                company_state[job_id] = {
                    'first_seen': job['found_at'],
                    'last_seen': job['found_at'],
                    'title': job['title'],
                    'url': job.get('url', ''),
                    'location': job.get('location', ''),
                    'score': score,
                    'priority': self.get_priority_level(score),
                }
            else:
                entry = company_state[job_id]
                entry['last_seen'] = now_iso
                # Backfill missing fields for pre-fix state entries
                if not entry.get('url'):
                    entry['url'] = job.get('url', '')
                if not entry.get('location'):
                    entry['location'] = job.get('location', '')
                if not entry.get('score'):
                    sc = self.calculate_job_score(job['title'], company_priority, job.get('location', ''))
                    entry['score'] = sc
                    entry['priority'] = self.get_priority_level(sc)

        # Write updated state back under lock
        with self._state_lock:
            self.state[company] = company_state

        if new_jobs:
            print(f"  ✨ Found {len(new_jobs)} new job(s)")

            for job in new_jobs:
                score = company_state[job['id']]['score']
                priority = company_state[job['id']]['priority']

                job['score'] = score
                job['priority'] = priority

                print(f"     - [{score}/100 {priority}] {job['title']}")
                print(f"       {job['url']}")

        else:
            print(f"  ✓ No new jobs")

        return new_jobs
    
    def _append_history(self, jobs):
        """Append new jobs to history.json and jobs.db (SQLite)."""
        import sqlite3
        now_iso = datetime.now().isoformat()

        # --- JSON log ---
        history_file = Path('history.json')
        try:
            history = json.loads(history_file.read_text(encoding='utf-8')) if history_file.exists() else []
        except Exception:
            history = []
        for job in jobs:
            history.append({
                'found_at': job.get('found_at', now_iso),
                'company': job['company'],
                'title': job['title'],
                'url': job.get('url', ''),
                'location': job.get('location', ''),
                'score': job.get('score', 0),
                'priority': job.get('priority', 'LOW'),
            })
        history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding='utf-8')

        # --- SQLite DB ---
        try:
            conn = sqlite3.connect('jobs.db')
            conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                found_at TEXT, company TEXT, title TEXT,
                url TEXT, location TEXT, score INTEGER, priority TEXT)''')
            for job in jobs:
                import hashlib
                job_id = hashlib.md5(f"{job.get('url','')}{job['title']}".encode()).hexdigest()
                conn.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?)', (
                    job_id,
                    job.get('found_at', now_iso), job['company'], job['title'],
                    job.get('url', ''), job.get('location', ''),
                    job.get('score', 0), job.get('priority', 'LOW')))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning('SQLite write failed: %s', e)

    # ATS slugs rot silently: a company moves boards and its URL starts 404ing while
    # the monitor keeps reporting a clean run. Re-probe every board on this cadence.
    _SLUG_CHECK_INTERVAL_DAYS = 7

    def _board_probe_url(self, url):
        """Return the URL that a slug check should hit for this company entry."""
        if url.startswith('lever://'):
            return f"https://api.lever.co/v0/postings/{url[len('lever://'):]}?mode=json"
        if url.startswith('smartrecruiters://'):
            return f"https://api.smartrecruiters.com/v1/companies/{url[len('smartrecruiters://'):]}/postings?limit=1"
        if url.startswith('workday://'):
            sub, wd_num, board = url[len('workday://'):].split('/')
            return f"https://{sub}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{sub}/{board}/jobs"
        if url.startswith(('netflix://', 'microsoft://', 'workable://')):
            return None            # search APIs, not per-company boards — nothing to validate
        return url

    def validate_slugs(self):
        """Probe every enabled board and report the ones that no longer resolve.

        Runs against the configured URL only — it does not guess replacements. The
        result is stored in state and surfaced on the dashboard so a dead board is
        visible instead of silently contributing zero jobs.

        Probes run in parallel across ATS groups, the same way run_once() fetches.
        The per-domain throttle still serializes requests within a group, so this
        stays as polite as the sequential version while overlapping the waits of
        boards hosted on different domains.
        """
        companies = [c for c in self.config['companies'] if c.get('enabled', True)]
        print()
        print(f"Validating {len(companies)} board URLs...")

        probes = [(c, self._board_probe_url(c['url'])) for c in companies]
        probes = [(c, probe) for c, probe in probes if probe]

        groups: dict[str, list] = defaultdict(list)
        for c, probe in probes:
            groups[self._ats_group(c['url'])].append((c, probe))

        def probe_group(group_probes):
            """Probe one ATS group sequentially; the throttle spaces same-domain calls."""
            return [self._probe_board(c, probe) for c, probe in group_probes]

        results = []
        with ThreadPoolExecutor(max_workers=min(12, len(groups) or 1)) as pool:
            futures = [pool.submit(probe_group, g) for g in groups.values()]
            for future in as_completed(futures):
                results.extend(future.result())

        # Sort by config order so the report reads the same way every run regardless
        # of which group's thread happened to finish first.
        order = {c['name']: i for i, c in enumerate(companies)}
        results.sort(key=lambda r: order.get(r['company'], 0))

        dead        = [{'company': r['company'], 'url': r['url']} for r in results if r['verdict'] == 'dead']
        unreachable = [{'company': r['company'], 'url': r['url']} for r in results if r['verdict'] == 'unreachable']
        for r in results:
            if r['verdict'] == 'dead':
                print(f"  ✗ {r['company']}: {r['url']}")
            elif r['verdict'] == 'unreachable':
                print(f"  ? {r['company']}: unreachable (not counted as dead)")

        result = {
            'ts': datetime.now().isoformat(timespec='seconds'),
            'checked': len(results),
            'dead': dead,
            'unreachable': unreachable,
        }
        self.state.setdefault('__meta__', {})['slug_check'] = result
        self.save_state()
        print(f"Slug check: {len(results)} probed, {len(dead)} dead")
        if dead:
            log.warning('Slug check found %d dead board(s): %s',
                        len(dead), ', '.join(d['company'] for d in dead))
        return result

    def _probe_board(self, company, probe):
        """Probe one board URL and classify the outcome.

        Returns {'company', 'url', 'verdict'} where verdict is 'alive', 'dead' or
        'unreachable'. Called from several threads at once, so it must not rely on
        the position of entries in the shared _fetch_errors list.
        """
        # Everything appended to _fetch_errors from here on is a candidate for this
        # probe. Other threads append too, so the mark alone is not enough to claim
        # an entry — it only rules out entries that are definitely older.
        with _fetch_errors_lock:
            mark = len(_fetch_errors)

        if 'myworkdayjobs.com' in probe:
            resp = self._http_post(probe, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        else:
            resp = self._http_get(probe)

        if resp is not None:
            return {'company': company['name'], 'url': company['url'], 'verdict': 'alive'}

        # A None response also covers timeouts and an open circuit breaker. Only a
        # 4xx means the slug itself is wrong — otherwise one provider outage would
        # condemn every company hosted on that domain.
        #
        # Match on the probe URL rather than on position. Probes run concurrently, so
        # a positional slice would sweep up another company's 404 and condemn a board
        # that is perfectly alive. Every retry of this probe appends its own entry, so
        # collect all of them: one attempt may time out before the next returns a 4xx.
        with _fetch_errors_lock:
            reasons = {e['error_type'] for e in _fetch_errors[mark:] if e['url'] == probe}

        verdict = 'dead' if '4xx' in reasons else 'unreachable'
        return {'company': company['name'], 'url': company['url'], 'verdict': verdict}

    def _slug_check_is_due(self):
        """True when the last slug validation is older than the check interval."""
        last = self.state.get('__meta__', {}).get('slug_check', {}).get('ts')
        if not last:
            return True
        try:
            return datetime.now() - datetime.fromisoformat(last) > timedelta(days=self._SLUG_CHECK_INTERVAL_DAYS)
        except ValueError:
            return True

    def _detect_stale_boards(self):
        """Flag boards that fetched successfully but returned nothing they used to.

        A dead slug 404s and lands in failed_companies. A board whose API changed
        shape, or whose listing silently emptied, returns 200 with zero postings —
        indistinguishable from a quiet company unless compared against last run.
        """
        meta = self.state.setdefault('__meta__', {})
        previous = meta.get('raw_counts', {})
        with self._raw_counts_lock:
            current = dict(self._raw_counts)

        stale = []
        for company, was in previous.items():
            now = current.get(company)
            if now is None:
                continue          # never fetched this run — a fetch error, already reported
            if was > 0 and now == 0:
                stale.append({'company': company, 'previous': was})
                log.warning('⚠ %s returned 0 postings (had %d last run) — board may have changed',
                            company, was)

        # Only remember companies actually checked, so a disabled one does not linger.
        meta['raw_counts'] = current
        meta['raw_counts_at'] = datetime.now().isoformat(timespec='seconds')
        return stale

    def save_html_report(self, jobs):
        """Append new jobs to history and write fixed-path per-run report."""
        if not jobs:
            return None
        self._append_history(jobs)
        jobs = [safe_report_job(job) for job in jobs]
        os.makedirs('reports', exist_ok=True)
        # Fixed path — overwritten each run so you always know where to look
        path = 'reports/jobs_latest.html'
        rows = ''.join(
            f"<tr class='{j['priority'].lower()}'>"
            f"<td>{j['company']}</td>"
            f"<td><a href='{j['url']}' target='_blank'>{j['title']}</a></td>"
            f"<td>{j.get('location', '')}</td>"
            f"<td>{j.get('score', '-')}</td>"
            f"<td><span class='badge'>{j['priority']}</span></td>"
            f"</tr>"
            for j in sorted(jobs, key=lambda x: x.get('score', 0), reverse=True)
        )
        html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Job Report {datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;color:#222}}
  h1{{font-size:1.4rem;margin-bottom:.5rem}}
  p.meta{{color:#666;font-size:.85rem;margin-bottom:1.5rem}}
  table{{width:100%;border-collapse:collapse;font-size:.9rem}}
  th{{background:#f0f0f0;text-align:left;padding:8px 12px;border-bottom:2px solid #ddd}}
  td{{padding:8px 12px;border-bottom:1px solid #eee;vertical-align:top}}
  tr.urgent td{{background:#fff3f3}} tr.high td{{background:#fff8f0}}
  tr.medium td{{background:#fffdf0}} tr:hover td{{background:#f5f5f5}}
  a{{color:#0066cc;text-decoration:none}} a:hover{{text-decoration:underline}}
  .badge{{font-size:.75rem;padding:2px 7px;border-radius:10px;font-weight:600;
    background:#eee;color:#555}}
  tr.urgent .badge{{background:#ffd5d5;color:#c00}}
  tr.high .badge{{background:#ffe5cc;color:#a50}}
  tr.medium .badge{{background:#fff5cc;color:#880}}
</style></head><body>
<h1>AI Job Report — This Run</h1>
<p class='meta'>Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &mdash; {len(jobs)} new role(s) &mdash;
  <a href='history.html'>View all history →</a></p>
<table><thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Score</th><th>Priority</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

        return Path(path).resolve()

    @staticmethod
    def _ats_group(url: str) -> str:
        """Bucket a company URL into its ATS group for parallel scheduling."""
        if 'ashbyhq.com' in url:       return 'ashby'
        if 'greenhouse.io' in url:     return 'greenhouse'
        if url.startswith('lever://'):  return 'lever'
        # Each Workday tenant is a distinct domain, so they need no shared serialization
        # (_polite_sleep already spaces per-domain). Separate groups let them overlap.
        if url.startswith('workday://'): return 'workday:' + url[len('workday://'):].split('/')[0]
        if url.startswith('netflix://'): return 'netflix'
        if url.startswith('microsoft://'): return 'microsoft'
        if 'amazon.jobs' in url:       return 'amazon'
        if url.startswith('smartrecruiters://'): return 'smartrecruiters'
        return 'other'

    def run_once(self):
        """Run one complete check cycle — ATS groups run in parallel, same-ATS requests serialized."""
        run_start = time.monotonic()
        run_ts = datetime.now().isoformat(timespec='seconds')
        # Wall-clock seconds per phase. Written to metrics.jsonl so a slow run can be
        # attributed from the dashboard instead of by reading gaps between log lines.
        phases: dict[str, float] = {}
        company_times: list[tuple[str, float]] = []
        group_times: dict[str, float] = {}

        reset_fetch_errors()
        with self._raw_counts_lock:
            self._raw_counts = {}

        print(f"\n{'='*60}")
        print(f"AI Job Monitor v2.0 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
        active = sum(1 for c in self.config['companies'] if c.get('enabled', True))
        parked = len(self.config['companies']) - active
        parked_note = f", {parked} parked" if parked else ""
        print(f"Monitoring {active} companies{parked_note} (parallel by ATS)")
        print(f"State expiry: {self.state_expiry_days} days")
        print(f"{'='*60}\n")

        if self._slug_check_is_due():
            print('Slug validation is due (runs every '
                  f'{self._SLUG_CHECK_INTERVAL_DAYS} days)')
            with _timed(phases, 'slug_check'):
                self.validate_slugs()

        # Group companies by ATS so we serialize within each domain but parallelize across ATSes
        groups: dict[str, list] = defaultdict(list)
        for c in self.config['companies']:
            if not c.get('enabled', True):
                continue
            groups[self._ats_group(c['url'])].append(c)

        all_new_jobs = []
        companies_errored = 0

        def run_group(group_companies):
            """Check all companies in one ATS group sequentially, timing each one."""
            results = []
            errs = 0
            timings = []
            for company_data in group_companies:
                started = time.monotonic()
                try:
                    new_jobs = self.check_company(company_data)
                    results.extend(new_jobs)
                    # State is saved once at the end of the run. Saving per company
                    # rewrote the entire file 100+ times from every worker thread.
                except Exception as e:
                    errs += 1
                    log.error("Error checking %s: %s", company_data['name'], e)
                    print(f"  ❌ Error checking {company_data['name']}: {e}")
                finally:
                    # Timed in a finally so a company that raises still reports the
                    # time it burned — an exception after a long timeout is exactly
                    # the case worth seeing on the dashboard.
                    timings.append((company_data['name'], round(time.monotonic() - started, 1)))
            return results, errs, timings

        # Run each ATS group in its own thread (max 8 concurrent groups)
        with _timed(phases, 'fetch'):
            with ThreadPoolExecutor(max_workers=min(12, len(groups))) as pool:
                futures = {pool.submit(run_group, companies): ats
                           for ats, companies in groups.items()}
                for future in as_completed(futures):
                    ats = futures[future]
                    try:
                        jobs, errs, timings = future.result()
                        all_new_jobs.extend(jobs)
                        companies_errored += errs
                        company_times.extend(timings)
                        # A group runs sequentially, so its own cost is the sum of its
                        # companies. This is what shows which ATS sets the run's floor.
                        group_times[ats] = round(sum(t for _, t in timings), 1)
                    except Exception as e:
                        print(f"  ❌ Group {ats} failed: {e}")
                        companies_errored += 1

        urgent_count = sum(1 for j in all_new_jobs if j.get('score', 0) >= 80)
        with _timed(phases, 'report'):
            report_path = self.save_html_report(all_new_jobs)

        self.last_run_at = datetime.now()
        with _timed(phases, 'state'):
            # Must run before save_state(): it records this run's board sizes into __meta__.
            stale_companies = self._detect_stale_boards()
            self.save_state()

        # Regenerate cumulative history page so it's ready to open without a server
        with _timed(phases, 'history'):
            try:
                import view_history
                history_path = view_history.render(view_history.load_all_jobs())
            except Exception as e:
                history_path = None
                log.warning('Could not regenerate history page: %s', e)

        duration_s = round(time.monotonic() - run_start, 1)
        circuit_open = [d for d, cb in _circuit.items()
                        if cb.get('opened_at') is not None
                        and time.monotonic() - cb['opened_at'] < _CB_COOLDOWN]

        # Map errored URLs → company names using config
        # Include both the raw config URL and the derived HTTP URL for custom schemes
        url_to_company = {}
        for c in self.config['companies']:
            url_to_company[c['url']] = c['name']
            cfg = c['url']
            if cfg.startswith('lever://'):
                slug = cfg[len('lever://'):]
                url_to_company[f'https://api.lever.co/v0/postings/{slug}'] = c['name']
            elif cfg.startswith('workday://'):
                parts = cfg[len('workday://'):].split('/')
                url_to_company[f'https://{parts[0]}.wd{parts[1] if len(parts)>1 else "5"}.myworkdayjobs.com'] = c['name']
            elif cfg.startswith('smartrecruiters://'):
                slug = cfg[len('smartrecruiters://'):]
                url_to_company[f'https://api.smartrecruiters.com/v1/companies/{slug}'] = c['name']
        with _fetch_errors_lock:
            snapshot = list(_fetch_errors)
        failed_companies = []
        seen_companies = set()
        for err in snapshot:
            # Match by exact URL or prefix (Greenhouse adds ?content=true variant)
            name = next(
                (url_to_company[cfg_url] for cfg_url in url_to_company
                 if err['url'].startswith(cfg_url) or cfg_url.startswith(err['url'])),
                err['url']  # fallback: show URL if no company matched
            )
            if name not in seen_companies:
                seen_companies.add(name)
                failed_companies.append({'company': name, 'error_type': err['error_type'], 'error': err['error']})

        _append_metrics({
            'ts': run_ts,
            'duration_s': duration_s,
            # Where duration_s went. Phases are wall-clock and do not sum to
            # duration_s: 'fetch' is the elapsed time of the whole parallel block,
            # while group_times/slow_companies are per-thread sums that overlap it.
            'phases': phases,
            'group_times': group_times,
            'slow_companies': [
                {'company': name, 'seconds': secs}
                for name, secs in sorted(company_times, key=lambda t: -t[1])[:_SLOW_COMPANY_COUNT]
            ],
            # Counts what was actually checked. Parked companies are excluded so
            # this does not silently inflate as the skip list grows.
            'companies_total': sum(1 for c in self.config['companies']
                                   if c.get('enabled', True)),
            'companies_parked': sum(1 for c in self.config['companies']
                                    if not c.get('enabled', True)),
            # companies_errored counts companies whose check raised. A 4xx or exhausted-retry
            # fetch returns None instead of raising, so those land only in failed_companies —
            # without adding them here a board that 404s every run reports as a clean run.
            'companies_errored': companies_errored + len(failed_companies),
            'failed_companies': failed_companies,
            'stale_companies': stale_companies,
            # Derived from config on every run rather than stored, so the health
            # card can never drift from what is actually being monitored.
            'disabled_companies': [
                {'company': c['name'],
                 'reason': c.get('disabled_reason', 'disabled, no reason recorded')}
                for c in self.config['companies'] if not c.get('enabled', True)
            ],
            'unresolved_companies': [
                {'company': c['name'],
                 'reason': f"{c.get('ats', 'unknown ATS')} — {c.get('reason', '')}".strip(' —')}
                for c in self.config.get('unsupported_ats', {}).get('companies', [])
            ],
            'slug_check': self.state.get('__meta__', {}).get('slug_check', {}),
            'new_jobs': len(all_new_jobs),
            'new_jobs_high': sum(1 for j in all_new_jobs if j.get('priority') == 'HIGH'),
            'new_jobs_med':  sum(1 for j in all_new_jobs if j.get('priority') == 'MEDIUM'),
            'new_jobs_low':  sum(1 for j in all_new_jobs if j.get('priority') == 'LOW'),
            'urgent': urgent_count,
            'circuit_open': circuit_open,
            'total_tracked': sum(
                len(v) for k, v in self.state.items()
                if k != '__meta__' and isinstance(v, dict)
            ),
        })

        print(f"\n{'='*60}")
        print(f"Summary:")
        print(f"  Total new jobs: {len(all_new_jobs)}")
        print(f"  🔥 URGENT: {urgent_count}")
        print(f"  ⏱ Duration: {duration_s}s")
        if phases:
            breakdown = "  ".join(f"{name} {secs}s" for name, secs in phases.items())
            print(f"     phases: {breakdown}")
        slowest = sorted(company_times, key=lambda t: -t[1])[:3]
        if slowest:
            print("     slowest: " + "  ".join(f"{n} {v}s" for n, v in slowest))
        if circuit_open:
            print(f"  🔴 Circuit open: {', '.join(circuit_open)}")
        # ── Company Health ────────────────────────────────────────────────────────
        failed_names = {fc['company'] for fc in failed_companies}
        now_dt = datetime.now()

        def _last_job_seen(company_name: str) -> str:
            """Return 'Xd ago' string for most recent first_seen in state, or 'never'."""
            jobs = self.state.get(company_name, {})
            if not jobs or not isinstance(jobs, dict):
                return 'never'
            dates = []
            for v in jobs.values():
                if isinstance(v, dict) and v.get('first_seen'):
                    try:
                        dates.append(datetime.fromisoformat(v['first_seen']))
                    except ValueError:
                        pass
            if not dates:
                return 'never'
            delta = now_dt - max(dates)
            if delta.days == 0:
                return 'today'
            return f"{delta.days}d ago"

        print(f"\n  ── Company Health ──────────────────────────────────")
        if failed_companies:
            print(f"  ✗ FAILED ({len(failed_companies)}):")
            for fc in failed_companies:
                tag = '404 wrong slug' if fc['error_type'] == '4xx' else 'network error'
                print(f"      {fc['company']:32s}  {tag}")

        # Passed: enabled companies not in failed list, sorted by last-job-seen.
        # Disabled boards are skipped entirely: they can never report a job, so
        # listing them would pin them at "last job: never" and read as a scraper
        # failure rather than the deliberate choice it is.
        passed = []
        for c in self.config['companies']:
            if c['name'] not in failed_names and c.get('enabled', True):
                passed.append((c['name'], _last_job_seen(c['name'])))
        # Sort: never first, then oldest days-ago first
        def _sort_key(item):
            ljs = item[1]
            if ljs == 'never': return 99999
            if ljs == 'today': return 0
            return int(ljs.replace('d ago', ''))
        passed.sort(key=_sort_key, reverse=True)

        stale = [(n, d) for n, d in passed if d == 'never' or (d != 'today' and _sort_key((n, d)) > 30)]
        healthy = [(n, d) for n, d in passed if (n, d) not in stale]
        print(f"  ✓ PASSED ({len(passed)})  —  {len(healthy)} healthy, {len(stale)} stale/unseen:")
        for name, ljs in stale:
            print(f"      {name:32s}  last job: {ljs}")
        if healthy:
            print(f"      ... {len(healthy)} companies active (jobs seen recently)")
        if history_path:
            print(f"  📋 History: file://{history_path}")
        elif report_path:
            print(f"  📄 Run report: file://{report_path}")
        print(f"{'='*60}\n")

        return all_new_jobs
    
    def run_continuous(self, interval_minutes=30):
        """Run continuously with specified check interval"""
        print(f"🚀 Starting continuous monitoring (every {interval_minutes} min)")
        print(f"📋 URGENT jobs (score 80+) logged")
        print(f"📧 Daily digest: 9 AM IST for others")
        print(f"🔄 State expiry: {self.state_expiry_days} days")
        print(f"Monitoring {len(self.config['companies'])} companies")
        print(f"Press Ctrl+C to stop\n")
        
        while True:
            try:
                self.run_once()
                print(f"⏳ Next check in {interval_minutes} minutes...")
                time.sleep(interval_minutes * 60)
            except KeyboardInterrupt:
                print("\n\n👋 Stopping monitor. Goodbye!")
                break
            except Exception as e:
                print(f"\n❌ Unexpected error: {e}")
                print(f"Retrying in {interval_minutes} minutes...")
                time.sleep(interval_minutes * 60)


def main():
    """Main entry point with CLI argument parsing"""
    parser = argparse.ArgumentParser(description='AI Job Monitor v2.0')
    parser.add_argument('--config', dest='config_file', default='config.json', help='Path to config JSON (default: config.json)')
    parser.add_argument('--env', dest='env_file', help='Path to .env file')
    parser.add_argument('--dry-run', action='store_true', help='Log actions without sending alerts')
    parser.add_argument('--once', action='store_true', help='Run one check cycle and exit')
    parser.add_argument('--interval', type=int, default=30, help='Minutes between checks (default: 30)')
    parser.add_argument('--validate-slugs', action='store_true',
                        help='Probe every configured board URL and report dead ones, then exit')
    args = parser.parse_args()

    monitor = JobMonitor(config_file=args.config_file, env_file=args.env_file, dry_run=args.dry_run)

    if args.validate_slugs:
        monitor.validate_slugs()
    elif args.once:
        monitor.run_once()
    else:
        monitor.run_continuous(args.interval)


if __name__ == '__main__':
    main()

