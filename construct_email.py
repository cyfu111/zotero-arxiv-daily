"""Render metadata safely; email never depends on PDF downloads or an LLM."""
import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid, getaddresses
import html
import math
import re
import smtplib
import ssl
from urllib.parse import urlsplit


class DeliveryUncertainError(RuntimeError):
    """SMTP connection failed during DATA; acceptance cannot be established."""


def safe_url(value):
    return html.escape(value, quote=True) if urlsplit(value).scheme in ('http', 'https') else '#'


def get_stars(score):
    """Original semantic scale: <=6 is zero, >=8 is five, in half-star steps."""
    if score is None or not math.isfinite(score):
        return ''
    halves = 0 if score <= 6 else 10 if score >= 8 else math.ceil((score - 6) / 0.2)
    full = '<span style="vertical-align:middle">⭐</span>'
    half = '<span style="display:inline-block;width:0.5em;overflow:hidden;white-space:nowrap;vertical-align:middle">⭐</span>'
    return full * (halves // 2) + half * (halves % 2)


def render_email(papers, warnings=(), summaries=None):
    esc = html.escape
    parts = []
    if not papers:
        message = ('No recommendations could be confirmed because collection was incomplete.'
                   if warnings else 'No new recommendations in this run.')
        parts.append('<p>' + message + '</p>')
    for p in papers:
        summary = (summaries or {}).get(p.url, p.summary)
        authors = ', '.join(p.author_names[:8]) + (' et al.' if len(p.author_names) > 8 else '')
        parts.append(f'<article><h3><a href="{safe_url(p.url)}">{esc(p.title)}</a></h3><p>{esc(p.source)} · {esc(p.published)}<br>{esc(authors)}</p>')
        if p.semantic_score is not None and math.isfinite(p.semantic_score):
            label = p.relevance_basis or 'Semantic relevance'
            parts.append(f'<p><strong>{esc(label)}:</strong> {get_stars(p.semantic_score)} <span>({p.semantic_score:.2f}/10)</span></p>')
        elif p.relevance_basis == 'lexical':
            parts.append('<p>Keyword match ranking · semantic relevance unavailable</p>')
        parts.append(f'<p>{esc(summary)}</p>')
        if p.pdf_url:
            parts.append(f'<p><a href="{safe_url(p.pdf_url)}">PDF</a></p>')
        parts.append('</article><hr>')
    return '<!doctype html><html><body>' + '\n'.join(parts) + '</body></html>'


def parse_recipients(primary, additional=None):
    """Preserve the existing destination; validate and deduplicate extra addresses."""
    if not primary:
        raise ValueError('A primary receiver is required')
    values = list(primary) if isinstance(primary, (list, tuple)) else [primary]
    if additional:
        values.extend(additional if isinstance(additional, (list, tuple)) else [additional])
    recipients = []
    for value in values:
        if not isinstance(value, str) or '\n' in value or '\r' in value:
            raise ValueError('Invalid receiver address')
        for _, address in getaddresses([value.replace(';', ',')]):
            if not re.fullmatch(r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+", address):
                raise ValueError('Invalid receiver address')
            if address.casefold() not in {r.casefold() for r in recipients}:
                recipients.append(address)
    if not recipients:
        raise ValueError('At least one receiver is required')
    return recipients


def send_email(sender, receiver, password, smtp_server, smtp_port, html, smtp_factory=None):
    """No retry after DATA: an ambiguous disconnect may already have delivered mail."""
    if not all((sender, receiver, password, smtp_server, smtp_port)):
        raise ValueError('SMTP sender, receiver, password, server and port are required')
    recipients = parse_recipients(receiver)
    msg = MIMEText(html, 'html', 'utf-8')
    msg['From'] = formataddr(('Daily research', sender))
    msg['To'] = ', '.join(recipients)
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y/%m/%d')
    msg['Subject'] = Header(f'Daily research {today}', 'utf-8').encode()
    msg['Message-ID'] = make_msgid(domain=sender.split('@')[-1])
    context = ssl.create_default_context()
    if smtp_port == 465:
        server = (smtp_factory or smtplib.SMTP_SSL)(smtp_server, smtp_port, timeout=30, context=context)
    else:
        server = (smtp_factory or smtplib.SMTP)(smtp_server, smtp_port, timeout=30)
    try:
        if smtp_port != 465:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        server.login(sender, password)
        try:
            refused = server.sendmail(sender, recipients, msg.as_string())
        except (smtplib.SMTPServerDisconnected, OSError) as exc:
            raise DeliveryUncertainError('SMTP disconnected during DATA; check recipient before retrying') from exc
        if refused:
            # Some recipients may have accepted DATA; retry would duplicate their mail.
            if len(refused) < len(recipients):
                raise DeliveryUncertainError("SMTP accepted only some recipients; check delivery before retrying")
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT can fail after successful DATA; that must not turn acceptance into failure.
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            try:
                server.close()
            except (smtplib.SMTPException, OSError):
                pass
