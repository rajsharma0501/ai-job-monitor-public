import unittest
from report_safety import safe_report_job


class ReportSafetyTests(unittest.TestCase):
    def test_board_markup_cannot_become_html(self):
        job = safe_report_job({'title': "<script>alert(1)</script>",
                               'company': "x' onclick='alert(1)",
                               'url': 'javascript:alert(1)'})
        self.assertNotIn('<script>', job['title'])
        self.assertNotIn("'", job['company'])
        self.assertEqual(job['url'], '')

    def test_https_link_survives_with_attribute_escaping(self):
        job = safe_report_job({'url': 'https://example.com/jobs?a=1&b=2'})
        self.assertEqual(job['url'], 'https://example.com/jobs?a=1&amp;b=2')

    def test_invalid_style_and_priority_values_are_normalized(self):
        job = safe_report_job({'score': '0; color:red', 'priority': '<svg>'})
        self.assertEqual(job['score'], 0)
        self.assertEqual(job['priority'], 'LOW')
