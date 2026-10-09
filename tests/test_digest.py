import json
from datetime import date
from email.message import Message
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from digest import HttpClient, Paper, fetch_arxiv_api, fetch_crossref, fetch_rss, new_papers, load_state
from main import parse_args, run
from construct_email import render_email, send_email

TODAY = date(2026, 10, 8)


def paper(**kwargs):
    data = dict(title='Berry curvature contribution to nonlinear spin transport in magnetic crystals', summary='Theory of spin transport', url='https://doi.org/10.1234/a', source='Physical Review B', doi='10.1234/a', author_names=['A Author'])
    data.update(kwargs)
    return Paper(**data)


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


class DiscoveryTests(unittest.TestCase):
    def test_doi_and_preprint_alias_dedup(self):
        p = paper()
        duplicate = paper(doi='https://doi.org/10.1234/A', url='https://arxiv.org/abs/1234.5', identifier='arxiv:1234.5')
        title_only = paper(doi='', identifier='arxiv:1234.6')
        other_author = paper(doi='', identifier='arxiv:1234.7', author_names=['B Author'])
        self.assertEqual(new_papers([p, duplicate, title_only, other_author], {'sent': {}}), [p, other_author])

    def test_short_titles_are_not_overdeduplicated(self):
        p, q = paper(title='Introduction', doi='10.1234/a'), paper(title='Introduction', doi='10.1234/b')
        self.assertEqual(len(new_papers([p, q], {'sent': {}})), 2)

    def test_arxiv_atom_recovers_window_without_rss_lookup(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>http://arxiv.org/abs/2610.00001v2</id><title>Transport</title><summary>Abstract</summary><published>2026-10-07T00:00:00Z</published><author><name>A</name></author><arxiv:doi>10.1234/ABC</arxiv:doi></entry></feed>'''
        client = FakeClient([xml])
        papers, notices = fetch_arxiv_api(client, 'cond-mat.mes-hall+cond-mat.str-el', date(2026, 10, 1), TODAY)
        self.assertEqual(papers[0].doi, '10.1234/abc')
        self.assertEqual(papers[0].identifier, 'arxiv:2610.00001')
        self.assertIn('submittedDate:[202610010000 TO 202610082359]', client.calls[0][1]['search_query'])
        self.assertFalse(notices)

    def test_arxiv_partial_page_failure_keeps_results(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>200</o:totalResults><entry><id>http://arxiv.org/abs/2610.00001</id><title>Transport</title><summary>Abstract</summary></entry></feed>'''
        papers, notices = fetch_arxiv_api(FakeClient([xml, TimeoutError()]), 'cond-mat.mes-hall', TODAY, TODAY, 200)
        self.assertEqual(len(papers), 1)
        self.assertTrue(notices)

    def test_crossref_missing_abstract_delayed_index_and_isolation(self):
        data = {'message': {'items': [{'DOI': '10.1234/a', 'title': ['A &amp; B'], 'published': {'date-parts': [[2026, 9, 1]]}, 'container-title': ['PRB']}, {'DOI': '10.1234/old', 'title': ['old'], 'published': {'date-parts': [[2000, 1, 1]]}}]}}
        client = FakeClient([json.dumps(data).encode(), TimeoutError()])
        papers, notices = fetch_crossref(client, date(2026, 10, 1), TODAY, 'Berry curvature;transport')
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].summary, 'A & B')
        self.assertIn('from-index-date:2026-10-01', client.calls[0][1]['filter'])
        self.assertIn('from-pub-date:', client.calls[0][1]['filter'])
        self.assertEqual(client.calls[0][1]['offset'], 0)
        self.assertNotIn('cursor', client.calls[0][1])
        self.assertTrue(notices)

    def test_rss_parses_publisher_dates_and_doi(self):
        xml = b'''<rss><channel><item><title>Transport &amp; magnets</title><link>http://link.aps.org/doi/10.1103/xyz</link><pubDate>Wed, 07 Oct 2026 12:00:00 GMT</pubDate><description>abstract</description></item></channel></rss>'''
        papers, _ = fetch_rss(FakeClient([xml]), 'https://feeds.aps.org/rss/recent/prb.xml', date(2026, 10, 1), TODAY)
        self.assertEqual(papers[0].doi, '10.1103/xyz')
        self.assertEqual(papers[0].url, 'https://doi.org/10.1103/xyz')

    def test_retry_after_and_503_recovery(self):
        headers = Message(); headers['Retry-After'] = '15'
        replies = [HTTPError('https://x', 429, 'busy', headers, None), HTTPError('https://x', 503, 'busy', {}, None), b'ok']
        sleeps = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'ok'
        def opener(*args, **kwargs):
            value = replies.pop(0)
            if isinstance(value, Exception): raise value
            return Response()
        self.assertEqual(HttpClient(opener, sleeps.append).get('https://x'), b'ok')
        self.assertTrue(any(s >= 15 for s in sleeps))

    def test_unauthorized_not_retried(self):
        calls = []
        def opener(*args, **kwargs):
            calls.append(1); raise HTTPError('https://x', 401, 'no', {}, None)
        with self.assertRaises(HTTPError): HttpClient(opener, lambda _: None).get('https://x')
        self.assertEqual(len(calls), 1)

    def test_excessive_retry_after_does_not_violate_cooldown(self):
        headers = Message(); headers['Retry-After'] = '600'
        def opener(*args, **kwargs): raise HTTPError('https://x', 429, 'no', headers, None)
        with self.assertRaises(RuntimeError): HttpClient(opener, lambda _: None).get('https://x')

    def test_exhausted_host_circuit_skips_following_queries(self):
        calls = []
        def opener(*args, **kwargs):
            calls.append(1)
            raise HTTPError('https://x', 503, 'busy', {}, None)
        client = HttpClient(opener, lambda _: None, attempts=2)
        with self.assertRaises(HTTPError): client.get('https://x/a')
        with self.assertRaises(RuntimeError): client.get('https://x/b')
        self.assertEqual(len(calls), 2)

    def test_global_budget_stops_collection(self):
        now = [0]
        client = HttpClient(clock=lambda: now[0], budget_seconds=1)
        now[0] = 2
        with self.assertRaises(TimeoutError): client.get('https://unused.example')

    def test_dedup_preserves_new_aliases(self):
        first = paper(doi='', identifier='arxiv:1234.5')
        journal = paper()
        state = {'sent': {k: '2026-10-07' for k in first.keys}}
        self.assertEqual(new_papers([journal], state), [])
        self.assertIn('doi:10.1234/a', state['sent'])
        self.assertEqual(new_papers([paper(author_names=[])], state), [])
        batch = new_papers([first, journal], {'sent': {}})
        self.assertIn('doi:10.1234/a', batch[0].keys)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = str(Path(self.directory.name) / 'state.json')
        with patch.dict('os.environ', {}, clear=True):
            self.args = parse_args(['--state_file', self.state, '--ranking', 'lexical', '--zotero_id', 'x', '--zotero_key', 'y'])
        self.sent = []
    def deliver(self, *args): self.sent.append(args[-1])
    def invoke(self, candidates, warnings=(), today=TODAY, deliver=None):
        with patch('main.collect', return_value=(candidates, list(warnings))):
            return run(self.args, today=today, corpus_loader=lambda *a: [], deliver=deliver or self.deliver)

    def test_success_then_repeat_run_sends_only_once(self):
        self.invoke([paper()]); self.invoke([paper()])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('doi:10.1234/a', load_state(self.state)['sent'])

    def test_failed_smtp_does_not_advance_ledger(self):
        def fail(*args): raise smtplib.SMTPException('failed')
        with self.assertRaises(smtplib.SMTPException): self.invoke([paper()], deliver=fail)
        self.assertFalse(Path(self.state).exists())
        self.invoke([paper()])
        self.assertEqual(len(self.sent), 1)

    def test_source_outage_sends_status_and_next_day_recovers(self):
        self.invoke([], ['arXiv unavailable'])
        self.assertIn('arXiv unavailable', self.sent[0])
        self.invoke([paper()], today=date(2026, 10, 9))
        self.assertIn(paper().title, self.sent[1])
        self.invoke([paper()], today=date(2026, 10, 10))
        self.assertNotIn(paper().title, self.sent[2])

    def test_selected_only_marked_sent(self):
        self.args.max_paper_num = 1
        self.invoke([paper(), paper(doi='10.1234/b', title='Different theory title')])
        sent = load_state(self.state)['sent']
        self.assertEqual(sum(k.startswith('doi:') for k in sent), 1)

    def test_dry_run_leaves_state_and_smtp_untouched(self):
        self.args.dry_run = True
        self.args.output = str(Path(self.directory.name) / 'preview.html')
        self.invoke([paper()])
        self.assertFalse(self.sent)
        self.assertFalse(Path(self.state).exists())
        self.assertTrue(Path(self.args.output).exists())

    def test_no_papers_sends_daily_mail_by_default(self):
        self.invoke([])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('No new recommendations', self.sent[0])

    def test_failure_status_not_suppressed_by_send_empty_false(self):
        self.args.send_empty = False
        self.invoke([], ['Crossref unavailable'])
        self.assertEqual(len(self.sent), 1)

    def test_corrupt_state_fails_closed(self):
        Path(self.state).write_text('not JSON')
        with self.assertRaises(ValueError): self.invoke([paper()])
        self.assertFalse(self.sent)

    def test_html_escape_and_unsafe_link(self):
        value = render_email([paper(title='<script>alert(1)</script>', url='javascript:alert(1)')])
        self.assertNotIn('<script>', value)
        self.assertNotIn('javascript:', value)

    def test_boolean_false_and_debug_are_safe(self):
        with patch.dict('os.environ', {}, clear=True):
            args = parse_args(['--send_empty', 'false', '--debug'])
        self.assertFalse(args.send_empty)
        self.assertTrue(args.dry_run)

    def test_uncertain_delivery_blocks_next_run(self):
        from construct_email import DeliveryUncertainError
        def uncertain(*args): raise DeliveryUncertainError('unknown')
        with self.assertRaises(DeliveryUncertainError): self.invoke([paper()], deliver=uncertain)
        self.assertIn('uncertain_delivery', load_state(self.state))
        with self.assertRaises(RuntimeError): self.invoke([paper()])
        self.assertFalse(self.sent)

    def test_smtp_quit_failure_after_acceptance_is_not_failed_send(self):
        class SMTP:
            def __init__(self, *args, **kwargs): self.closed = False
            def login(self, *args): pass
            def sendmail(self, *args): return {}
            def quit(self): raise smtplib.SMTPServerDisconnected()
            def close(self): self.closed = True
        send_email('a@example.com', 'b@example.com', 'fake', 'smtp.example.com', 465, '<p>test</p>', smtp_factory=SMTP)


if __name__ == '__main__': unittest.main()
