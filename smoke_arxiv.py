"""Real arXiv-only verification. No SMTP, Zotero, LLM, delivery state or mail imports."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import sys

from digest import HttpClient, arxiv_categories, fetch_arxiv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-config', action='store_true', help='Fail if the production ARXIV_QUERY is absent')
    parser.add_argument('--require-api', action='store_true', help='Additionally require API records (daily-feed recovery alone fails)')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    categories = os.environ.get('ARXIV_QUERY', '')
    if args.require_config and not categories.strip():
        parser.error('ARXIV_QUERY must be configured; refusing to substitute unrelated categories')
    categories = categories or 'cond-mat.mes-hall+cond-mat.mtrl-sci+cond-mat.str-el'
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=int(os.environ.get('LOOKBACK_DAYS') or '7'))
    maximum = int(os.environ.get('SOURCE_MAX_RESULTS') or '300')
    print(json.dumps({'check': 'arxiv-live', 'category_count': len(arxiv_categories(categories)),
                      'since': str(since), 'until': str(today), 'max_results': maximum,
                      'production_categories_required': args.require_config}), flush=True)
    papers, warnings = fetch_arxiv(HttpClient(budget_seconds=360), categories, since, today, maximum)
    api_count = sum(p.source == 'arXiv' for p in papers)
    daily_count = sum(p.source == 'arXiv (daily feed)' for p in papers)
    invalid = [p for p in papers if not p.identifier.startswith('arxiv:') or not p.summary or not p.title or not p.url.startswith('https://arxiv.org/abs/')]
    print(json.dumps({'arxiv_records': len(papers), 'api_records': api_count,
                      'daily_feed_only_records': daily_count, 'warnings': warnings}, ensure_ascii=False), flush=True)
    # Empty results may be valid on some dates, but they cannot verify real retrieval.
    if not papers or invalid or (args.require_api and not api_count):
        print('FAIL: required real arXiv records were not retrieved and validated.', file=sys.stderr)
        return 1
    print('PASS: real arXiv metadata fetched; no email sent and no delivery state changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
