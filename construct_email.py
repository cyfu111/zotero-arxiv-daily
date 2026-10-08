"""Render metadata safely; email never depends on PDF downloads or an LLM."""
import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
import html
import smtplib
import ssl
from urllib.parse import urlsplit


class DeliveryUncertainError(RuntimeError):
    """SMTP connection failed during DATA; acceptance cannot be established."""


def safe_url(value):
    return html.escape(value, quote=True) if urlsplit(value).scheme in ('http', 'https') else '#'


def render_email(papers, warnings=(), summaries=None):
    esc = html.escape
    parts = ['<h2>Daily research papers</h2>']
    if warnings:
        parts.append('<h3>Delivery / source status</h3><ul>' + ''.join('<li>' + esc(str(w)) + '</li>' for w in warnings) + '</ul>')
    if not papers:
        parts.append('<p>No new recommendations in this run. See source status above for any collection failures.</p>')
    for p in papers:
        summary = (summaries or {}).get(p.url, p.summary)
        authors = ', '.join(p.author_names[:8]) + (' et al.' if len(p.author_names) > 8 else '')
        parts.append(f'<article><h3><a href="{safe_url(p.url)}">{esc(p.title)}</a></h3><p>{esc(p.source)} · {esc(p.published)}<br>{esc(authors)}</p><p>{esc(summary)}</p>')
        if p.pdf_url:
            parts.append(f'<p><a href="{safe_url(p.pdf_url)}">PDF</a></p>')
        parts.append('</article><hr>')
    parts.append('<p>To change delivery, edit your repository Actions configuration.</p>')
    return '<!doctype html><html><body>' + '\n'.join(parts) + '</body></html>'


def send_email(sender, receiver, password, smtp_server, smtp_port, html, smtp_factory=None):
    """No retry after DATA: an ambiguous disconnect may already have delivered mail."""
    if not all((sender, receiver, password, smtp_server, smtp_port)):
        raise ValueError('SMTP sender, receiver, password, server and port are required')
    msg = MIMEText(html, 'html', 'utf-8')
    msg['From'] = formataddr(('Daily research', sender))
    msg['To'] = receiver
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
            refused = server.sendmail(sender, [receiver], msg.as_string())
        except (smtplib.SMTPServerDisconnected, OSError) as exc:
            raise DeliveryUncertainError('SMTP disconnected during DATA; check recipient before retrying') from exc
        if refused:
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
