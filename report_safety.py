"""Prepare untrusted board fields for insertion into HTML reports."""
from html import escape
from urllib.parse import urlsplit


def safe_report_job(job):
    result = dict(job)
    for key in ('company', 'title', 'location', 'found_at'):
        result[key] = escape(str(job.get(key) or ''), quote=True)
    url = str(job.get('url') or '')
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ('https', 'http') and bool(parsed.netloc)
    except ValueError:
        valid = False
    result['url'] = escape(url, quote=True) if valid else ''
    priority = job.get('priority')
    result['priority'] = priority if priority in ('URGENT', 'HIGH', 'MEDIUM', 'LOW') else 'LOW'
    try:
        result['score'] = max(0, min(100, int(job.get('score') or 0)))
    except (TypeError, ValueError, OverflowError):
        result['score'] = 0
    return result
