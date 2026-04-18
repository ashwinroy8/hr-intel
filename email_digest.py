"""Daily email digest via Gmail SMTP."""
import logging
import os
import smtplib
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def _send_email(to_addresses: list, subject: str, html: str):
    """Send HTML email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"HR Intel <{SMTP_USER}>"
    msg["To"] = ", ".join(to_addresses)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_addresses, msg.as_string())


def _build_html(articles: list, stats: dict, today: str) -> str:
    """Build the HTML digest email."""

    # Top articles with people
    articles_with_people = [a for a in articles if a.get("people_count", 0) > 0]
    articles_no_people = [a for a in articles if a.get("people_count", 0) == 0]

    # Build article rows
    def article_block(article):
        region_color = {
            "India": "#f97316",
            "Middle East": "#10b981",
        }.get(article.get("region", ""), "#6b7280")

        cat_color = "#8b5cf6" if article.get("category") == "L&D" else "#3b82f6"
        people_count = article.get("people_count", 0)
        people_badge = f'<span style="background:#dcfce7;color:#16a34a;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;">👤 {people_count} contacts</span>' if people_count else ""

        return f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;">
            <div style="margin-bottom:4px;">
              <span style="background:{region_color}20;color:{region_color};padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;margin-right:4px;">{article.get('region','')}</span>
              <span style="background:{cat_color}20;color:{cat_color};padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;margin-right:4px;">{article.get('category','')}</span>
              {people_badge}
            </div>
            <a href="{article.get('source_url','#')}" style="color:#1e40af;font-size:14px;font-weight:600;text-decoration:none;line-height:1.4;">{article.get('title','')}</a>
            <div style="color:#6b7280;font-size:12px;margin-top:3px;">{article.get('source_name','')} · {str(article.get('fetched_at',''))[:10]}</div>
          </td>
        </tr>"""

    top_articles_html = "".join(article_block(a) for a in articles_with_people[:10])
    if not top_articles_html:
        top_articles_html = "".join(article_block(a) for a in articles_no_people[:10])

    app_url = os.getenv("APP_URL", "https://hr-intel-production.up.railway.app")

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:620px;margin:32px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);padding:32px 32px 24px;">
      <div style="color:#c7d2fe;font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;">HR Intel Daily Digest</div>
      <h1 style="color:#ffffff;margin:0;font-size:22px;font-weight:700;">{today}</h1>
      <p style="color:#c7d2fe;margin:8px 0 0;font-size:13px;">Your daily HR &amp; L&D intelligence briefing</p>
    </div>

    <!-- Stats bar -->
    <div style="background:#f5f3ff;padding:16px 32px;display:flex;gap:24px;border-bottom:1px solid #ede9fe;">
      <div style="text-align:center;">
        <div style="font-size:22px;font-weight:700;color:#4f46e5;">{stats.get('total_articles', 0)}</div>
        <div style="font-size:11px;color:#6b7280;">Total Articles</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:22px;font-weight:700;color:#4f46e5;">{len(articles)}</div>
        <div style="font-size:11px;color:#6b7280;">New Today</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:22px;font-weight:700;color:#4f46e5;">{len(articles_with_people)}</div>
        <div style="font-size:11px;color:#6b7280;">With Contacts</div>
      </div>
      <div style="text-align:center;">
        <div style="font-size:22px;font-weight:700;color:#4f46e5;">{stats.get('total_people', 0)}</div>
        <div style="font-size:11px;color:#6b7280;">People Found</div>
      </div>
    </div>

    <!-- Articles -->
    <div style="padding:24px 32px;">
      <h2 style="font-size:15px;font-weight:700;color:#111827;margin:0 0 16px;">
        📰 Today's Top Articles
      </h2>
      <table width="100%" cellpadding="0" cellspacing="0">
        {top_articles_html}
      </table>
    </div>

    <!-- CTA -->
    <div style="padding:0 32px 32px;text-align:center;">
      <a href="{app_url}/dashboard"
         style="display:inline-block;background:#4f46e5;color:#ffffff;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;text-decoration:none;">
        Open HR Intel →
      </a>
      <p style="color:#9ca3af;font-size:11px;margin:16px 0 0;">
        You're receiving this because you're subscribed to HR Intel daily digest.
      </p>
    </div>

  </div>
</body>
</html>"""


async def send_daily_digest(db):
    """Fetch data and send digest to configured recipients."""
    import database

    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP_USER or SMTP_PASS not configured — skipping digest")
        return

    settings = await database.get_settings(db)
    recipients_raw = settings.get("digest_emails", "")
    if not recipients_raw:
        logger.info("No digest recipients configured — skipping")
        return

    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()]
    if not recipients:
        return

    today = date.today().isoformat()
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()

    # Get today's articles
    articles = await database.get_articles(db, limit=50)
    stats = await database.get_stats(db)

    subject = f"HR Intel Digest — {today} · {len(articles)} articles"
    html = _build_html(articles, stats, today)

    try:
        import asyncio
        await asyncio.to_thread(_send_email, recipients, subject, html)
        logger.info(f"Daily digest sent to {recipients}")
    except Exception as e:
        logger.error(f"Failed to send digest: {e}")
