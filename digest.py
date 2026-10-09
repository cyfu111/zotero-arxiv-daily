"""Bounded, fault-tolerant paper discovery and delivery state (standard library only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import logging
from pathlib import Path
import random
import re
import time
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

LOG = logging.getLogger(__name__)
ATOM = '{http://www.w3.org/2005/Atom}'
ARXIV = '{http://arxiv.org/schemas/atom}'


def plain(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def normalize_doi(value):
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', str(value or '').strip(), flags=re.I).lower()


@dataclass
class Paper:
    title: str
    summary: str
    url: str
    source: str
    published: str = ''
    doi: str = ''
    identifier: str = ''
    author_names: list[str] = field(default_factory=list)
    pdf_url: str = ''
    score: float = 0.0
    aliases: list[str] = field(default_factory=list)

    @property
    def authors(self):
        return [SimpleNamespace(name=name) for name in self.author_names]

    @property
    def keys(self):
        keys = []
        if self.doi:
            keys.append('doi:' + normalize_doi(self.doi))
        if self.identifier:
            keys.append(self.identifier)
        # Exact normalized long title + first author only. Avoid merging short/generic titles.
        title = re.sub(r'[^\w]', '', self.title.casefold())
        author = re.sub(r'[^\w]', '', self.author_names[0].casefold()) if self.author_names else ''
        if len(title) >= 40 and author:
            keys.append('title:' + hashlib.sha256((title + '|' + author).encode()).hexdigest())
        if not keys:
            keys.append('url:' + self.url)
        return list(dict.fromkeys(keys + self.aliases))


class HttpClient:
    """Serial polite requests; bounded retries honour Retry-After, never retry 4xx blindly."""
    def __init__(self, opener=urlopen, sleep=time.sleep, clock=time.monotonic, attempts=4, budget_seconds=600):
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.attempts = attempts
        self.last_request = {}
        self.blocked_hosts = set()
        self.deadline = clock() + budget_seconds

    def get(self, url, params=None, interval=1.1):
        if params:
            url += '?' + urlencode(params)
        host = urlsplit(url).netloc
        pace_key = 'arxiv.org' if host in ('export.arxiv.org', 'rss.arxiv.org') else host
        if host in self.blocked_hosts:
            raise RuntimeError('Source host circuit open after exhausted retries')
        for attempt in range(self.attempts):
            if self.clock() >= self.deadline:
                raise TimeoutError('Collection time budget exhausted')
            remaining = interval - (self.clock() - self.last_request.get(pace_key, -1e10))
            if remaining > 0:
                if self.clock() + remaining >= self.deadline:
                    raise TimeoutError('Collection time budget exhausted')
                self.sleep(remaining)
            self.last_request[pace_key] = self.clock()
            try:
                request = Request(url, headers={'User-Agent': 'zotero-journal-daily/0.4 (https://github.com/cyfu111/zotero-arxiv-daily)', 'Accept': 'application/json, application/atom+xml, application/xml'})
                with self.opener(request, timeout=min(30, max(1, self.deadline - self.clock()))) as response:
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                path = '/atom/[categories]' if host == 'rss.arxiv.org' else urlsplit(url).path
                LOG.warning('HTTP request failed: %s %s (%s), attempt %d/%d', host, path, failure_label(exc), attempt + 1, self.attempts)
                if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                    raise
                if attempt + 1 >= self.attempts:
                    self.blocked_hosts.add(host)
                    raise
                delay = min(60, 5 * 2 ** attempt) + random.uniform(0, 1)
                retry_after = exc.headers.get('Retry-After') if isinstance(exc, HTTPError) and exc.headers else None
                if retry_after:
                    try:
                        try:
                            server_delay = float(retry_after)
                        except ValueError:
                            server_delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                        if server_delay > 120:
                            self.blocked_hosts.add(host)
                            raise RuntimeError('Server requested a long cooldown; retry on the next scheduled run') from exc
                        delay = max(delay, server_delay)
                    except (ValueError, TypeError):
                        pass
                LOG.warning('Temporary HTTP failure (%s); retry %d/%d after %.1fs', type(exc).__name__, attempt + 1, self.attempts, delay)
                if self.clock() + delay >= self.deadline:
                    raise TimeoutError('Collection time budget exhausted')
                self.sleep(delay)


def arxiv_categories(categories):
    categories = [x.strip() for x in re.split(r'[+,\s]+', categories or '') if x.strip()]
    if not categories or any(not re.fullmatch(r'[A-Za-z0-9.-]+', x) for x in categories):
        raise ValueError('ARXIV_QUERY must contain arXiv category names separated by + or commas')
    return list(dict.fromkeys(categories))


def failure_label(exc):
    # Do not log whole request URLs, query values, response bodies or credentials.
    if isinstance(exc, HTTPError):
        return f'HTTP {exc.code}'
    return type(exc).__name__


def arxiv_feed_root(body):
    root = ET.fromstring(body)
    if root.tag != ATOM + 'feed':
        raise ValueError('arXiv response is not an Atom feed')
    for entry in root.findall(ATOM + 'entry'):
        if '/api/errors' in entry.findtext(ATOM + 'id', '') or entry.findtext(ATOM + 'title', '').lower() == 'error':
            raise ValueError('arXiv returned an API error')
    return root


def arxiv_paper(entry, *, daily=False):
    raw_id = entry.findtext(ATOM + 'id', '')
    identifier = re.sub(r'v\d+$', '', raw_id.removeprefix('oai:arXiv.org:').rsplit('/abs/', 1)[-1])
    if not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})', identifier):
        raise ValueError('arXiv entry has an invalid identifier')
    title = plain(entry.findtext(ATOM + 'title'))
    summary = plain(entry.findtext(ATOM + 'summary'))
    if not title or not summary:
        raise ValueError('arXiv entry is missing title or abstract')
    authors = [plain(a.findtext(ATOM + 'name')) for a in entry.findall(ATOM + 'author')]
    if daily:
        summary = re.sub(r'^arXiv:\S+\s+Announce Type:\s*[^\n]+\n\s*Abstract:\s*', '', summary)
        creators = entry.findtext('{http://purl.org/dc/elements/1.1/}creator', '')
        authors = [x.strip() for x in creators.split(',') if x.strip()] or authors
    return Paper(title, summary, 'https://arxiv.org/abs/' + identifier,
                 'arXiv (daily feed)' if daily else 'arXiv',
                 entry.findtext(ATOM + 'published', '')[:10],
                 normalize_doi(entry.findtext(ARXIV + 'doi') or entry.findtext(ARXIV + 'DOI')),
                 'arxiv:' + identifier, authors, 'https://arxiv.org/pdf/' + identifier)


def fetch_arxiv_daily(client, categories, since, until, max_results=1000):
    """Official daily announcements include full abstracts; never re-query IDs via API.

    Daily published timestamps are announcement dates, not original submission dates.
    This feed cannot backfill a historical window, even when its HTTP request succeeds.
    """
    categories = arxiv_categories(categories)
    url = 'https://rss.arxiv.org/atom/' + '+'.join(categories)
    root = arxiv_feed_root(client.get(url, interval=3.1))
    entries = root.findall(ATOM + 'entry')
    papers, seen, notices = [], set(), []
    for entry in entries:
        # Replacements may concern years-old papers. Cross-list announcements are new
        # to this category and are deduplicated against already sent arXiv identities.
        kind = entry.findtext(ARXIV + 'announce_type', '').lower()
        if kind not in ('new', 'cross'):
            continue
        stamp = entry.findtext(ATOM + 'published', '')
        try:
            announced = datetime.fromisoformat(stamp.replace('Z', '+00:00')).date()
        except ValueError:
            raise ValueError('arXiv daily entry has no valid announcement date')
        if not since <= announced <= until:
            continue
        paper = arxiv_paper(entry, daily=True)
        if paper.identifier not in seen:
            papers.append(paper)
            seen.add(paper.identifier)
    papers.sort(key=lambda p: (p.published, p.identifier), reverse=True)
    if len(entries) >= 2000 or len(papers) > max_results:
        notices.append('arXiv daily feed result limit reached; category coverage may be incomplete')
    LOG.info('arXiv daily feed: %d eligible records (announcement dates; not historical backfill)', len(papers[:max_results]))
    return papers[:max_results], notices


def fetch_arxiv_api(client, categories, since, until, max_results=1000):
    categories = arxiv_categories(categories)
    query = '(' + ' OR '.join('cat:' + x for x in categories) + ')'
    query += f' AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]'
    papers, warnings, seen = [], [], set()
    start = 0
    while start < max_results:
        try:
            size = min(100, max_results - start)
            LOG.info('arXiv API page: offset=%d size=%d window=%s..%s', start, size, since, until)
            body = client.get('https://export.arxiv.org/api/query', {'search_query': query, 'start': start, 'max_results': size, 'sortBy': 'submittedDate', 'sortOrder': 'descending'}, interval=3.1)
            root = arxiv_feed_root(body)
            entries = root.findall(ATOM + 'entry')
            for entry in entries:
                paper = arxiv_paper(entry)
                if paper.identifier not in seen:
                    papers.append(paper)
                    seen.add(paper.identifier)
            total = int(root.findtext('{http://a9.com/-/spec/opensearch/1.1/}totalResults', str(start + len(entries))))
            # Increment by returned records, not the requested size: short pages must
            # not silently skip offsets. Every response consumes a bounded page budget.
            start += len(entries)
            if start >= total:
                break
            if not entries:
                warnings.append('arXiv API returned an empty page before totalResults; recovery window incomplete')
                break
            if start >= max_results:
                warnings.append('arXiv result limit reached; narrow categories or increase SOURCE_MAX_RESULTS')
        except Exception as exc:
            if not papers:
                raise
            warnings.append('arXiv API pagination interrupted (' + failure_label(exc) + '); recovery window incomplete')
            break
    LOG.info('arXiv API: %d records fetched', len(papers))
    return papers, warnings


def fetch_arxiv(client, categories, since, until, max_results=1000):
    """Daily announcements plus date-window recovery, using two official interfaces.

    Fetch announcements first, independently of API availability. This is not endpoint
    rotation after a denial: each service is queried once with its own bounded retry
    policy, and the API's cooldown/circuit breaker is never cleared.
    """
    arxiv_categories(categories)
    if max_results < 1 or since > until:
        raise ValueError('Invalid arXiv result limit or date window')
    daily, recovered, warnings = [], [], []
    daily_error = api_error = None
    try:
        daily, notices = fetch_arxiv_daily(client, categories, since, until, max_results)
        warnings.extend(notices)
    except Exception as exc:
        daily_error = exc
        LOG.warning('arXiv daily feed unavailable (%s)', failure_label(exc))
    try:
        recovered, notices = fetch_arxiv_api(client, categories, since, until, max_results)
        warnings.extend(notices)
    except Exception as exc:
        api_error = exc
        LOG.warning('arXiv API unavailable (%s)', failure_label(exc))
    if api_error:
        if daily_error:
            raise RuntimeError(f'arXiv daily feed failed ({failure_label(daily_error)}); API failed ({failure_label(api_error)})') from api_error
        warnings.append(f'arXiv API unavailable ({failure_label(api_error)}); daily feed supplied {len(daily)} records only. The {since}..{until} recovery window is incomplete; a daily feed cannot backfill missed days.')
    elif daily_error:
        warnings.append(f'arXiv daily feed unavailable ({failure_label(daily_error)}); using API submission-date results')
    # Prefer submission-date API metadata for overlapping records. Identity keys match
    # both interfaces, so switching interfaces cannot resend the same preprint.
    merged = {p.identifier: p for p in daily}
    merged.update({p.identifier: p for p in recovered})
    papers = sorted(merged.values(), key=lambda p: (p.published, p.identifier), reverse=True)
    if len(papers) > max_results:
        warnings.append('arXiv combined result limit reached; coverage is incomplete and capped candidates may age out')
    return papers[:max_results], warnings


def crossref_date(item):
    for key in ('published-online', 'published-print', 'published', 'issued'):
        parts = item.get(key, {}).get('date-parts', [[]])[0]
        if len(parts) >= 3:
            try:
                return date(*parts[:3])
            except ValueError:
                continue
    return None


def fetch_crossref(client, since, until, queries, issns=(), max_results=300, max_age_days=90):
    """Discover recent journal works by index time, including delayed publisher deposits."""
    targets = [('query', q.strip()) for q in queries.split(';') if q.strip()]
    targets += [('issn', i.strip()) for i in issns if re.fullmatch(r'\d{4}-\d{3}[\dXx]', i.strip())]
    if not targets:
        raise ValueError('Set JOURNAL_QUERIES or JOURNAL_ISSNS (or add ISSNs to your Zotero library)')
    all_papers, warnings = [], []
    for kind, target in targets:
        try:
            count = 0
            while count < max_results:
                params = {'filter': f'type:journal-article,from-index-date:{since},until-index-date:{until},from-pub-date:{until - timedelta(days=max_age_days)},until-pub-date:{until}', 'rows': min(100, max_results-count), 'offset': count}
                url = 'https://api.crossref.org/works'
                if kind == 'query':
                    params['query.bibliographic'] = target
                else:
                    url = 'https://api.crossref.org/journals/' + quote(target, safe='') + '/works'
                message = json.loads(client.get(url, params))['message']
                items = message['items']
                for item in items:
                    published = crossref_date(item)
                    if not published or not until - timedelta(days=max_age_days) <= published <= until:
                        continue
                    title = plain((item.get('title') or [''])[0])
                    doi = normalize_doi(item.get('DOI'))
                    if not title or not doi:
                        continue
                    journal = plain((item.get('container-title') or ['Journal'])[0])
                    authors = [' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name', '') for a in item.get('author', [])]
                    all_papers.append(Paper(title, plain(item.get('abstract')) or title, 'https://doi.org/' + doi, journal + ' (Crossref)', published.isoformat(), doi, author_names=authors))
                count += len(items)
                if not items or count >= message.get('total-results', count):
                    break
                if count >= max_results:
                    warnings.append(f'Crossref {target}: result limit reached; narrow search or increase SOURCE_MAX_RESULTS')
                    break
        except Exception as exc:
            warnings.append(f'Crossref {target}: unavailable ({type(exc).__name__}); other sources continue')
    return all_papers, warnings


def load_state(path):
    path = Path(path)
    if not path.exists():
        return {'version': 1, 'sent': {}, 'last_delivery_date': None}
    state = json.loads(path.read_text())
    if state.get('version') != 1 or not isinstance(state.get('sent'), dict):
        raise ValueError('Invalid delivery state; refusing to risk duplicate email')
    return state


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, sort_keys=True, indent=2))
    temporary.replace(path)


def new_papers(papers, state):
    owners, unique = {}, []
    for paper in papers:
        prior = [state['sent'][key] for key in paper.keys if key in state['sent']]
        if prior:
            # Learn DOI/title aliases of an already delivered preprint or journal record.
            state['sent'].update({key: max(prior) for key in paper.keys})
            continue
        match = next((owners[key] for key in paper.keys if key in owners), None)
        if match is not None:
            match.aliases.extend(paper.keys)
            owners.update({key: match for key in match.keys})
            continue
        unique.append(paper)
        owners.update({key: paper for key in paper.keys})
    return unique


def record_delivery(state, papers, today):
    # Retain aliases longer than both discovery and publication windows.
    cutoff = (today - timedelta(days=180)).isoformat()
    state['sent'] = {k: v for k, v in state['sent'].items() if v >= cutoff}
    for paper in papers:
        state['sent'].update({key: today.isoformat() for key in paper.keys})
    state['last_delivery_date'] = today.isoformat()


def fallback_rank(papers, corpus):
    """Local deterministic fallback, useful when the embedding model cannot load."""
    tokens = set(re.findall(r'\w{3,}', ' '.join(c.get('data', {}).get('title', '') + ' ' + c.get('data', {}).get('abstractNote', '') for c in corpus).casefold()))
    for p in papers:
        words = set(re.findall(r'\w{3,}', (p.title + ' ' + p.summary).casefold()))
        p.score = 10 * len(words & tokens) / max(1, len(words))
    return sorted(papers, key=lambda p: p.score, reverse=True)


def fetch_rss(client, url, since, until):
    """Publisher Atom/RSS feeds complement Crossref's delayed or absent deposits."""
    root = ET.fromstring(client.get(url))
    entries = root.findall(ATOM + 'entry') or root.findall('.//item') or root.findall('{http://purl.org/rss/1.0/}item')
    if not entries and root.tag not in ('rss', ATOM + 'feed', '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF'):
        raise ValueError('Response is not a recognised RSS/Atom feed')
    papers = []
    def field(entry, name):
        for child in entry:
            if child.tag.rsplit('}', 1)[-1] == name:
                return ''.join(child.itertext()).strip()
        return ''
    for entry in entries:
        title = plain(field(entry, 'title'))
        link = field(entry, 'link')
        if not link:
            link = next((c.attrib.get('href', '') for c in entry if c.tag == ATOM + 'link' and c.attrib.get('rel', 'alternate') == 'alternate'), '')
        stamp = field(entry, 'published') or field(entry, 'date') or field(entry, 'pubDate') or field(entry, 'updated')
        try:
            try:
                published = datetime.fromisoformat(stamp.replace('Z', '+00:00')).date()
            except ValueError:
                published = parsedate_to_datetime(stamp).date()
        except (ValueError, TypeError, AttributeError):
            continue
        if not title or not link.startswith(('https://', 'http://')) or not since <= published <= until:
            continue
        doi = field(entry, 'doi') or field(entry, 'identifier')
        if not normalize_doi(doi).startswith('10.'):
            match = re.search(r'10\.\d{4,9}/[^\s<>]+', link)
            doi = match.group(0) if match else ''
        if normalize_doi(doi).startswith('10.'):
            link = 'https://doi.org/' + normalize_doi(doi)
        authors = [plain(''.join(c.itertext())) for c in entry if c.tag.rsplit('}', 1)[-1] in ('creator', 'author')]
        papers.append(Paper(title, plain(field(entry, 'description') or field(entry, 'summary') or field(entry, 'content')) or title, link, 'Publisher RSS', published.isoformat(), normalize_doi(doi), author_names=authors))
    return papers, []
