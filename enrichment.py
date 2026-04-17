"""Contact enrichment via Apollo.io (primary) and Hunter.io (fallback)."""
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")


# ─────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────

def _empty_result() -> dict:
    return {
        "email": None,
        "phone": None,
        "linkedin_url": None,
        "source": None,
        "confidence": None,
    }


# ─────────────────────────────────────────────
# Apollo.io
# ─────────────────────────────────────────────

async def enrich_via_apollo(name: str, company: str) -> dict:
    """
    Match a person on Apollo and return email, phone, LinkedIn.
    Apollo People Match API: POST /api/v1/people/match
    Free tier: 50 credits/month. Each match = 1 credit only if email is found.
    """
    if not APOLLO_API_KEY:
        return _empty_result()

    parts = name.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    payload = {
        "api_key": APOLLO_API_KEY,
        "first_name": first,
        "last_name": last,
        "organization_name": company or "",
        "reveal_personal_emails": False,  # set True if on paid plan
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.apollo.io/api/v1/people/match",
                json=payload,
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
            )
            if resp.status_code == 422:
                logger.debug(f"Apollo: no match for {name} @ {company}")
                return _empty_result()
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning(f"Apollo HTTP error for {name}: {e.response.status_code}")
        return _empty_result()
    except Exception as e:
        logger.warning(f"Apollo request failed for {name}: {e}")
        return _empty_result()

    person = data.get("person") or {}
    if not person:
        return _empty_result()

    # Extract best phone
    phone = None
    for ph in (person.get("phone_numbers") or []):
        raw = ph.get("sanitized_number") or ph.get("raw_number")
        if raw:
            phone = raw
            break

    # Extract LinkedIn
    linkedin = person.get("linkedin_url")
    if linkedin and not linkedin.startswith("http"):
        linkedin = "https://" + linkedin

    email = person.get("email")
    logger.info(f"Apollo {'found' if email else 'no email'} for {name} @ {company}")

    return {
        "email": email or None,
        "phone": phone,
        "linkedin_url": linkedin,
        "source": "Apollo",
        "confidence": "high" if email else "partial",
    }


# ─────────────────────────────────────────────
# Hunter.io
# ─────────────────────────────────────────────

async def enrich_via_hunter(name: str, company: str) -> dict:
    """
    Find email via Hunter.io Email Finder.
    Free tier: 25 searches/month.
    """
    if not HUNTER_API_KEY:
        return _empty_result()

    parts = name.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    if not first or not last or not company:
        return _empty_result()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/email-finder",
                params={
                    "first_name": first,
                    "last_name": last,
                    "company": company,
                    "api_key": HUNTER_API_KEY,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"Hunter request failed for {name}: {e}")
        return _empty_result()

    result = data.get("data") or {}
    email = result.get("email")
    score = result.get("score", 0)

    logger.info(f"Hunter {'found' if email else 'no email'} for {name} @ {company} (score={score})")

    return {
        "email": email or None,
        "phone": None,
        "linkedin_url": None,
        "source": "Hunter",
        "confidence": "high" if score >= 80 else "medium" if score >= 50 else "low",
    }


# ─────────────────────────────────────────────
# Cascade pipeline
# ─────────────────────────────────────────────

async def _search_contacts_via_hunter(company_name: str) -> list:
    """
    Use Hunter.io Domain Search to find HR/L&D contacts at a company.
    Searches by company name and filters by department.
    """
    if not HUNTER_API_KEY:
        return []

    # Map common company names to domains (Hunter needs domain not company name)
    contacts = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Hunter domain search by company name — use the "company" param
            for department in ["human_resources", "executive"]:
                resp = await client.get(
                    "https://api.hunter.io/v2/domain-search",
                    params={
                        "company": company_name,
                        "department": department,
                        "limit": 5,
                        "api_key": HUNTER_API_KEY,
                    },
                )
                if not resp.is_success:
                    continue
                data = resp.json().get("data") or {}
                for person in data.get("emails") or []:
                    title = (person.get("position") or "").lower()
                    # Map to role type
                    if any(k in title for k in ["chief people", "chro", "hr director", "vp hr", "head of hr", "people officer"]):
                        role_type = "HR Head"
                    elif any(k in title for k in ["learning", "l&d", "training", "talent development"]):
                        role_type = "L&D Head"
                    elif any(k in title for k in ["ceo", "chief executive", "managing director"]):
                        role_type = "CEO"
                    elif any(k in title for k in ["sales", "revenue", "commercial"]):
                        role_type = "Sales Head"
                    else:
                        continue

                    name_parts = [person.get("first_name") or "", person.get("last_name") or ""]
                    name = " ".join(p for p in name_parts if p).strip()
                    if name:
                        contacts.append({
                            "name": name,
                            "title": person.get("position") or "",
                            "role_type": role_type,
                            "email": person.get("value"),
                            "phone": None,
                            "linkedin_url": person.get("linkedin") or None,
                            "source": "Hunter",
                        })
    except Exception as e:
        logger.warning(f"Hunter domain search failed for {company_name}: {e}")

    return contacts


async def search_contacts_at_company(company_name: str) -> list:
    """
    Search for key contacts at a company (HR Head, L&D Head, CEO, Sales Head).
    Tries Apollo first, falls back to Hunter.io domain search.
    """
    if not APOLLO_API_KEY and not HUNTER_API_KEY:
        return []

    if not APOLLO_API_KEY:
        return await _search_contacts_via_hunter(company_name)

    role_configs = [
        {
            "role_type": "HR Head",
            "titles": ["CHRO", "Chief People Officer", "HR Director", "VP HR", "Head of HR", "People Director"],
        },
        {
            "role_type": "L&D Head",
            "titles": ["Chief Learning Officer", "L&D Director", "VP Learning", "Head of Learning", "Learning Director", "Head of L&D"],
        },
        {
            "role_type": "CEO",
            "titles": ["CEO", "Chief Executive Officer", "Managing Director", "MD"],
        },
        {
            "role_type": "Sales Head",
            "titles": ["Chief Sales Officer", "VP Sales", "Sales Director", "Head of Sales", "SVP Sales"],
        },
    ]

    contacts = []

    async with httpx.AsyncClient(timeout=15) as client:
        for role_config in role_configs:
            payload = {
                "api_key": APOLLO_API_KEY,
                "q_organization_name": company_name,
                "person_titles": role_config["titles"],
                "per_page": 1,
                "page": 1,
            }
            try:
                resp = await client.post(
                    "https://api.apollo.io/api/v1/people/search",
                    json=payload,
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
                )
                if resp.status_code in (401, 403, 422):
                    logger.debug(f"Apollo search no result for {role_config['role_type']} at {company_name}: {resp.status_code}")
                    continue
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                logger.warning(f"Apollo search HTTP error for {role_config['role_type']} at {company_name}: {e.response.status_code}")
                continue
            except Exception as e:
                logger.warning(f"Apollo search failed for {role_config['role_type']} at {company_name}: {e}")
                continue

            people = data.get("people") or []
            if not people:
                continue

            person = people[0]
            name = person.get("name") or ""
            title = ""
            for emp in (person.get("employment_history") or []):
                if emp.get("current"):
                    title = emp.get("title") or ""
                    break
            if not title:
                title = (person.get("title") or "")

            # Extract phone
            phone = None
            for ph in (person.get("phone_numbers") or []):
                raw = ph.get("sanitized_number") or ph.get("raw_number")
                if raw:
                    phone = raw
                    break

            # Extract LinkedIn
            linkedin = person.get("linkedin_url")
            if linkedin and not linkedin.startswith("http"):
                linkedin = "https://" + linkedin

            email = person.get("email")

            if name:
                contacts.append({
                    "name": name,
                    "title": title,
                    "role_type": role_config["role_type"],
                    "email": email or None,
                    "phone": phone,
                    "linkedin_url": linkedin,
                    "source": "Apollo",
                })
                logger.info(f"Apollo found {role_config['role_type']} at {company_name}: {name}")

    # Fall back to Hunter if Apollo found nothing
    if not contacts:
        return await _search_contacts_via_hunter(company_name)

    return contacts


async def enrich_contact(name: str, company: str) -> dict:
    """
    Try Apollo first, fall back to Hunter.
    Returns enrichment dict with email, phone, linkedin_url, source, confidence.
    """
    if not name or not company:
        return _empty_result()

    # Stage 1: Apollo
    if APOLLO_API_KEY:
        result = await enrich_via_apollo(name, company)
        if result.get("email") or result.get("phone") or result.get("linkedin_url"):
            return result

    # Stage 2: Hunter fallback
    if HUNTER_API_KEY:
        result = await enrich_via_hunter(name, company)
        if result.get("email"):
            return result

    return {**_empty_result(), "source": "not_found"}
