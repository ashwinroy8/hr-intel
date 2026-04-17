"""Claude-powered people extraction — uses requests directly (no SDK) for Railway compatibility."""
import asyncio
import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


def _headers():
    return {**ANTHROPIC_HEADERS, "x-api-key": ANTHROPIC_API_KEY}


def _call_claude(payload: dict, timeout: int = 40) -> dict:
    """Synchronous Claude API call via requests. Runs in a thread via asyncio.to_thread."""
    resp = requests.post(ANTHROPIC_URL, headers=_headers(), json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────
# People extraction
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a precise data extraction engine for HR and Learning & Development industry news.

Your task: Extract every REAL person explicitly named in the article.

Rules:
- Only extract people actually named (full name or at least first + last name)
- Extract their job title/designation EXACTLY as stated in the article
- Extract company name as stated in the article
- Extract phone number ONLY if explicitly stated in the article
- Extract email ONLY if explicitly stated in the article
- Extract LinkedIn URL ONLY if explicitly stated in the article
- Write a brief 1-sentence context explaining why this person is mentioned
- Do NOT infer, guess, or hallucinate any contact details
- Do NOT include fictional, historical, or quoted-as-example persons

Return ONLY a valid JSON array. No markdown fences, no explanation.
If no people are found, return: []"""


async def extract_people_from_article(title: str, body: str, article_url: str = "") -> list:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping extraction")
        return []
    if not body or len(body.strip()) < 20:
        logger.debug(f"Article too short: {title[:60]}")
        return []

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "tools": [
            {
                "name": "save_people",
                "description": "Save extracted people from the article.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "people": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name":         {"type": "string"},
                                    "designation":  {"type": ["string", "null"]},
                                    "company":      {"type": ["string", "null"]},
                                    "phone":        {"type": ["string", "null"]},
                                    "email":        {"type": ["string", "null"]},
                                    "linkedin_url": {"type": ["string", "null"]},
                                    "context":      {"type": ["string", "null"]},
                                },
                                "required": ["name"],
                            },
                        }
                    },
                    "required": ["people"],
                },
            }
        ],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": f"Article Title: {title}\n\nArticle Text:\n{body[:5000]}\n\nExtract all named people from this article."}
        ],
    }

    try:
        data = await asyncio.to_thread(_call_claude, payload)
    except requests.exceptions.RequestException as e:
        logger.error(f"Claude request failed for '{title[:60]}': {e}")
        return []
    except Exception as e:
        logger.error(f"Claude error for '{title[:60]}': {e}")
        return []

    logger.info(f"Claude response stop_reason={data.get('stop_reason')} content_types={[b.get('type') for b in data.get('content', [])]}")

    # Parse tool_use response
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "save_people":
            people = block.get("input", {}).get("people", [])
            logger.info(f"Extracted {len(people)} people from: {title[:60]}")
            return _clean_people(people)

    logger.warning(f"No tool_use block in Claude response for: {title[:60]}")
    return []


def _clean_people(raw: list) -> list:
    seen, cleaned = set(), []
    for p in raw:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name or len(name) < 3 or name.lower() in seen:
            continue
        seen.add(name.lower())
        linkedin_url = p.get("linkedin_url")
        if not linkedin_url:
            search_q = f"{name} {p.get('company') or ''}".strip().replace(" ", "%20")
            linkedin_url = f"https://www.linkedin.com/search/results/people/?keywords={search_q}"
        cleaned.append({
            "name": name,
            "designation": (p.get("designation") or "").strip() or None,
            "company":     (p.get("company") or "").strip() or None,
            "phone":       (p.get("phone") or "").strip() or None,
            "email":       (p.get("email") or "").strip() or None,
            "linkedin_url": linkedin_url,
            "context":     (p.get("context") or "").strip() or None,
        })
    return cleaned


# ─────────────────────────────────────────────
# Outreach email generation
# ─────────────────────────────────────────────

OUR_COMPANY_CONTEXT = """
Company: A skilling and employee engagement platform
What we do: We help organisations upskill their workforce, drive learning culture, and boost employee engagement through technology-led solutions including learning management, assessments, gamification, and engagement tools.
Key value propositions:
- Personalised learning paths at scale
- Measurable skill development and ROI
- Engagement through gamification, recognition, and social learning
- Trusted by leading enterprises across India and Middle East
"""

OUTREACH_SYSTEM_PROMPT = """You are an expert B2B sales copywriter for an HR technology company.
Write highly personalised, warm, and compelling outreach emails to HR leaders.

Rules:
- Reference the specific article/news about the person
- Connect their initiative directly to how our platform can help
- Keep it concise: subject line + 4-5 short paragraphs
- Tone: warm, peer-to-peer, not pushy
- End with a low-pressure call to action (20-min call)
- No generic openers like "I hope this email finds you well"
- No buzzwords like "synergies", "leverage", "game-changer"

Return in this exact format:
SUBJECT: <subject line>

<email body>"""


async def generate_outreach_email(
    person_name: str,
    designation: str,
    company: str,
    context: str,
    article_title: str,
    article_summary: str,
    company_context: str = "",
) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None

    effective_context = company_context if company_context else OUR_COMPANY_CONTEXT

    prompt = f"""Write a personalised outreach email to:
Name: {person_name}
Title: {designation or 'HR Leader'}
Company: {company or 'their organisation'}
Why relevant: {context or 'mentioned in HR news'}

Article: {article_title}
Summary: {article_summary[:400] if article_summary else ''}

Our company:
{effective_context}"""

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 600,
        "system": OUTREACH_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        data = await asyncio.to_thread(_call_claude, payload, 30)
        return data["content"][0]["text"].strip()
    except Exception as e:
        logger.error(f"Outreach generation failed for {person_name}: {e}")
        return None


# ─────────────────────────────────────────────
# Ensure extracted
# ─────────────────────────────────────────────

async def ensure_extracted(db, article: dict) -> list:
    from database import get_article_people, save_people

    if article["people_extracted"]:
        existing = await get_article_people(db, article["id"])
        if existing:
            return existing
        # Marked as extracted but no people saved — re-extract

    body = article.get("body") or article.get("summary") or ""
    # Always try to fetch full text — RSS summaries rarely contain names
    from news_fetcher import fetch_article_fulltext
    full = await fetch_article_fulltext(article["source_url"])
    if full and len(full) > len(body):
        body = full
        logger.info(f"Fetched fulltext ({len(body)} chars) for: {article['title'][:60]}")
    else:
        logger.info(f"Using stored body ({len(body)} chars) for: {article['title'][:60]}")

    people = await extract_people_from_article(article["title"], body, article["source_url"])
    logger.info(f"Extraction result: {len(people)} people for '{article['title'][:60]}'")
    await save_people(db, article["id"], people)
    return people
