#!/usr/bin/env python3
"""
Local dashboard server for AI Job Monitor.

Usage:
    python serve.py          # serves dashboard at http://localhost:8765
    python serve.py --port 9000

Endpoints:
    GET  /         → regenerate and serve the history dashboard
    POST /run      → trigger one monitor scan (non-blocking, 202)
    GET  /status   → {running, last, exit_code, ok, new_jobs, error, log_tail}
    GET  /log      → full plain-text log of the last run
"""
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PORT = 8765

# Shared run state — only mutated under _lock
_state = {
    'running': False,
    'last': None,       # ISO datetime of last completed run
    'exit_code': None,  # 0 = success, non-zero = failure, None = never run
    'ok': None,         # True/False/None
    'new_jobs': None,   # int parsed from stdout, or None
    'error': None,      # human-readable error string, or None
    'log': '',          # full captured output (stdout + stderr)
}
_lock = threading.Lock()


def _parse_new_jobs(text):
    """Extract 'Total new jobs: N' from monitor stdout."""
    m = re.search(r'Total new jobs:\s*(\d+)', text)
    return int(m.group(1)) if m else None


class Handler(BaseHTTPRequestHandler): #Request handler

    def _local_request(self):
        """Reject browser requests from other sites and DNS rebinding hosts."""
        allowed = {f'localhost:{self.server.server_port}',
                   f'127.0.0.1:{self.server.server_port}'}
        if self.headers.get('Host') not in allowed:
            self._send(403, 'Local dashboard requests only.', 'text/plain')
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{host}' for host in allowed}:
            self._send(403, 'Cross-origin requests are disabled.', 'text/plain')
            return False
        return True

    def _send(self, code, body, content_type='text/html; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b'', 'text/plain')

    def do_GET(self):
        if not self._local_request():
            return
        if self.path in ('/', '/index.html'):
            try:
                subprocess.run(
                    [sys.executable, 'view_history.py', '--no-open'],
                    capture_output=True, timeout=30,
                    cwd=str(Path(__file__).resolve().parent),
                )
            except Exception:
                pass
            p = Path('reports/history.html')
            if p.exists():
                self._send(200, p.read_bytes())
            else:
                self._send(404, 'No dashboard yet — run the monitor once first.')

        elif self.path == '/status':
            with _lock:
                payload = {
                    'running':   _state['running'],
                    'last':      _state['last'],
                    'exit_code': _state['exit_code'],
                    'ok':        _state['ok'],
                    'new_jobs':  _state['new_jobs'],
                    'error':     _state['error'],
                    'log_tail':  _state['log'][-2000:] if _state['log'] else '',
                }
            self._send(200, json.dumps(payload), 'application/json')

        elif self.path == '/log':
            with _lock:
                log = _state['log'] or '(no log yet)'
            self._send(200, log, 'text/plain; charset=utf-8')

        elif self.path == '/metrics':
            metrics_file = Path('logs/metrics.jsonl')
            records = []
            if metrics_file.exists():
                for line in metrics_file.read_text(encoding='utf-8', errors='replace').splitlines():
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            self._send(200, json.dumps(records), 'application/json')

        elif self.path == '/jobs':
            state_file = Path('job_state.json')
            stats = {'companies': 0, 'total': 0, 'high': 0, 'med': 0, 'low': 0, 'by_company': []}
            try:
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding='utf-8', errors='replace'))
                    for company, jobs in state.items():
                        if company == '__meta__' or not isinstance(jobs, dict):
                            continue
                        stats['companies'] += 1
                        co_high = sum(1 for j in jobs.values() if j.get('priority') == 'HIGH')
                        co_med  = sum(1 for j in jobs.values() if j.get('priority') == 'MEDIUM')
                        co_low  = sum(1 for j in jobs.values() if j.get('priority') == 'LOW')
                        stats['total'] += len(jobs)
                        stats['high']  += co_high
                        stats['med']   += co_med
                        stats['low']   += co_low
                        dates = [v['first_seen'] for v in jobs.values()
                                 if isinstance(v, dict) and v.get('first_seen')]
                        last_seen = max(dates) if dates else None
                        stats['by_company'].append({
                            'company': company, 'count': len(jobs),
                            'high': co_high, 'med': co_med, 'low': co_low,
                            'last_seen': last_seen,
                        })
                    stats['by_company'].sort(key=lambda x: x['count'], reverse=True)
            except Exception:
                pass  # mid-write race condition — return partial/empty stats
            self._send(200, json.dumps(stats), 'application/json')

        elif self.path == '/logfile':
            log_file = Path('logs/job_monitor.log')
            metrics_file = Path('logs/metrics.jsonl')
            lines = []
            if log_file.exists():
                all_lines = log_file.read_text(encoding='utf-8', errors='replace').splitlines()
                cutoff = None
                if metrics_file.exists():
                    records = []
                    for ln in metrics_file.read_text(encoding='utf-8', errors='replace').splitlines():
                        ln = ln.strip()
                        if ln:
                            try:
                                records.append(json.loads(ln))
                            except json.JSONDecodeError:
                                pass
                    if records:
                        anchor = records[-5] if len(records) >= 5 else records[0]
                        cutoff = anchor['ts'].replace('T', ' ')  # "2026-08-27 17:25:24"
                if cutoff:
                    lines = [l for l in all_lines if len(l) >= 19 and l[:19] >= cutoff]
                else:
                    lines = all_lines
            self._send(200, json.dumps(lines), 'application/json')

        elif self.path in ('/obs', '/obs/'):
            p = Path(__file__).resolve().parent / 'observability.html'
            if p.exists():
                self._send(200, p.read_bytes(), 'text/html; charset=utf-8')
            else:
                self._send(404, 'observability.html not found — run setup first.')

        else:
            self._send(404, 'Not found.')

    def do_POST(self):
        if not self._local_request():
            return
        if self.path == '/run':
            with _lock:
                if _state['running']:
                    self._send(409, json.dumps({'error': 'already running'}),
                               'application/json')
                    return
                _state['running'] = True
                _state['log'] = ''
                _state['error'] = None
                _state['ok'] = None
                _state['exit_code'] = None
                _state['new_jobs'] = None

            threading.Thread(target=_run_monitor, daemon=True).start()
            self._send(202, json.dumps({'status': 'started'}), 'application/json')
        else:
            self._send(404, 'Not found.')

    def log_message(self, fmt, *args):  # suppress per-request noise
        pass


def _run_monitor():
    """Run job_monitor.py --once in a subprocess; update _state when done."""
    try:
        env = {**__import__('os').environ, 'PYTHONUTF8': '1'}
        result = subprocess.run(
            [sys.executable, 'job_monitor.py', '--once'],
            capture_output=True, text=True, timeout=600,
            encoding='utf-8', errors='replace',
            cwd=str(Path(__file__).resolve().parent),
            env=env,
        )
        combined = (result.stdout or '') + (result.stderr or '')
        exit_code = result.returncode
        ok = (exit_code == 0)
        new_jobs = _parse_new_jobs(combined)
        error = None if ok else f'Monitor exited with code {exit_code}.'

    except subprocess.TimeoutExpired:
        combined = 'Run timed out after 10 minutes.'
        exit_code = -1
        ok = False
        new_jobs = None
        error = 'Timed out after 10 minutes.'

    except Exception as exc:
        combined = str(exc)
        exit_code = -1
        ok = False
        new_jobs = None
        error = f'Unexpected error: {exc}'

    with _lock:
        _state['running'] = False
        _state['last'] = datetime.now().isoformat()
        _state['exit_code'] = exit_code
        _state['ok'] = ok
        _state['new_jobs'] = new_jobs
        _state['error'] = error
        _state['log'] = combined


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=PORT)
    args = p.parse_args()

    server = HTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Dashboard → http://localhost:{args.port}')
    print('Press Ctrl+C to stop.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
