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
from urllib.parse import urlencode, quote
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
        from urllib.parse import urlsplit
        host = urlsplit(url).netloc
        if host in self.blocked_hosts:
            raise RuntimeError('Source host circuit open after exhausted retries')
        for attempt in range(self.attempts):
            if self.clock() >= self.deadline:
                raise TimeoutError('Collection time budget exhausted')
            remaining = interval - (self.clock() - self.last_request.get(host, -1e10))
            if remaining > 0:
                if self.clock() + remaining >= self.deadline:
                    raise TimeoutError('Collection time budget exhausted')
                self.sleep(remaining)
            self.last_request[host] = self.clock()
            try:
                request = Request(url, headers={'User-Agent': 'zotero-journal-daily/0.4 (https://github.com/cyfu111/zotero-arxiv-daily)', 'Accept': 'application/json, application/atom+xml, application/xml'})
                with self.opener(request, timeout=min(30, max(1, self.deadline - self.clock()))) as response:
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
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


def fetch_arxiv(client, categories, since, until, max_results=1000):
    categories = [x.strip() for x in re.split(r'[+,\s]+', categories or '') if x.strip()]
    if not categories or any(not re.fullmatch(r'[A-Za-z0-9.-]+', x) for x in categories):
        raise ValueError('ARXIV_QUERY must contain arXiv category names separated by + or commas')
    query = '(' + ' OR '.join('cat:' + x for x in categories) + ')'
    query += f' AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]'
    papers = []
    warnings = []
    for start in range(0, max_results, 100):
        try:
            body = client.get('https://export.arxiv.org/api/query', {'search_query': query, 'start': start, 'max_results': min(100, max_results-start), 'sortBy': 'submittedDate', 'sortOrder': 'descending'}, interval=3.1)
            root = ET.fromstring(body)
            entries = root.findall(ATOM + 'entry')
            for entry in entries:
                url = entry.findtext(ATOM + 'id', '')
                if '/api/errors' in url or entry.findtext(ATOM + 'title', '').lower() == 'error':
                    raise ValueError('arXiv returned an API error')
                identifier = re.sub(r'v\d+$', '', url.rsplit('/abs/', 1)[-1])
                papers.append(Paper(plain(entry.findtext(ATOM + 'title')), plain(entry.findtext(ATOM + 'summary')), 'https://arxiv.org/abs/' + identifier, 'arXiv', entry.findtext(ATOM + 'published', '')[:10], normalize_doi(entry.findtext(ARXIV + 'doi')), 'arxiv:' + identifier, [plain(a.findtext(ATOM + 'name')) for a in entry.findall(ATOM + 'author')], 'https://arxiv.org/pdf/' + identifier))
            total = int(root.findtext('{http://a9.com/-/spec/opensearch/1.1/}totalResults', str(len(entries))))
            if start + len(entries) >= total or not entries:
                break
            if start + len(entries) >= max_results:
                warnings.append('arXiv result limit reached; narrow categories or increase SOURCE_MAX_RESULTS')
        except Exception as exc:
            if not papers:
                raise
            warnings.append('arXiv pagination interrupted: ' + type(exc).__name__)
            break
    return papers, warnings


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
