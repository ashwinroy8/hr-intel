"""Claude-powered people extraction from HR news articles."""
import json
import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYSTEM_PROMPT = """You are a precise data extraction engine for HR and Learning & Development industry news.

Your task: Extract every REAL person explicitly named in the article.

Rules:
- Only extract people actually named (full name or at least first + last name)
- Extract their job title/designation EXACTLY as stated in the article
- Extract company name as stated in the article
- Extract phone number ONLY if explicitly stated (format: as-is from article)
- Extract email ONLY if explicitly stated in the article
- Extract LinkedIn URL ONLY if explicitly stated in the article
- Write a brief 1-sentence context explaining why this person is mentioned
- Do NOT infer, guess, or hallucinate any contact details
- Do NOT include fictional, historical, or quoted-as-example persons

Return ONLY a valid JSON array. No markdown fences, no explanation, no preamble.
If no people are found, return an empty array: []

Example output format:
[
  {
    "name": "Priya Sharma",
    "designation": "Chief People Officer",
    "company": "Infosys",
    "phone": null,
    "email": null,
    "linkedin_url": null,
    "context": "Announced a new hybrid work policy for 300,000 employees across India."
  }
]"""


def _build_user_message(title: str, body: str) -> str:
    return f"""Article Title: {title}

Article Text:
{body[:5000]}

Extract all named people from this article."""


async def extract_people_from_article(
    title: str,
    body: str,
    article_url: str = "",
) -> list[dict]:
    """Call Claude to extract people mentioned in an article.
    Returns list of person dicts (name, designation, company, phone, email, linkedin_url, context).
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping extraction")
        return []

    if not body or len(body.strip()) < 50:
        logger.debug(f"Article too short for extraction: {title[:60]}")
        return []

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    # Use tool_use to enforce structured JSON output
    tools = [
        {
            "name": "save_people",
            "description": "Save the list of people extracted from the article.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "people": {
                        "type": "array",
                        "description": "List of people found in the article",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Full name of the person"},
                                "designation": {"type": ["string", "null"], "description": "Job title or designation"},
                                "company": {"type": ["string", "null"], "description": "Company or organisation name"},
                                "phone": {"type": ["string", "null"], "description": "Phone number if stated in article"},
                                "email": {"type": ["string", "null"], "description": "Email address if stated in article"},
                                "linkedin_url": {"type": ["string", "null"], "description": "LinkedIn profile URL if stated in article"},
                                "context": {"type": ["string", "null"], "description": "1-sentence context for why person is mentioned"},
                            },
                            "required": ["name"],
                        },
                    }
                },
                "required": ["people"],
            },
        }
    ]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=tools,
            tool_choice={"type": "auto"},
            messages=[
                {"role": "user", "content": _build_user_message(title, body)}
            ],
        )
    except anthropic.APIError as e:
        logger.error(f"Claude API error for article '{title[:60]}': {e}")
        return []

    # Parse tool_use response
    for block in response.content:
        if block.type == "tool_use" and block.name == "save_people":
            people = block.input.get("people", [])
            logger.info(f"Extracted {len(people)} people from: {title[:60]}")
            return _clean_people(people)

    # Fallback: try to parse text content as JSON
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    logger.info(f"Extracted {len(data)} people (text fallback) from: {title[:60]}")
                    return _clean_people(data)
            except json.JSONDecodeError:
                pass

    logger.debug(f"No people extracted from: {title[:60]}")
    return []


def _clean_people(raw: list) -> list[dict]:
    """Normalise and deduplicate extracted people."""
    seen_names = set()
    cleaned = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name or len(name) < 3:
            continue
        name_lower = name.lower()
        if name_lower in seen_names:
            continue
        seen_names.add(name_lower)

        # Build LinkedIn search URL if no URL provided but name + company known
        linkedin_url = p.get("linkedin_url")
        if not linkedin_url:
            company = p.get("company") or ""
            search_q = f"{name} {company}".strip()
            linkedin_url = f"https://www.linkedin.com/search/results/people/?keywords={search_q.replace(' ', '%20')}"

        cleaned.append(
            {
                "name": name,
                "designation": (p.get("designation") or "").strip() or None,
                "company": (p.get("company") or "").strip() or None,
                "phone": (p.get("phone") or "").strip() or None,
                "email": (p.get("email") or "").strip() or None,
                "linkedin_url": linkedin_url,
                "context": (p.get("context") or "").strip() or None,
            }
        )
    return cleaned


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
Write highly personalised, warm, and compelling outreach emails to HR leaders and decision makers.

Rules:
- Reference the specific article/news about the person — show you've done your homework
- Connect their initiative/challenge directly to how our platform can help
- Keep it concise: subject line + 4-5 short paragraphs max
- Tone: warm, peer-to-peer, not pushy or salesy
- End with a clear but low-pressure call to action (e.g. a 20-min call)
- Do NOT use generic openers like "I hope this email finds you well"
- Do NOT use buzzwords like "synergies", "leverage", "game-changer"
- Make the subject line specific and curiosity-driven

Return in this exact format:
SUBJECT: <subject line here>

<email body here>"""


async def generate_outreach_email(
    person_name: str,
    designation: str,
    company: str,
    context: str,
    article_title: str,
    article_summary: str,
    company_context: str = "",
) -> Optional[str]:
    """Generate a personalised sales outreach email for a person mentioned in an article."""
    if not ANTHROPIC_API_KEY:
        return None

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    effective_context = company_context if company_context else OUR_COMPANY_CONTEXT

    prompt = f"""Write a personalised outreach email to this person:

Name: {person_name}
Title: {designation or 'HR Leader'}
Company: {company or 'their organisation'}
Why they're relevant: {context or 'mentioned in HR news'}

Article that triggered this outreach:
Title: {article_title}
Summary: {article_summary[:500] if article_summary else ''}

Our company background:
{effective_context}

Write the outreach email now."""

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=OUTREACH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Outreach email generation failed for {person_name}: {e}")
        return None


async def ensure_extracted(db, article: dict) -> list[dict]:
    """Extract people for an article if not already done. Returns people list."""
    from database import get_article_people, save_people

    if article["people_extracted"]:
        return await get_article_people(db, article["id"])

    # Try to get better body text
    body = article.get("body") or article.get("summary") or ""
    if len(body) < 100:
        from news_fetcher import fetch_article_fulltext
        full = await fetch_article_fulltext(article["source_url"])
        if full:
            body = full

    people = await extract_people_from_article(article["title"], body, article["source_url"])
    await save_people(db, article["id"], people)
    return people
