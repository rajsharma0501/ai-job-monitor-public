"""Cross-cutting instrumentation: logging, rate limiting, circuit breaking, metrics.

None of this knows anything about job boards. It is the machinery that answers
"is the run healthy, how long did it take, and which remote is misbehaving" —
kept apart from job_monitor.py so the polling logic stays readable and so a new
measurement has one obvious home.

Shared mutable state lives here and is never rebound. Callers that need to empty
a collection call the reset helper rather than assigning a fresh object: a plain
`module._fetch_errors = []` would rebind only the caller's name and leave every
`from observability import ...` binding pointing at the abandoned list, which
loses errors silently.
"""

import json
import logging
import os
import random
import sys
import threading
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

# ── Module-level logging setup ───────────────────────────────────────────────
os.makedirs('logs', exist_ok=True)
log = logging.getLogger('job_monitor')
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
_fh = RotatingFileHandler('logs/job_monitor.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
_fh.setFormatter(_fmt)
log.addHandler(_fh)
try:
    _stderr_utf8 = open(sys.stderr.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
except Exception:
    _stderr_utf8 = sys.stderr
_ch = logging.StreamHandler(_stderr_utf8)
_ch.setFormatter(_fmt)
_ch.setLevel(logging.WARNING)
log.addHandler(_ch)


# ── Per-domain rate limiting ──────────────────────────────────────────────────
_last_request: dict = {}
_rate_lock = threading.Lock()

def _polite_sleep(url: str, delay: float = 1.5):
    domain = urlparse(url).netloc
    with _rate_lock:
        elapsed = time.monotonic() - _last_request.get(domain, 0)
        wait = delay - elapsed + random.uniform(0, 0.5)
        _last_request[domain] = time.monotonic() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


# ── Circuit breaker state (per domain) ───────────────────────────────────────
# { domain: {'failures': int, 'opened_at': float|None} }
_CB_THRESHOLD = 3          # failures before opening
_CB_COOLDOWN  = 7200       # seconds before probing again (2 hours)
_circuit: dict = {}

def reset_circuit():
    """Forget all circuit-breaker state, in place.

    Same contract as reset_fetch_errors: callers must never do
    `_circuit = {}`, which would leave _cb_record_failure mutating a dict
    nobody else can see.
    """
    _circuit.clear()

def _cb_is_open(domain: str) -> bool:
    """Return True if circuit is open AND cooldown hasn't expired yet."""
    cb = _circuit.get(domain)
    if not cb or cb['opened_at'] is None:
        return False
    if time.monotonic() - cb['opened_at'] >= _CB_COOLDOWN:
        return False  # cooldown expired → allow probe
    return True

def _cb_record_failure(domain: str):
    cb = _circuit.setdefault(domain, {'failures': 0, 'opened_at': None})
    cb['failures'] += 1
    if cb['failures'] >= _CB_THRESHOLD and cb['opened_at'] is None:
        cb['opened_at'] = time.monotonic()
        log.warning('🔴 Circuit OPEN for %s after %d failures — skipping for %dh',
                    domain, _CB_THRESHOLD, _CB_COOLDOWN // 3600)

def _cb_record_success(domain: str):
    cb = _circuit.get(domain)
    if cb and cb['opened_at'] is not None:
        log.info('🟢 Circuit CLOSED for %s — API recovered', domain)
    _circuit[domain] = {'failures': 0, 'opened_at': None}


# ── Per-run fetch error tracking (reset each run_once call) ──────────────────
_fetch_errors: list = []          # [{'url': str, 'error': str, 'error_type': '4xx'|'network'}]
_fetch_errors_lock = threading.Lock()

def _record_fetch_error(url: str, error: str, error_type: str):
    with _fetch_errors_lock:
        _fetch_errors.append({'url': url, 'error': error[:120], 'error_type': error_type})

def reset_fetch_errors():
    """Empty the per-run error list in place.

    Clears rather than rebinds so that every module holding a reference to this
    list keeps seeing the same object. Rebinding here would silently detach
    _record_fetch_error from the list the rest of the program reads.
    """
    with _fetch_errors_lock:
        _fetch_errors.clear()


# ── Run metrics ──────────────────────────────────────────────────────────────
_SLOW_COMPANY_COUNT = 10   # how many of the slowest boards to record per run
_METRICS_FILE = Path('logs/metrics.jsonl')

@contextmanager
def _timed(phases: dict, name: str):
    """Record the wall-clock seconds spent inside this block under phases[name].

    Phases are recorded even when the block raises, so a run that dies partway
    still reports where it got to.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        phases[name] = round(time.monotonic() - start, 1)

def _append_metrics(record: dict):
    """Append one run-level metrics record to logs/metrics.jsonl."""
    try:
        with open(_METRICS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        log.warning('Could not write metrics: %s', e)
