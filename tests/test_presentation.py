import math
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from construct_email import DeliveryUncertainError, get_stars, parse_recipients, render_email, send_email
from digest import Paper, fallback_rank
from main import parse_args, run
from datetime import date


def paper(**changes):
    values = dict(title='Altermagnetic spin transport', summary='Spintronics theory', url='https://example.org/paper', source='arXiv', published='2026-10-11')
    values.update(changes)
    return Paper(**values)


class PresentationTests(unittest.TestCase):
    def test_original_star_boundaries_and_half_steps(self):
        for score in (-5, 0, 6): self.assertEqual(get_stars(score), '')
        self.assertEqual(get_stars(8).count('⭐'), 5)
        self.assertEqual(get_stars(100).count('⭐'), 5)
        self.assertEqual(get_stars(7).count('⭐'), 3)
        self.assertIn('width:0.5em', get_stars(7))
        for score in (None, math.nan, math.inf): self.assertEqual(get_stars(score), '')

    def test_stars_ignore_topic_bonus(self):
        p = paper(score=9, semantic_score=7, topic_bonus=2, relevance_basis='Zotero semantic relevance')
        body = render_email([p], ['HTTP 503, model failure'])
        self.assertEqual(body.count('⭐'), 3)
        self.assertIn('(7.00/10)', body)
        for text in ('Daily research papers', 'Delivery / source status', 'HTTP 503', 'model failure', 'Actions configuration'):
            self.assertNotIn(text, body)

    def test_keyword_fallback_clears_stale_semantic_score(self):
        p = fallback_rank([paper(semantic_score=8)], [{'data': {'title': 'spin transport'}}])[0]
        body = render_email([p])
        self.assertIsNone(p.semantic_score)
        self.assertNotIn('⭐', body)
        self.assertIn('Keyword match ranking', body)

    def test_empty_failure_and_empty_success_are_distinct(self):
        self.assertIn('collection was incomplete', render_email([], ['API timeout']))
        self.assertNotIn('API timeout', render_email([], ['API timeout']))
        self.assertIn('No new recommendations', render_email([]))

    def test_parse_extra_receivers_preserves_primary_and_deduplicates(self):
        self.assertEqual(parse_recipients('First <primary@example.com>', 'extra@example.com; PRIMARY@example.com'), ['primary@example.com', 'extra@example.com'])
        for bad in ('bad', 'x@example.com\r\nBcc: x@example.com', 'x@@example.com'):
            with self.assertRaises(ValueError): parse_recipients('primary@example.com', bad)
        with self.assertRaises(ValueError): parse_recipients(None, 'extra@example.com')

    def test_environment_extra_receiver_reaches_delivery_without_email_test(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'RECEIVER': 'primary@example.com', 'ADDITIONAL_RECEIVERS': 'extra@example.com'}, clear=True):
            args = parse_args(['--state_file', tmp + '/state.json', '--ranking', 'lexical'])
            sent = []
            with patch('main.collect', return_value=([paper()], [])):
                run(args, today=date(2026, 10, 11), deliver=lambda *a: sent.append(a))
            self.assertEqual(sent[0][1], ['primary@example.com', 'extra@example.com'])

    def test_smtp_uses_every_recipient_and_handles_partial_acceptance(self):
        calls = []
        class SMTP:
            def __init__(self, *a, **kw): pass
            def login(self, *a): pass
            def quit(self): pass
            def sendmail(self, sender, recipients, message):
                calls.append((recipients, message))
                return {'extra@example.com': (550, b'refused')}
        with self.assertRaises(DeliveryUncertainError):
            send_email('sender@example.com', ['primary@example.com', 'extra@example.com'], 'fake', 'smtp.example.com', 465, '<p>Offline fixture</p>', smtp_factory=SMTP)
        self.assertEqual(calls[0][0], ['primary@example.com', 'extra@example.com'])
        self.assertIn('To: primary@example.com, extra@example.com', calls[0][1])

    def test_source_warning_survives_deduplicated_empty_output(self):
        import json
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            p = paper()
            state = Path(tmp) / 'state.json'
            state.write_text(json.dumps({'version': 1, 'sent': {k: '2026-10-10' for k in p.keys}}))
            args = parse_args(['--dry-run', '--state_file', str(state), '--output', tmp + '/preview.html'])
            with patch('main.collect', return_value=([p], ['One source unavailable'])):
                run(args, today=date(2026, 10, 11))
            self.assertIn('collection was incomplete', Path(args.output).read_text())

    def test_embedding_failure_keeps_papers_and_logs_notice(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            args = parse_args(['--dry-run', '--state_file', tmp + '/state.json', '--output', tmp + '/preview.html'])
            with patch('main.collect', return_value=([paper()], [])), patch.dict('sys.modules', {'recommender': None}), self.assertLogs('main', level='WARNING') as logs:
                run(args, today=date(2026, 10, 11))
            html = Path(args.output).read_text()
            self.assertIn('Altermagnetic spin transport', html)
            self.assertIn('Keyword match ranking', html)
            self.assertNotIn('⭐', html)
            self.assertTrue(any('Embedding ranking unavailable' in line for line in logs.output))

if __name__ == '__main__': unittest.main()
