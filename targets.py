"""Identify target companies from news articles using Claude."""
import asyncio
import json
import logging
import os
import requests
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _headers():
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def _call_claude(payload, timeout=60):
    resp = requests.post(ANTHROPIC_URL, headers=_headers(), json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


SYSTEM_PROMPT = """You are a sales intelligence analyst for MobCast, an enterprise Learning & Development (L&D) SaaS platform.

Your task: Analyse the provided news articles and identify companies that are strong Ideal Customer Profiles (ICPs) for MobCast.

ICP criteria:
- Companies with 1000+ employees
- Strong buying signals: expanding operations, opening new offices/stores, reskilling workforce, AI adoption, digital transformation, hiring at scale, L&D investment, new leadership appointments
- Industries: IT/Tech, BFSI, Manufacturing, Retail, Healthcare, Consulting, Energy, Real Estate, Logistics
- Regions: India, Middle East, or Global

Exclusions:
- Colleges, schools, universities, government education departments
- Small companies (under 1000 employees)
- Companies with no clear buying signal

Be precise — only include companies where the article clearly signals an L&D or workforce development need."""

TOOL_SCHEMA = {
    "name": "save_target_companies",
    "description": "Save the identified target companies that are strong ICPs for MobCast enterprise L&D platform.",
    "input_schema": {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company_name": {"type": "string", "description": "Official company name"},
                        "industry": {"type": "string", "description": "Industry sector (e.g. IT, BFSI, Manufacturing, Retail)"},
                        "region": {"type": "string", "description": "Region: India, Middle East, or Global"},
                        "signal": {
                            "type": "string",
                            "enum": [
                                "expansion",
                                "reskilling",
                                "AI adoption",
                                "digital transformation",
                                "hiring surge",
                                "leadership change",
                                "L&D investment",
                            ],
                        },
                        "signal_summary": {"type": "string", "description": "1 sentence explaining why this company is a target"},
                        "employee_size": {"type": "string", "description": "Estimated employee size, e.g. '5000+', '1000-5000', 'Large Enterprise'"},
                    },
                    "required": ["company_name", "signal", "signal_summary"],
                },
            }
        },
        "required": ["companies"],
    },
}


async def identify_target_companies(articles: list) -> list:
    """
    Takes a list of article dicts and uses Claude to identify companies that
    are strong ICPs for MobCast enterprise L&D software.
    Returns up to 50 company dicts.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping target identification")
        return []

    if not articles:
        logger.info("No articles provided for target identification")
        return []

    all_companies: dict[str, dict] = {}  # keyed by company_name.lower() for deduplication

    # Send up to 30 articles per batch
    batch_size = 30
    batches = [articles[i : i + batch_size] for i in range(0, len(articles), batch_size)]

    for batch_num, batch in enumerate(batches, 1):
        articles_text = ""
        for i, article in enumerate(batch, 1):
            title = article.get("title", "")
            summary = article.get("summary") or article.get("body") or ""
            source = article.get("source_name", "")
            region = article.get("region", "")
            articles_text += f"\n---\nArticle {i}: {title}\nSource: {source} | Region: {region}\n{summary[:600]}\n"

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "tools": [TOOL_SCHEMA],
            "tool_choice": {"type": "any"},
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Here are {len(batch)} recent news articles. "
                        "Identify all companies that are strong ICPs for MobCast enterprise L&D software.\n\n"
                        f"{articles_text}\n\n"
                        "Return only companies with clear signals. Focus on quality over quantity."
                    ),
                }
            ],
        }

        try:
            logger.info(f"Calling Claude for target identification (batch {batch_num}/{len(batches)}, {len(batch)} articles)")
            data = await asyncio.to_thread(_call_claude, payload)
        except requests.exceptions.RequestException as e:
            logger.error(f"Claude request failed for target identification (batch {batch_num}): {e}")
            continue
        except Exception as e:
            logger.error(f"Target identification error (batch {batch_num}): {e}")
            continue

        # Parse tool_use response
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "save_target_companies":
                companies = block.get("input", {}).get("companies", [])
                logger.info(f"Claude identified {len(companies)} target companies in batch {batch_num}")
                for company in companies:
                    key = (company.get("company_name") or "").strip().lower()
                    if key and key not in all_companies:
                        all_companies[key] = {
                            "company_name": (company.get("company_name") or "").strip(),
                            "industry": company.get("industry"),
                            "region": company.get("region"),
                            "signal": company.get("signal"),
                            "signal_summary": company.get("signal_summary"),
                            "employee_size": company.get("employee_size"),
                        }
                break
        else:
            logger.warning(f"No tool_use block in Claude response for batch {batch_num}")

        # Stop if we already have 50 companies
        if len(all_companies) >= 50:
            break

    result = list(all_companies.values())[:50]
    logger.info(f"Total unique target companies identified: {len(result)}")
    return result
