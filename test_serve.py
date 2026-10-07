"""
Production-robustness tests for serve.py.

Tests every failure path the /run → /status flow can hit:
- successful run
- failed run (non-zero exit)
- timeout
- subprocess exception
- double-click (already running)
- /log endpoint
- /status while running vs. idle
"""
import importlib
import json
import threading
import time
import unittest
import unittest.mock as mock
from http.client import HTTPConnection
from pathlib import Path


# ---------------------------------------------------------------------------
# Load serve module with its global state so we can reset between tests
# ---------------------------------------------------------------------------
import serve as _serve_mod


def _reset_state():
    with _serve_mod._lock:
        _serve_mod._state.update({
            'running': False, 'last': None, 'exit_code': None,
            'ok': None, 'new_jobs': None, 'error': None, 'log': '',
        })


# ---------------------------------------------------------------------------
# Helper: fake subprocess.CompletedProcess
# ---------------------------------------------------------------------------
def _fake_result(stdout='', stderr='', returncode=0):
    r = mock.MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


# ---------------------------------------------------------------------------
# Unit tests for _parse_new_jobs
# ---------------------------------------------------------------------------
class ParseNewJobsTests(unittest.TestCase):

    def test_parses_count(self):
        self.assertEqual(_serve_mod._parse_new_jobs('Total new jobs: 5'), 5)

    def test_parses_zero(self):
        self.assertEqual(_serve_mod._parse_new_jobs('Total new jobs: 0'), 0)

    def test_returns_none_when_missing(self):
        self.assertIsNone(_serve_mod._parse_new_jobs('Nothing matched'))

    def test_ignores_surrounding_text(self):
        text = '==== Summary ====\n  Total new jobs: 12\n  URGENT: 2\n'
        self.assertEqual(_serve_mod._parse_new_jobs(text), 12)


# ---------------------------------------------------------------------------
# Unit tests for _run_monitor (the background worker function)
# ---------------------------------------------------------------------------
class RunMonitorWorkerTests(unittest.TestCase):

    def setUp(self):
        _reset_state()

    def _run_and_wait(self):
        t = threading.Thread(target=_serve_mod._run_monitor, daemon=True)
        t.start()
        t.join(timeout=5)

    def test_success_sets_ok_true(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('Total new jobs: 3', returncode=0)):
            self._run_and_wait()
        self.assertTrue(_serve_mod._state['ok'])
        self.assertEqual(_serve_mod._state['exit_code'], 0)
        self.assertEqual(_serve_mod._state['new_jobs'], 3)
        self.assertIsNone(_serve_mod._state['error'])
        self.assertFalse(_serve_mod._state['running'])

    def test_failure_sets_ok_false(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('Traceback...', returncode=1)):
            self._run_and_wait()
        self.assertFalse(_serve_mod._state['ok'])
        self.assertEqual(_serve_mod._state['exit_code'], 1)
        self.assertIn('1', _serve_mod._state['error'])

    def test_timeout_sets_error(self):
        import subprocess
        with mock.patch('subprocess.run',
                        side_effect=subprocess.TimeoutExpired('cmd', 600)):
            self._run_and_wait()
        self.assertFalse(_serve_mod._state['ok'])
        self.assertEqual(_serve_mod._state['exit_code'], -1)
        self.assertIn('imed out', _serve_mod._state['error'])

    def test_unexpected_exception_sets_error(self):
        with mock.patch('subprocess.run', side_effect=OSError('disk full')):
            self._run_and_wait()
        self.assertFalse(_serve_mod._state['ok'])
        self.assertIn('disk full', _serve_mod._state['error'])

    def test_running_flag_cleared_after_success(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result(returncode=0)):
            self._run_and_wait()
        self.assertFalse(_serve_mod._state['running'])

    def test_running_flag_cleared_after_failure(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result(returncode=2)):
            self._run_and_wait()
        self.assertFalse(_serve_mod._state['running'])

    def test_log_captured_from_stdout_and_stderr(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('STDOUT', 'STDERR', returncode=0)):
            self._run_and_wait()
        self.assertIn('STDOUT', _serve_mod._state['log'])
        self.assertIn('STDERR', _serve_mod._state['log'])

    def test_last_timestamp_set_after_run(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result(returncode=0)):
            self._run_and_wait()
        self.assertIsNotNone(_serve_mod._state['last'])

    def test_zero_new_jobs_on_success(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('Total new jobs: 0', returncode=0)):
            self._run_and_wait()
        self.assertEqual(_serve_mod._state['new_jobs'], 0)
        self.assertTrue(_serve_mod._state['ok'])

    def test_new_jobs_none_when_not_in_output(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('some other output', returncode=0)):
            self._run_and_wait()
        self.assertIsNone(_serve_mod._state['new_jobs'])


# ---------------------------------------------------------------------------
# Integration tests: spin up the real HTTPServer on a free port
# ---------------------------------------------------------------------------
import socket

def _free_port():
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


class ServerIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        from http.server import HTTPServer
        cls.server = HTTPServer(('', cls.port), _serve_mod.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _reset_state()

    def _get(self, path):
        conn = HTTPConnection('localhost', self.port, timeout=5)
        conn.request('GET', path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    def _post(self, path):
        conn = HTTPConnection('localhost', self.port, timeout=5)
        conn.request('POST', path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body

    def test_cross_origin_run_is_rejected(self):
        conn = HTTPConnection('localhost', self.port, timeout=5)
        with mock.patch('subprocess.run') as run:
            conn.request('POST', '/run', headers={'Origin': 'https://example.com'})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            run.assert_not_called()
        conn.close()

    def test_untrusted_host_cannot_read_status(self):
        conn = HTTPConnection('localhost', self.port, timeout=5)
        conn.request('GET', '/status', headers={'Host': 'example.com'})
        response = conn.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        conn.close()

    def test_status_idle(self):
        status, body = self._get('/status')
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertFalse(d['running'])
        self.assertIsNone(d['last'])

    def test_status_includes_all_fields(self):
        _, body = self._get('/status')
        d = json.loads(body)
        for key in ('running', 'last', 'exit_code', 'ok', 'new_jobs', 'error', 'log_tail'):
            self.assertIn(key, d, f'Missing field: {key}')

    def test_log_endpoint_empty(self):
        status, body = self._get('/log')
        self.assertEqual(status, 200)
        self.assertIn(b'no log', body.lower())

    def test_404_for_unknown_path(self):
        status, _ = self._get('/nonexistent')
        self.assertEqual(status, 404)

    def test_post_run_returns_202(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('Total new jobs: 2', returncode=0)):
            status, body = self._post('/run')
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)['status'], 'started')

    def test_post_run_while_running_returns_409(self):
        """Second POST /run while a run is in-flight must return 409."""
        gate = threading.Event()

        def slow_run(*a, **kw):
            gate.wait(timeout=5)
            return _fake_result(returncode=0)

        with mock.patch('subprocess.run', side_effect=slow_run):
            # First call starts the run (202); running flag set before thread spawns
            self._post('/run')
            time.sleep(0.05)  # give handler time to set running=True
            status, body = self._post('/run')
        gate.set()  # let background thread finish

        self.assertEqual(status, 409)
        self.assertIn('already running', json.loads(body)['error'])

    def test_run_success_reflected_in_status(self):
        event = threading.Event()

        def fake_run(*a, **kw):
            event.wait(timeout=3)
            return _fake_result('Total new jobs: 7', returncode=0)

        with mock.patch('subprocess.run', side_effect=fake_run):
            self._post('/run')
            _, body = self._get('/status')
            d = json.loads(body)
            self.assertTrue(d['running'])
            event.set()
            # wait for worker to finish
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, body = self._get('/status')
                if not json.loads(body)['running']:
                    break
                time.sleep(0.1)
        d = json.loads(body)
        self.assertFalse(d['running'])
        self.assertTrue(d['ok'])
        self.assertEqual(d['new_jobs'], 7)
        self.assertEqual(d['exit_code'], 0)

    def test_run_failure_reflected_in_status(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('crash output', returncode=1)):
            self._post('/run')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, body = self._get('/status')
                if not json.loads(body)['running']:
                    break
                time.sleep(0.1)
        d = json.loads(body)
        self.assertFalse(d['ok'])
        self.assertEqual(d['exit_code'], 1)
        self.assertIsNotNone(d['error'])

    def test_log_endpoint_after_run(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('hello log', returncode=0)):
            self._post('/run')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, body = self._get('/status')
                if not json.loads(body)['running']:
                    break
                time.sleep(0.1)
        _, log_body = self._get('/log')
        self.assertIn(b'hello log', log_body)

    def test_log_tail_in_status_after_run(self):
        with mock.patch('subprocess.run',
                        return_value=_fake_result('visible output', returncode=0)):
            self._post('/run')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                _, body = self._get('/status')
                if not json.loads(body)['running']:
                    break
                time.sleep(0.1)
        d = json.loads(body)
        self.assertIn('visible output', d['log_tail'])


if __name__ == '__main__':
    unittest.main()
