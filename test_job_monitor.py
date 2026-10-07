import unittest
import os
import warnings

from job_monitor import JobMonitor

warnings.filterwarnings("ignore", message=".*LibreSSL.*", category=Warning)


class JobMonitorTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = {
            'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN'),
            'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID')
        }
        os.environ['TELEGRAM_BOT_TOKEN'] = 'test-token'
        os.environ['TELEGRAM_CHAT_ID'] = '1'
        self.monitor = JobMonitor(config_file='config.template.json')
        self.monitor.location_filter = ['india', 'bangalore', 'hyderabad', 'remote']

    def tearDown(self):
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_calculate_job_score_high_priority(self):
        score = self.monitor.calculate_job_score("Principal AI Engineer, Agents", "high")
        self.assertEqual(score, 70)

    def test_matches_criteria(self):
        self.assertTrue(self.monitor.matches_criteria("Staff ML Engineer"))
        self.assertTrue(self.monitor.matches_criteria("Senior Machine Learning Engineer"))
        self.assertFalse(self.monitor.matches_criteria("Junior Sales Associate"))

    def test_matches_criteria_drops_domainless_titles(self):
        """A senior engineering title with no domain word is dropped.

        Allowing these through (domain OR seniority+role) pulled in 1297 of 1613
        jobs in a single run — backend, frontend, mobile, security, support.
        """
        self.assertFalse(self.monitor.matches_criteria("Staff Backend Engineer"))
        self.assertFalse(self.monitor.matches_criteria("Senior Engineer, IAM"))
        self.assertFalse(self.monitor.matches_criteria("Senior Physical Design Engineer"))

    def test_matches_criteria_keeps_domain_terms_added_for_recall(self):
        """Recall comes from the domain list, not from relaxing the rule."""
        self.assertTrue(self.monitor.matches_criteria("Member of Technical Staff"))
        self.assertTrue(self.monitor.matches_criteria("Senior GenAI Engineer"))
        self.assertTrue(self.monitor.matches_criteria("Staff ML Infrastructure Engineer"))

    def test_matches_criteria_still_drops_non_engineering(self):
        self.assertFalse(self.monitor.matches_criteria("Senior Accountant"))
        self.assertFalse(self.monitor.matches_criteria("Staff Recruiter"))
        self.assertFalse(self.monitor.matches_criteria("Product Marketing Manager"))

    def test_priority_levels(self):
        self.assertEqual(self.monitor.get_priority_level(80), "URGENT")
        self.assertEqual(self.monitor.get_priority_level(60), "HIGH")
        self.assertEqual(self.monitor.get_priority_level(40), "MEDIUM")
        self.assertEqual(self.monitor.get_priority_level(10), "LOW")

    def test_extract_jobs_from_html(self):
        html = """
        <html><body>
          <a class="job">Principal Machine Learning Engineer</a>
          <a class="job">Junior Sales Associate</a>
        </body></html>
        """
        jobs = self.monitor.extract_jobs("TestCo", "https://example.com", html)
        titles = [job["title"] for job in jobs]
        self.assertIn("Principal Machine Learning Engineer", titles)
        self.assertNotIn("Junior Sales Associate", titles)

    def test_extract_jobs_respects_max_age(self):
        self.monitor.max_job_age_days = 1
        html = """
        <html><body>
          <div class="job-card">Principal AI Engineer - 3 days ago</div>
          <div class="job-card">Principal AI Engineer - 1 day ago</div>
        </body></html>
        """
        jobs = self.monitor.extract_jobs("TestCo", "https://example.com", html)
        titles = [job["title"] for job in jobs]
        self.assertNotIn("Principal AI Engineer - 3 days ago", titles)

    def test_job_id_includes_url(self):
        html = """
        <html><body>
          <a class="job" href="/job/123">Staff ML Engineer</a>
          <a class="job" href="/job/456">Senior ML Engineer</a>
        </body></html>
        """
        jobs = self.monitor.extract_jobs("TestCo", "https://example.com", html)
        job_ids = {job["id"] for job in jobs}
        self.assertEqual(len(job_ids), 2)

    def test_template_has_no_personal_company_list(self):
        self.assertEqual(self.monitor.config['companies'], [])

    def test_dry_run_flag_default_false(self):
        self.assertFalse(self.monitor.dry_run)

    def test_bot_identity_off_by_default(self):
        self.assertFalse(self.monitor.bot_identity)
        ua = self.monitor._user_agent()
        self.assertIn('Mozilla', ua)

    def test_bot_identity_on(self):
        self.monitor.bot_identity = True
        ua = self.monitor._user_agent()
        self.assertIn('JobAlertBot', ua)

    def test_http_get_retry_on_failure(self):
        import unittest.mock as mock
        from requests.exceptions import ConnectionError
        self.monitor.max_retries = 2
        self.monitor.request_delay = 0
        with mock.patch('requests.get', side_effect=ConnectionError('fail')) as mock_get:
            with mock.patch('time.sleep'):
                result = self.monitor._http_get('http://example.com')
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 2)

    def test_polite_sleep_enforced(self):
        import job_monitor as jm
        import unittest.mock as mock
        jm._last_request.clear()
        with mock.patch('time.sleep') as mock_sleep:
            jm._polite_sleep('http://example.com', delay=1.0)
            jm._polite_sleep('http://example.com', delay=1.0)
        mock_sleep.assert_called()

    def test_workday_url_parsed(self):
        url = 'workday://nvidia/5/NVIDIAExternalCareerSite'
        _, rest = url.split('workday://', 1)
        parts = rest.split('/')
        self.assertEqual(parts[0], 'nvidia')
        self.assertEqual(parts[1], '5')
        self.assertEqual(parts[2], 'NVIDIAExternalCareerSite')

    def test_circuit_breaker_opens_after_threshold(self):
        import job_monitor as jm
        domain = 'cb-test-open.example.com'
        jm._circuit.pop(domain, None)
        for _ in range(jm._CB_THRESHOLD):
            jm._cb_record_failure(domain)
        self.assertIsNotNone(jm._circuit[domain]['opened_at'])
        self.assertTrue(jm._cb_is_open(domain))

    def test_circuit_breaker_closes_on_success(self):
        import job_monitor as jm
        domain = 'cb-test-close.example.com'
        jm._circuit[domain] = {'failures': 3, 'opened_at': jm.time.monotonic()}
        jm._cb_record_success(domain)
        self.assertFalse(jm._cb_is_open(domain))
        self.assertEqual(jm._circuit[domain]['failures'], 0)

    def test_circuit_breaker_silent_skip(self):
        import job_monitor as jm
        import unittest.mock as mock
        domain = 'cb-test-skip.example.com'
        jm._circuit[domain] = {'failures': 3, 'opened_at': jm.time.monotonic()}
        self.monitor.request_delay = 0
        with mock.patch('requests.get') as mock_get:
            result = self.monitor._http_get(f'http://{domain}/jobs')
        mock_get.assert_not_called()
        self.assertIsNone(result)

    def test_matches_location_india(self):
        self.assertTrue(self.monitor.matches_location('Bangalore, India'))
        self.assertTrue(self.monitor.matches_location('Hyderabad, India'))
        self.assertTrue(self.monitor.matches_location('India'))

    def test_matches_location_remote(self):
        self.assertTrue(self.monitor.matches_location('Remote'))
        self.assertTrue(self.monitor.matches_location('Remote - US or India'))

    def test_matches_location_unknown(self):
        self.assertTrue(self.monitor.matches_location(''))
        self.assertTrue(self.monitor.matches_location(None))

    def test_matches_location_rejects_us_eu(self):
        self.assertFalse(self.monitor.matches_location('San Francisco, CA'))
        self.assertFalse(self.monitor.matches_location('New York City'))
        self.assertFalse(self.monitor.matches_location('London, UK'))
        self.assertFalse(self.monitor.matches_location('Seattle, WA'))

    def test_config_has_keywords(self):
        self.assertIsInstance(self.monitor.kw_seniority, list)
        self.assertIsInstance(self.monitor.kw_domain, list)
        self.assertIsInstance(self.monitor.kw_role, list)
        self.assertIn('principal', self.monitor.kw_seniority)
        self.assertIn('ml', self.monitor.kw_domain)
        self.assertIn('engineer', self.monitor.kw_role)

    def test_custom_domain_keyword_matches(self):
        self.monitor.kw_domain = ['data engineering', 'etl']
        self.assertTrue(self.monitor.matches_criteria("Senior Data Engineering Lead"))
        self.assertFalse(self.monitor.matches_criteria("Senior ML Engineer"))

    def test_custom_role_keyword_matches(self):
        self.monitor.kw_role = ['manager', 'engineer']
        self.assertTrue(self.monitor.matches_criteria("ML Engineering Manager"))
        self.assertTrue(self.monitor.matches_criteria("Senior ML Engineer"))

    def test_empty_location_filter_allows_all(self):
        self.monitor.location_filter = []
        self.assertTrue(self.monitor.matches_location('San Francisco, CA'))
        self.assertTrue(self.monitor.matches_location('London, UK'))
        self.assertTrue(self.monitor.matches_location('Hyderabad, India'))

    def test_custom_location_filter(self):
        self.monitor.location_filter = ['germany', 'remote']
        self.assertTrue(self.monitor.matches_location('Berlin, Germany'))
        self.assertTrue(self.monitor.matches_location('Remote'))
        self.assertFalse(self.monitor.matches_location('San Francisco, CA'))
        self.assertTrue(self.monitor.matches_location('Unknown'))

    def test_circuit_breaker_cooldown_allows_probe(self):
        import job_monitor as jm
        domain = 'cb-test-cooldown.example.com'
        # opened_at far in the past (cooldown expired)
        jm._circuit[domain] = {'failures': 3, 'opened_at': jm.time.monotonic() - jm._CB_COOLDOWN - 1}
        self.assertFalse(jm._cb_is_open(domain))


class NewJobDetectionTests(unittest.TestCase):
    """Tests for the 'second run finds no new jobs' bug scenario."""

    def setUp(self):
        os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
        os.environ.setdefault('TELEGRAM_CHAT_ID', '1')
        self.monitor = JobMonitor(config_file='config.template.json', dry_run=True)
        self.monitor.location_filter = ['india', 'bangalore', 'hyderabad', 'remote']
        self.monitor.state = {}
        self.monitor.request_delay = 0

    def _make_greenhouse_postings(self, jobs):
        """Build a fake Greenhouse API response payload."""
        return [
            {'id': str(i), 'title': title, 'absolute_url': url,
             'location': {'name': location}}
            for i, (title, url, location) in enumerate(jobs)
        ]

    def _run_process_postings(self, company, postings):
        """Call _process_postings the way extract_jobs_greenhouse does."""
        return self.monitor._process_postings(
            company, postings,
            link_fn=lambda p: p.get('absolute_url', ''),
            location_fn=lambda p: (
                p['location']['name'] if isinstance(p.get('location'), dict)
                else str(p.get('location', ''))
            )
        )

    def _simulate_check(self, company, jobs_from_api):
        """
        Simulate check_company without HTTP: inject jobs directly and
        run the state-comparison logic that determines new_jobs.
        Returns the list of new jobs found.
        """
        postings = self._make_greenhouse_postings(jobs_from_api)
        jobs = self._run_process_postings(company, postings)

        with self.monitor._state_lock:
            company_state = dict(self.monitor.state.get(company, {}))

        import hashlib
        new_jobs = []
        now_iso = __import__('datetime').datetime.now().isoformat()
        for job in jobs:
            job_id = job['id']
            if job_id not in company_state:
                new_jobs.append(job)
                score = self.monitor.calculate_job_score(job['title'], 'medium', job.get('location', ''))
                company_state[job_id] = {
                    'first_seen': now_iso,
                    'last_seen': now_iso,
                    'title': job['title'],
                    'score': score,
                    'priority': self.monitor.get_priority_level(score),
                }
            else:
                company_state[job_id]['last_seen'] = now_iso

        with self.monitor._state_lock:
            self.monitor.state[company] = company_state

        return new_jobs

    # --- deduplication across runs ---

    def test_first_run_finds_all_matching_jobs(self):
        """All matching jobs are new on first run when state is empty."""
        jobs = [
            ('Senior ML Engineer', 'https://co.com/1', 'Bangalore, India'),
            ('Staff AI Researcher', 'https://co.com/2', 'Hyderabad, India'),
        ]
        new = self._simulate_check('TestCo', jobs)
        self.assertEqual(len(new), 2)

    def test_second_run_with_same_jobs_finds_nothing(self):
        """Run 2 with identical jobs to run 1 should find 0 new jobs."""
        jobs = [
            ('Senior ML Engineer', 'https://co.com/1', 'Bangalore, India'),
        ]
        self._simulate_check('TestCo', jobs)  # run 1
        new = self._simulate_check('TestCo', jobs)  # run 2
        self.assertEqual(len(new), 0, "Same jobs should not re-appear as new")

    def test_second_run_with_one_new_job_finds_exactly_one(self):
        """Run 2 with an additional new job should find exactly that one new job."""
        job_1 = ('Senior ML Engineer', 'https://co.com/1', 'Bangalore, India')
        job_2 = ('Principal AI Architect', 'https://co.com/2', 'Hyderabad, India')

        self._simulate_check('TestCo', [job_1])          # run 1: sees job_1
        new = self._simulate_check('TestCo', [job_1, job_2])  # run 2: job_2 is new
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]['title'], 'Principal AI Architect')

    def test_job_removed_then_reposted_counts_as_new(self):
        """A job that was removed from the board and re-posted with a new URL should count as new."""
        original = ('Senior ML Engineer', 'https://co.com/jobs/100', 'Bangalore, India')
        reposted = ('Senior ML Engineer', 'https://co.com/jobs/999', 'Bangalore, India')

        self._simulate_check('TestCo', [original])
        new = self._simulate_check('TestCo', [reposted])
        self.assertEqual(len(new), 1, "Same title + new URL = new job_id = should appear as new")

    def test_non_matching_job_never_appears(self):
        """Jobs that don't match criteria are never returned, even on first run."""
        jobs = [
            ('Junior Sales Associate', 'https://co.com/1', 'Bangalore, India'),
            ('Backend Software Engineer', 'https://co.com/2', 'Bangalore, India'),
        ]
        new = self._simulate_check('TestCo', jobs)
        self.assertEqual(len(new), 0)

    def test_location_filtered_job_does_not_appear(self):
        """Jobs outside the location filter are silently excluded and not tracked."""
        jobs = [
            ('Senior ML Engineer', 'https://co.com/1', 'San Francisco, CA'),
        ]
        new = self._simulate_check('TestCo', jobs)
        self.assertEqual(len(new), 0)

    def test_unknown_location_passes_through(self):
        """Jobs with empty/unknown location must pass the location filter (to avoid false negatives)."""
        jobs = [
            ('Senior ML Engineer', 'https://co.com/1', ''),
        ]
        new = self._simulate_check('TestCo', jobs)
        self.assertEqual(len(new), 1)

    def test_state_persists_across_simulated_runs(self):
        """Company state grows by one entry per unique new job across runs."""
        job_a = ('Senior ML Engineer', 'https://co.com/1', 'Bangalore, India')
        job_b = ('Staff AI Architect', 'https://co.com/2', 'Bangalore, India')
        self._simulate_check('TestCo', [job_a])
        self._simulate_check('TestCo', [job_a, job_b])
        self.assertEqual(len(self.monitor.state['TestCo']), 2)

    # --- is_job_too_old / last_run_at logic ---

    def test_is_job_too_old_respects_max_age(self):
        self.monitor.max_job_age_days = 2
        self.monitor.last_run_at = None
        self.assertFalse(self.monitor.is_job_too_old('Posted 1 day ago'))
        self.assertFalse(self.monitor.is_job_too_old('Posted 2 days ago'))
        self.assertTrue(self.monitor.is_job_too_old('Posted 3 days ago'))

    def test_is_job_too_old_before_last_run_is_filtered(self):
        """A job posted before the last run should be filtered (already covered in run 1)."""
        from datetime import datetime, timedelta
        self.monitor.max_job_age_days = 7
        self.monitor.last_run_at = datetime.now() - timedelta(hours=2)
        # "4 hours ago" → posted before last_run_at (2h ago) → too old
        self.assertTrue(self.monitor.is_job_too_old('Posted 4 hours ago'))

    def test_is_job_too_old_after_last_run_passes(self):
        """A job posted AFTER the last run should NOT be filtered (genuinely new)."""
        from datetime import datetime, timedelta
        self.monitor.max_job_age_days = 7
        self.monitor.last_run_at = datetime.now() - timedelta(hours=3)
        # "1 hour ago" → posted after last_run_at (3h ago) → not too old
        self.assertFalse(self.monitor.is_job_too_old('Posted 1 hour ago'))

    def test_is_job_too_old_no_age_in_text_passes(self):
        """If no relative age can be parsed, the job is NOT filtered (safe default)."""
        self.monitor.last_run_at = __import__('datetime').datetime.now()
        self.assertFalse(self.monitor.is_job_too_old('Great opportunity at a top company'))

    def test_is_job_too_old_minutes_granularity(self):
        """Jobs posted minutes ago are never filtered."""
        self.monitor.max_job_age_days = 2
        self.monitor.last_run_at = None
        self.assertFalse(self.monitor.is_job_too_old('Posted 30 minutes ago'))

    # --- Greenhouse API path (mocked HTTP) ---

    def test_greenhouse_new_job_in_second_run(self):
        """Full Greenhouse path: run 2 returns a new job the API didn't have in run 1."""
        import unittest.mock as mock

        run1_payload = {'jobs': [
            {'id': 1, 'title': 'Senior ML Engineer', 'absolute_url': 'https://co.com/1',
             'location': {'name': 'Bangalore, India'}},
        ]}
        run2_payload = {'jobs': [
            {'id': 1, 'title': 'Senior ML Engineer', 'absolute_url': 'https://co.com/1',
             'location': {'name': 'Bangalore, India'}},
            {'id': 2, 'title': 'Staff AI Architect', 'absolute_url': 'https://co.com/2',
             'location': {'name': 'Hyderabad, India'}},
        ]}

        company = 'GreenCo'
        url = 'https://boards-api.greenhouse.io/v1/boards/greenco/jobs'

        mock_resp1 = mock.MagicMock()
        mock_resp1.json.return_value = run1_payload
        mock_resp2 = mock.MagicMock()
        mock_resp2.json.return_value = run2_payload

        with mock.patch.object(self.monitor, '_http_get',
                               side_effect=[mock_resp1, mock_resp2]):
            jobs_run1 = self.monitor.extract_jobs_greenhouse(company, url)
            with self.monitor._state_lock:
                company_state = dict(self.monitor.state.get(company, {}))
            now = __import__('datetime').datetime.now().isoformat()
            for j in jobs_run1:
                if j['id'] not in company_state:
                    company_state[j['id']] = {'first_seen': now, 'last_seen': now, 'title': j['title']}
            with self.monitor._state_lock:
                self.monitor.state[company] = company_state

            jobs_run2 = self.monitor.extract_jobs_greenhouse(company, url)
            new_in_run2 = [j for j in jobs_run2 if j['id'] not in self.monitor.state.get(company, {})]

        self.assertEqual(len(new_in_run2), 1)
        self.assertEqual(new_in_run2[0]['title'], 'Staff AI Architect')

    # --- greenhouse:// shorthand (regression: it used to fall through to the
    #     HTML scraper and silently return zero jobs for every run) ---

    def test_greenhouse_shorthand_is_routed_to_the_greenhouse_extractor(self):
        """greenhouse://slug must dispatch like the other ATS shorthands, not fall through."""
        matched = [method for pattern, method in self.monitor._EXTRACTORS.items()
                   if pattern in 'greenhouse://observeai']
        self.assertEqual(matched, ['extract_jobs_greenhouse'])

    def test_greenhouse_shorthand_does_not_trigger_an_html_fetch(self):
        """A shorthand URL is not fetchable; check_company must skip the page fetch for it."""
        url = 'greenhouse://observeai'
        needs_html = not any(p in url for p in self.monitor._EXTRACTORS)
        self.assertFalse(needs_html)

    def test_greenhouse_slug_parsed_from_both_url_forms(self):
        """Full board URL and greenhouse:// shorthand must hit the same API endpoint."""
        import unittest.mock as mock

        payload = {'jobs': [
            {'id': 1, 'title': 'Senior ML Engineer', 'absolute_url': 'https://co.com/1',
             'location': {'name': 'Bangalore, India'}},
        ]}

        for url in ('https://boards-api.greenhouse.io/v1/boards/observeai/jobs',
                    'greenhouse://observeai',
                    'greenhouse://observeai/'):
            resp = mock.MagicMock()
            resp.json.return_value = payload
            with mock.patch.object(self.monitor, '_http_get', return_value=resp) as http_get:
                jobs = self.monitor.extract_jobs_greenhouse('Observe.AI', url)

            called_with = http_get.call_args[0][0]
            self.assertEqual(
                called_with,
                'https://boards-api.greenhouse.io/v1/boards/observeai/jobs?content=true',
                f'wrong API url for config form {url!r}')
            self.assertEqual(len(jobs), 1, f'no jobs extracted for config form {url!r}')

    # --- custom:// boards (companies running their own careers site) ---

    def test_custom_scheme_is_routed_to_the_custom_extractor(self):
        matched = [m for pattern, m in self.monitor._EXTRACTORS.items()
                   if pattern in 'custom://atlassian']
        self.assertEqual(matched, ['extract_jobs_custom'])

    def test_custom_board_postings_are_filtered_and_linked(self):
        """A custom board's postings go through the same filters as any ATS."""
        import unittest.mock as mock

        postings = [
            {'title': 'Principal Machine Learning Engineer',
             'location': 'Bengaluru - India', 'url': 'https://co.test/1'},
            {'title': 'Office Manager', 'location': 'Bengaluru - India',
             'url': 'https://co.test/2'},          # dropped: no domain keyword
        ]
        with mock.patch('custom_boards.fetch', return_value=postings):
            jobs = self.monitor.extract_jobs_custom('Atlassian', 'custom://atlassian')

        self.assertEqual([j['title'] for j in jobs],
                         ['Principal Machine Learning Engineer'])
        self.assertEqual(jobs[0]['url'], 'https://co.test/1')

    def test_custom_board_failures_do_not_raise(self):
        """An unknown slug or a site changing its API must not kill the run."""
        import unittest.mock as mock

        self.assertEqual(self.monitor.extract_jobs_custom('X', 'custom://nope'), [])
        with mock.patch('custom_boards.fetch', side_effect=ValueError('API changed')):
            self.assertEqual(
                self.monitor.extract_jobs_custom('Atlassian', 'custom://atlassian'), [])

    def test_every_custom_config_url_has_a_registered_board(self):
        """A custom:// entry in config must name a board custom_boards knows."""
        import json
        import custom_boards

        with open('config.template.json', encoding='utf-8') as fh:
            companies = json.load(fh)['companies']

        unknown = [c['name'] for c in companies
                   if c['url'].startswith('custom://')
                   and c['url'][len('custom://'):].strip('/') not in custom_boards.BOARDS]
        self.assertEqual(unknown, [], f'config names unregistered custom boards: {unknown}')

    def test_config_has_no_unroutable_ats_shorthand(self):
        """Every scheme:// url in config.json must match an extractor."""
        import json
        with open('config.template.json', encoding='utf-8') as fh:
            companies = json.load(fh)['companies']

        unroutable = [
            (c['name'], c['url']) for c in companies
            if '://' in c['url'] and not c['url'].startswith(('http://', 'https://'))
            and not any(p in c['url'] for p in self.monitor._EXTRACTORS)
        ]
        self.assertEqual(unroutable, [], f'config urls no extractor can handle: {unroutable}')


if __name__ == "__main__":
    unittest.main()


class ProbeBoardAttributionTests(unittest.TestCase):
    """_probe_board must attribute fetch errors to the right company.

    Probes run concurrently, so the shared _fetch_errors list interleaves entries
    from several boards. Attributing by list position (the pre-parallel approach)
    made one company's 404 condemn another company's healthy board.
    """

    def setUp(self):
        import job_monitor as jm
        self.jm = jm
        self.monitor = JobMonitor(config_file='config.template.json')
        self.monitor.location_filter = ['india', 'bangalore', 'hyderabad', 'remote']
        # Clear in place. Rebinding jm._fetch_errors would detach this module's
        # name from the list _record_fetch_error actually appends to.
        jm.reset_fetch_errors()

    def tearDown(self):
        self.jm.reset_fetch_errors()

    def _probe_returning_none(self, company_name, probe_url, errors_to_record):
        """Run _probe_board with the HTTP layer stubbed to fail."""
        def fake_get(url, **kwargs):
            for err_url, err_type in errors_to_record:
                self.jm._record_fetch_error(err_url, 'boom', err_type)
            return None

        self.monitor._http_get = fake_get
        return self.monitor._probe_board({'name': company_name, 'url': probe_url}, probe_url)

    def test_4xx_on_own_url_marks_board_dead(self):
        result = self._probe_returning_none(
            'Acme', 'https://example.test/acme',
            [('https://example.test/acme', '4xx')])
        self.assertEqual(result['verdict'], 'dead')

    def test_timeout_on_own_url_is_unreachable_not_dead(self):
        """A network failure must not condemn the slug — the board may be fine."""
        result = self._probe_returning_none(
            'Acme', 'https://example.test/acme',
            [('https://example.test/acme', 'network')])
        self.assertEqual(result['verdict'], 'unreachable')

    def test_another_companys_4xx_does_not_condemn_this_board(self):
        """The regression a positional slice would reintroduce."""
        result = self._probe_returning_none(
            'Acme', 'https://example.test/acme',
            [('https://example.test/other-company', '4xx'),
             ('https://example.test/acme', 'network')])
        self.assertEqual(result['verdict'], 'unreachable')

    def test_retries_on_own_url_are_all_collected(self):
        """One probe can append several entries; a late 4xx still counts."""
        result = self._probe_returning_none(
            'Acme', 'https://example.test/acme',
            [('https://example.test/acme', 'network'),
             ('https://example.test/acme', '4xx')])
        self.assertEqual(result['verdict'], 'dead')

    def test_stale_entry_for_same_url_is_ignored(self):
        """An entry recorded before the probe started must not leak into it."""
        self.jm._record_fetch_error('https://example.test/acme', 'old run', '4xx')
        result = self._probe_returning_none(
            'Acme', 'https://example.test/acme',
            [('https://example.test/acme', 'network')])
        self.assertEqual(result['verdict'], 'unreachable')
