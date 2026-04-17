"""News fetcher: RSS feeds + NewsAPI for HR/L&D news from India & Middle East."""
import asyncio
import os
import logging
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# ─────────────────────────────────────────────
# Enterprise L&D filter
# Articles matching ANY of these in title/summary are dropped.
# Targets college admissions, school exams, academic news — not enterprise.
# ─────────────────────────────────────────────
EXCLUDE_KEYWORDS = [
    "admission", "entrance exam", "board exam", "neet", "jee", "cat exam",
    "upsc", "ssc exam", "gate exam", "civil services", "10th", "12th",
    "cbse", "icse", "state board", "university exam", "college admission",
    "scholarship", "school students", "school children", "primary school",
    "secondary school", "higher secondary", "curriculum", "syllabus",
    "tuition", "coaching class", "edtech startup funding", "student loan",
    "college fee", "degree course", "postgraduate", "undergraduate",
    "phd", "mba admission", "iit", "iim admission", "nit ", "bits pilani",
    "semester", "results declared", "merit list", "rank list",
]

def _is_enterprise_relevant(title: str, summary: str) -> bool:
    """Return True if the article is about enterprise HR/L&D, not academic/school content."""
    text = (title + " " + summary).lower()
    return not any(kw in text for kw in EXCLUDE_KEYWORDS)

# ─────────────────────────────────────────────
# RSS source registry
# ─────────────────────────────────────────────
RSS_SOURCES = [
    # India HR
    {
        "url": "https://hr.economictimes.indiatimes.com/rss/topstories",
        "name": "ETHRWorld",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://www.peoplemattersglobal.com/rss/latest-stories",
        "name": "People Matters",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://www.peoplemanager.in/feed/",
        "name": "People Manager",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://www.hrkatha.com/feed/",
        "name": "HR Katha",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://humancapitalonline.com/feed/",
        "name": "Human Capital",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://www.businesstoday.in/rss/hr.xml",
        "name": "Business Today HR",
        "region": "India",
        "category": "HR",
    },
    # India Business (ET + Mint) — Jobs/HR only, not general education
    {
        "url": "https://economictimes.indiatimes.com/jobs/rssfeeds/1413652.cms",
        "name": "Economic Times - Jobs & HR",
        "region": "India",
        "category": "HR",
    },
    {
        "url": "https://www.livemint.com/rss/companies",
        "name": "Mint - Companies",
        "region": "India",
        "category": "HR",
    },
    # Middle East HR
    {
        "url": "https://gulfnews.com/rss/business",
        "name": "Gulf News Business",
        "region": "Middle East",
        "category": "HR",
    },
    {
        "url": "https://www.arabianbusiness.com/rss",
        "name": "Arabian Business",
        "region": "Middle East",
        "category": "HR",
    },
    {
        "url": "https://www.khaleejtimes.com/feed/",
        "name": "Khaleej Times",
        "region": "Middle East",
        "category": "HR",
    },
    # L&D / Global with India/ME coverage
    {
        "url": "https://www.hrdive.com/feeds/news/",
        "name": "HR Dive",
        "region": "Global",
        "category": "HR",
    },
    {
        "url": "https://www.hrdive.com/feeds/talent/",
        "name": "HR Dive - Talent",
        "region": "Global",
        "category": "HR",
    },
    {
        "url": "https://www.shrm.org/rss/pages/rss.aspx",
        "name": "SHRM",
        "region": "Global",
        "category": "HR",
    },
    {
        "url": "https://www.shrm.org/resourcesandtools/rss/pages/rss.aspx",
        "name": "SHRM Resources",
        "region": "Global",
        "category": "HR",
    },
    {
        "url": "https://www.td.org/rss",
        "name": "ATD Learning",
        "region": "Global",
        "category": "L&D",
    },
    {
        "url": "https://elearningindustry.com/feed",
        "name": "eLearning Industry",
        "region": "Global",
        "category": "L&D",
    },
]

# NewsAPI queries for India & Middle East HR
NEWSAPI_QUERIES = [
    {
        "q": '("HR" OR "human resources" OR "CHRO" OR "Chief People Officer" OR "people strategy") AND ("India" OR "Infosys" OR "TCS" OR "Wipro" OR "HCL" OR "Reliance")',
        "region": "India",
        "category": "HR",
    },
    {
        "q": '("corporate learning" OR "employee training" OR "workforce upskilling" OR "enterprise L&D" OR "talent development" OR "learning management") AND ("India" OR "enterprise" OR "corporate")',
        "region": "India",
        "category": "L&D",
    },
    {
        "q": '("HR" OR "human resources" OR "CHRO" OR "talent management" OR "people strategy") AND ("UAE" OR "Dubai" OR "Saudi Arabia" OR "Qatar" OR "Middle East" OR "Gulf")',
        "region": "Middle East",
        "category": "HR",
    },
    {
        "q": '("corporate training" OR "workforce development" OR "employee upskilling" OR "enterprise learning" OR "talent development") AND ("UAE" OR "Dubai" OR "Saudi Arabia" OR "Middle East")',
        "region": "Middle East",
        "category": "L&D",
    },
]


def _parse_date(entry) -> str:
    """Extract published date from feedparser entry."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.utcnow().isoformat()


def _extract_body(entry) -> str:
    """Pull full text from feed entry, strip HTML tags."""
    raw = ""
    if hasattr(entry, "content") and entry.content:
        raw = entry.content[0].get("value", "")
    elif hasattr(entry, "summary"):
        raw = entry.summary or ""
    if raw:
        try:
            soup = BeautifulSoup(raw, "lxml")
            return soup.get_text(separator=" ", strip=True)[:5000]
        except Exception:
            return raw[:5000]
    return ""


async def fetch_rss_source(source: dict) -> list[dict]:
    """Fetch and parse a single RSS source."""
    articles = []
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(source["url"])
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
    except Exception as e:
        logger.warning(f"RSS fetch failed for {source['name']}: {e}")
        return []

    for entry in feed.entries[:15]:  # max 15 per source
        link = getattr(entry, "link", None) or getattr(entry, "id", None)
        if not link:
            continue
        title = getattr(entry, "title", "").strip()
        summary = getattr(entry, "summary", "").strip()
        # Strip HTML from summary
        try:
            summary = BeautifulSoup(summary, "lxml").get_text(separator=" ", strip=True)[:500]
        except Exception:
            summary = summary[:500]

        body = _extract_body(entry)
        if not _is_enterprise_relevant(title, summary):
            logger.debug(f"Filtered (academic): {title[:60]}")
            continue

        articles.append(
            {
                "source_name": source["name"],
                "source_url": link,
                "title": title,
                "summary": summary,
                "body": body or summary,
                "published_at": _parse_date(entry),
                "region": source["region"],
                "category": source["category"],
            }
        )
    return articles


async def fetch_newsapi(query_config: dict, page_size: int = 10) -> list[dict]:
    """Fetch articles from NewsAPI.org."""
    if not NEWSAPI_KEY:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query_config["q"],
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": NEWSAPI_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"NewsAPI fetch failed: {e}")
        return []

    articles = []
    for item in data.get("articles", []):
        link = item.get("url", "")
        if not link or "[Removed]" in link:
            continue
        title = item.get("title", "").strip()
        summary = (item.get("description") or "").strip()[:500]

        if not _is_enterprise_relevant(title, summary):
            logger.debug(f"Filtered (academic/NewsAPI): {title[:60]}")
            continue

        articles.append(
            {
                "source_name": item.get("source", {}).get("name", "NewsAPI"),
                "source_url": link,
                "title": title,
                "summary": summary,
                "body": (item.get("content") or item.get("description") or "").strip()[:3000],
                "published_at": item.get("publishedAt", datetime.utcnow().isoformat()),
                "region": query_config["region"],
                "category": query_config["category"],
            }
        )
    return articles


async def fetch_all_news() -> list[dict]:
    """Fetch from all RSS sources and NewsAPI in parallel."""
    tasks = [fetch_rss_source(s) for s in RSS_SOURCES]
    tasks += [fetch_newsapi(q) for q in NEWSAPI_QUERIES]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    articles = []
    for r in results:
        if isinstance(r, list):
            articles.extend(r)
        elif isinstance(r, Exception):
            logger.warning(f"Fetch task error: {r}")

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for a in articles:
        url = a.get("source_url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(a)

    logger.info(f"Fetched {len(unique)} unique articles from {len(tasks)} sources")
    return unique


async def fetch_article_fulltext(url: str) -> Optional[str]:
    """Try to fetch the full article body from its URL."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; HRIntel/1.0)"}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            # Remove nav, footer, scripts, ads
            for tag in soup(["script", "style", "nav", "footer", "aside", "header", "form"]):
                tag.decompose()
            # Try article tag first, then main, then body
            for selector in ["article", "main", ".article-body", ".post-content", "body"]:
                el = soup.select_one(selector)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    if len(text) > 200:
                        return text[:6000]
    except Exception as e:
        logger.debug(f"Full text fetch failed for {url}: {e}")
    return None
