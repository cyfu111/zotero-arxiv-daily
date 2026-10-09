from datetime import date
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from digest import (HttpClient, fetch_arxiv, fetch_arxiv_api, fetch_arxiv_daily,
                    failure_label, new_papers)
from test_digest import FakeClient

TODAY = date(2026, 10, 9)
SINCE = date(2026, 10, 2)


def feed(*entries, total=None):
    counter = f'<o:totalResults>{total}</o:totalResults>' if total is not None else ''
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/">{counter}' + ''.join(entries) + '</feed>').encode()


def entry(identifier='2610.12345', *, daily=True, kind='new', stamp='2026-10-09', summary='Abstract text'):
    raw_id = 'oai:arXiv.org:' + identifier + 'v2' if daily else 'http://arxiv.org/abs/' + identifier + 'v2'
    return f'''<entry><id>{raw_id}</id><title>Magnetic spin transport</title>
    <summary>{summary}</summary><published>{stamp}T00:00:00-04:00</published>
    <arxiv:announce_type>{kind}</arxiv:announce_type><arxiv:DOI>10.1234/ABC</arxiv:DOI>
    <dc:creator>A Author, B Author</dc:creator><author><name>A Author</name></author></entry>'''


class ArxivTests(TestCase):
    def test_daily_without_api_roundtrip_parses_metadata(self):
        client = FakeClient([feed(entry(summary='arXiv:2610.12345v2 Announce Type: new \nAbstract: Spin dynamics'))])
        papers, warnings = fetch_arxiv_daily(client, 'cond-mat.mes-hall+cond-mat.str-el', SINCE, TODAY)
        p = papers[0]
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], 'https://rss.arxiv.org/atom/cond-mat.mes-hall+cond-mat.str-el')
        self.assertEqual(p.identifier, 'arxiv:2610.12345')
        self.assertEqual(p.summary, 'Spin dynamics')
        self.assertEqual(p.doi, '10.1234/abc')
        self.assertEqual(p.author_names, ['A Author', 'B Author'])
        self.assertFalse(warnings)

    def test_daily_stale_future_replacements_filtered_duplicates_removed(self):
        body = feed(entry(), entry(), entry('2610.10001', kind='cross'),
                    entry('2610.10002', kind='replace'), entry('2610.10003', kind='replace-cross'),
                    entry('2610.10004', stamp='2026-09-30'), entry('2610.10005', stamp='2026-10-10'))
        papers, _ = fetch_arxiv_daily(FakeClient([body]), 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertEqual({p.identifier for p in papers}, {'arxiv:2610.12345', 'arxiv:2610.10001'})

    def test_api_outage_keeps_daily_and_warns_window_incomplete(self):
        client = FakeClient([feed(entry()), HTTPError('https://export.arxiv.org/api/query', 503, 'busy', {}, None)])
        papers, warnings = fetch_arxiv(client, 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertEqual(len(papers), 1)
        self.assertIn('daily feed', papers[0].source)
        self.assertIn('HTTP 503', warnings[0])
        self.assertIn('recovery window is incomplete', warnings[0])
        self.assertEqual(len(client.calls), 2)

    def test_empty_daily_and_failed_api_never_claims_empty_success(self):
        papers, warnings = fetch_arxiv(FakeClient([feed(), TimeoutError()]), 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertEqual(papers, [])
        self.assertIn('0 records only', warnings[0])
        self.assertIn('incomplete', warnings[0])

    def test_api_covers_daily_outage_and_deduplicates_versions(self):
        client = FakeClient([TimeoutError(), feed(entry(daily=False), entry(daily=False))])
        papers, warnings = fetch_arxiv(client, 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, 'arXiv')
        self.assertIn('using API', warnings[0])

    def test_both_sources_unavailable_fail_with_status(self):
        with self.assertRaisesRegex(RuntimeError, 'API failed \\(HTTP 429\\)'):
            fetch_arxiv(FakeClient([TimeoutError(), HTTPError('https://x', 429, '', {}, None)]), 'cond-mat.mes-hall', SINCE, TODAY)

    def test_html_200_and_api_error_are_not_valid_empty_feeds(self):
        for body in [b'<html><body>error</body></html>', feed('<entry><id>https://arxiv.org/api/errors</id><title>Error</title></entry>')]:
            with self.assertRaises(ValueError):
                fetch_arxiv_api(FakeClient([body]), 'cond-mat.mes-hall', SINCE, TODAY)
            with self.assertRaises(ValueError):
                fetch_arxiv_daily(FakeClient([body]), 'cond-mat.mes-hall', SINCE, TODAY)

    def test_short_api_page_uses_returned_offset(self):
        client = FakeClient([feed(entry(daily=False), total=2), feed(entry('2610.12346', daily=False), total=2)])
        papers, warnings = fetch_arxiv_api(client, 'cond-mat.mes-hall', SINCE, TODAY, 100)
        self.assertEqual(len(papers), 2)
        self.assertEqual(client.calls[1][1]['start'], 1)
        self.assertFalse(warnings)

    def test_empty_api_page_before_total_is_partial(self):
        papers, warnings = fetch_arxiv_api(FakeClient([feed(total=3)]), 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertFalse(papers)
        self.assertIn('incomplete', warnings[0])

    def test_merge_prefers_api_and_retains_same_delivery_identity(self):
        papers, _ = fetch_arxiv(FakeClient([feed(entry()), feed(entry(daily=False))]), 'cond-mat.mes-hall', SINCE, TODAY)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].source, 'arXiv')
        self.assertFalse(new_papers(papers, {'sent': {'arxiv:2610.12345': '2026-10-08'}}))

    def test_invalid_category_does_not_make_requests(self):
        client = FakeClient([])
        with self.assertRaises(ValueError):
            fetch_arxiv(client, 'cat:cond-mat.mes-hall OR all:spin', SINCE, TODAY)
        self.assertFalse(client.calls)

    def test_daily_cap_warns(self):
        papers, warnings = fetch_arxiv_daily(FakeClient([feed(entry(), entry('2610.12346'))]), 'cond-mat.mes-hall', SINCE, TODAY, 1)
        self.assertEqual(len(papers), 1)
        self.assertTrue(warnings)

    def test_status_logs_do_not_include_query_or_error_body(self):
        def opener(*args, **kwargs):
            raise HTTPError('https://export.arxiv.org/api/query?secret=value', 503, 'secret-body', {}, None)
        with self.assertLogs('digest', level='WARNING') as captured:
            with self.assertRaises(HTTPError):
                HttpClient(opener=opener, attempts=1).get('https://export.arxiv.org/api/query', {'search_query': 'secret-value'})
        output = ' '.join(captured.output)
        self.assertIn('HTTP 503', output)
        self.assertIn('export.arxiv.org /api/query', output)
        self.assertNotIn('secret', output)

    def test_daily_failure_diagnostics_redact_categories(self):
        def opener(*args, **kwargs):
            raise HTTPError('https://rss.arxiv.org/atom/private-category', 503, 'busy', {}, None)
        with self.assertLogs('digest', level='WARNING') as captured:
            with self.assertRaises(HTTPError):
                HttpClient(opener=opener, attempts=1).get('https://rss.arxiv.org/atom/private-category')
        output = ' '.join(captured.output)
        self.assertIn('/atom/[categories]', output)
        self.assertNotIn('private-category', output)

    def test_smoke_requires_real_records_and_api_mode_is_stricter(self):
        import smoke_arxiv
        daily, _ = fetch_arxiv_daily(FakeClient([feed(entry())]), 'cond-mat.mes-hall', SINCE, TODAY)
        with patch('smoke_arxiv.fetch_arxiv', return_value=([], [])), patch('builtins.print'):
            self.assertEqual(smoke_arxiv.main([]), 1)
        with patch('smoke_arxiv.fetch_arxiv', return_value=(daily, ['API incomplete'])), patch('builtins.print'):
            self.assertEqual(smoke_arxiv.main([]), 0)
            self.assertEqual(smoke_arxiv.main(['--require-api']), 1)

    def test_arxiv_interfaces_share_serial_pacing(self):
        now, sleeps = [0.0], []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'ok'
        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay
        client = HttpClient(opener=lambda *a, **k: Response(), clock=lambda: now[0], sleep=sleep)
        client.get('https://rss.arxiv.org/atom/cond-mat.mes-hall', interval=3.1)
        client.get('https://export.arxiv.org/api/query', interval=3.1)
        self.assertEqual(sleeps, [3.1])
