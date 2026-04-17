"""HR Intel - FastAPI application entry point."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Form, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import aiosqlite

import database as db
import auth as auth_module
from news_fetcher import fetch_all_news
from people_extractor import ensure_extracted, generate_outreach_email
from enrichment import enrich_contact

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# ─────────────────────────────────────────────
# App lifecycle
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    logger.info("Database initialised")

    # Schedule news fetch: daily at 5:30 AM + every 4 hours
    scheduler.add_job(background_news_fetch, CronTrigger(hour=5, minute=30), id="daily_530")
    scheduler.add_job(background_news_fetch, "interval", hours=4, id="every_4h")
    scheduler.start()
    logger.info("Scheduler started: daily 5:30 AM + every 4 hours")

    # Initial fetch on startup
    asyncio.create_task(background_news_fetch())
    yield

    scheduler.shutdown()
    logger.info("Shutting down")


app = FastAPI(title="HR Intel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ─────────────────────────────────────────────
# DB dependency
# ─────────────────────────────────────────────

async def get_db_conn():
    conn = await db.get_db()
    try:
        yield conn
    finally:
        await conn.close()


# ─────────────────────────────────────────────
# Background: News fetch
# ─────────────────────────────────────────────

_fetch_lock = asyncio.Lock()
_last_fetch: Optional[float] = None
FETCH_COOLDOWN = 60 * 30  # 30 min manual cooldown


async def background_news_fetch():
    global _last_fetch
    if _fetch_lock.locked():
        return
    async with _fetch_lock:
        _last_fetch = time.time()
        logger.info("News fetch started…")
        articles = await fetch_all_news()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            saved = 0
            for article in articles:
                row_id = await db.upsert_article(conn, article)
                if row_id:
                    saved += 1
        logger.info(f"News fetch complete: {saved} new / {len(articles)} total")


# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────

def set_session(response, user_id: int):
    token = auth_module.create_session_token(user_id)
    response.set_cookie(
        auth_module.SESSION_COOKIE, token,
        max_age=auth_module.MAX_AGE, httponly=True, samesite="lax",
    )


async def require_login(request: Request, conn=Depends(get_db_conn)):
    user = await auth_module.get_current_user(request, conn)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


# ─────────────────────────────────────────────
# Routes: Auth
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, conn=Depends(get_db_conn)):
    user = await auth_module.get_current_user(request, conn)
    if user:
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "mode": "login"})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), conn=Depends(get_db_conn)):
    user = await auth_module.login_user(conn, email, password)
    if not user:
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "Invalid email or password.", "mode": "login"}, status_code=401)
    response = RedirectResponse("/dashboard", status_code=302)
    set_session(response, user["id"])
    return response


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "mode": "signup"})


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), conn=Depends(get_db_conn)):
    if len(password) < 8:
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "Password must be at least 8 characters.", "mode": "signup"}, status_code=400)
    user = await auth_module.register_user(conn, email, password, name)
    if not user:
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "Email already registered.", "mode": "signup"}, status_code=400)
    response = RedirectResponse("/dashboard", status_code=302)
    set_session(response, user["id"])
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(auth_module.SESSION_COOKIE)
    return response


# ─────────────────────────────────────────────
# Routes: Dashboard
# ─────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request, region: str = "All", category: str = "All",
    q: str = "", page: int = 1, has_people: str = "0",
    conn=Depends(get_db_conn), user=Depends(require_login),
):
    limit = 20
    offset = (page - 1) * limit

    # Multi-value source filter from checkboxes
    selected_sources = request.query_params.getlist("source")

    articles = await db.search_articles(conn, q, limit=limit) if q else \
               await db.get_articles(conn, region=region, category=category,
                                     sources=selected_sources or None,
                                     has_people=(has_people == "1"),
                                     limit=limit, offset=offset)

    bookmarked_ids = await db.get_bookmarked_ids(conn, user["id"])
    stats = await db.get_stats(conn)
    pipeline_counts = await db.get_pipeline_counts(conn)
    all_sources = await db.get_sources(conn)

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "articles": articles,
        "bookmarked_ids": bookmarked_ids, "stats": stats,
        "pipeline_counts": pipeline_counts,
        "region": region, "category": category, "q": q,
        "has_people": has_people,
        "page": page, "has_next": len(articles) == limit,
        "last_fetch": _last_fetch,
        "all_sources": all_sources,
        "selected_sources": selected_sources,
    })


# ─────────────────────────────────────────────
# Routes: Article
# ─────────────────────────────────────────────

@app.get("/article/{article_id}", response_class=HTMLResponse)
async def article_detail(request: Request, article_id: int, conn=Depends(get_db_conn), user=Depends(require_login)):
    article = await db.get_article(conn, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    people = await db.get_article_people(conn, article_id)
    if not article["people_extracted"]:
        asyncio.create_task(_extract_and_ignore(article))

    bookmarked_ids = await db.get_bookmarked_ids(conn, user["id"])
    settings = await db.get_settings(conn)

    return templates.TemplateResponse("article.html", {
        "request": request, "user": user, "article": article,
        "people": people, "bookmarked": article_id in bookmarked_ids,
        "extracting": not article["people_extracted"],
        "company_name": settings.get("company_name", "our platform"),
    })


async def _extract_and_ignore(article: dict):
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await ensure_extracted(conn, article)
    except Exception as e:
        logger.error(f"Background extraction failed: {e}")


@app.post("/article/{article_id}/extract")
async def trigger_extraction(article_id: int, conn=Depends(get_db_conn), user=Depends(require_login)):
    import os
    article = await db.get_article(conn, article_id)
    if not article:
        raise HTTPException(status_code=404)
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured on server")
    try:
        people = await ensure_extracted(conn, article)
    except Exception as e:
        logger.error(f"Extraction error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse({"count": len(people), "people": people})


@app.get("/admin/debug-extract/{article_id}")
async def debug_extract(article_id: int, conn=Depends(get_db_conn), user=Depends(require_login)):
    """Debug endpoint: shows body text and raw Claude response for an article."""
    import os
    from news_fetcher import fetch_article_fulltext
    from people_extractor import _call_claude, _headers, SYSTEM_PROMPT
    article = await db.get_article(conn, article_id)
    if not article:
        raise HTTPException(status_code=404)
    body = article.get("body") or article.get("summary") or ""
    full = await fetch_article_fulltext(article["source_url"])
    if full and len(full) > len(body):
        body = full
        body_source = "fulltext"
    else:
        body_source = "stored"
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "tools": [{"name": "save_people", "description": "Save extracted people.", "input_schema": {"type": "object", "properties": {"people": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}}, "required": ["people"]}}],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": f"Article Title: {article['title']}\n\nArticle Text:\n{body[:5000]}\n\nExtract all named people."}],
    }
    try:
        import asyncio
        data = await asyncio.to_thread(_call_claude, payload)
        return JSONResponse({"body_source": body_source, "body_length": len(body), "body_preview": body[:300], "claude_response": data})
    except Exception as e:
        return JSONResponse({"error": str(e), "body_source": body_source, "body_length": len(body)})


@app.post("/admin/reset-extraction")
async def reset_extraction(conn=Depends(get_db_conn), user=Depends(require_login)):
    """Reset people_extracted flag for articles that have no people saved, so they get re-extracted."""
    await conn.execute("""
        UPDATE articles SET people_extracted = 0
        WHERE people_extracted = 1
        AND id NOT IN (SELECT DISTINCT article_id FROM article_people)
    """)
    await conn.commit()
    return JSONResponse({"status": "ok", "message": "Reset extraction flag for empty articles"})


@app.post("/article/{article_id}/person/{person_id}/outreach")
async def generate_person_outreach(
    article_id: int, person_id: int,
    conn=Depends(get_db_conn), user=Depends(require_login),
):
    article = await db.get_article(conn, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    people = await db.get_article_people(conn, article_id)
    person = next((p for p in people if p["id"] == person_id), None)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")

    if person.get("outreach_email"):
        return JSONResponse({"email": person["outreach_email"], "cached": True})

    settings = await db.get_settings(conn)
    email = await generate_outreach_email(
        person_name=person["name"],
        designation=person["designation"] or "",
        company=person["company"] or "",
        context=person["context"] or "",
        article_title=article["title"],
        article_summary=article["summary"] or article["body"] or "",
        company_context=settings.get("company_context", ""),
    )
    if not email:
        raise HTTPException(status_code=503, detail="Email generation failed — check ANTHROPIC_API_KEY")

    await db.save_outreach_email(conn, person_id, email)
    return JSONResponse({"email": email, "cached": False})


# ─────────────────────────────────────────────
# Routes: Bookmarks
# ─────────────────────────────────────────────

@app.post("/bookmark/{article_id}")
async def toggle_bookmark(article_id: int, request: Request, conn=Depends(get_db_conn), user=Depends(require_login)):
    await db.toggle_bookmark(conn, user["id"], article_id)
    referer = request.headers.get("referer", "/dashboard")
    return RedirectResponse(referer, status_code=302)


@app.get("/bookmarks", response_class=HTMLResponse)
async def bookmarks_page(request: Request, conn=Depends(get_db_conn), user=Depends(require_login)):
    articles = await db.get_bookmarked_articles(conn, user["id"])
    bookmarked_ids = {a["id"] for a in articles}
    stats = await db.get_stats(conn)
    pipeline_counts = await db.get_pipeline_counts(conn)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "articles": articles,
        "bookmarked_ids": bookmarked_ids, "stats": stats,
        "pipeline_counts": pipeline_counts,
        "region": "All", "category": "All", "q": "", "page": 1,
        "has_next": False, "page_title": "Saved Articles",
    })


# ─────────────────────────────────────────────
# Routes: Contacts + Pipeline
# ─────────────────────────────────────────────

@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, q: str = "", status: str = "All", conn=Depends(get_db_conn), user=Depends(require_login)):
    people = await db.get_all_people(conn, limit=500, status_filter=status if status != "All" else None)
    if q:
        q_lower = q.lower()
        people = [p for p in people if
                  q_lower in (p.get("name") or "").lower() or
                  q_lower in (p.get("company") or "").lower() or
                  q_lower in (p.get("designation") or "").lower()]
    stats = await db.get_stats(conn)
    pipeline_counts = await db.get_pipeline_counts(conn)
    return templates.TemplateResponse("contacts.html", {
        "request": request, "user": user, "people": people,
        "q": q, "status": status, "stats": stats, "pipeline_counts": pipeline_counts,
    })


@app.post("/contacts/{person_id}/status")
async def update_contact_status(
    person_id: int, request: Request,
    outreach_status: str = Form(...),
    notes: str = Form(""),
    conn=Depends(get_db_conn), user=Depends(require_login),
):
    await db.update_person_status(conn, person_id, outreach_status, notes)
    referer = request.headers.get("referer", "/contacts")
    return RedirectResponse(referer, status_code=302)


@app.post("/contacts/{person_id}/outreach")
async def generate_contact_outreach(person_id: int, conn=Depends(get_db_conn), user=Depends(require_login)):
    """Generate outreach email from contacts page."""
    person = await db.get_person_by_id(conn, person_id)
    if not person:
        raise HTTPException(status_code=404)

    if person.get("outreach_email"):
        return JSONResponse({"email": person["outreach_email"], "cached": True})

    settings = await db.get_settings(conn)
    email = await generate_outreach_email(
        person_name=person["name"],
        designation=person["designation"] or "",
        company=person["company"] or "",
        context=person["context"] or "",
        article_title=person.get("article_title", ""),
        article_summary="",
        company_context=settings.get("company_context", ""),
    )
    if not email:
        raise HTTPException(status_code=503, detail="Email generation failed — check ANTHROPIC_API_KEY")

    await db.save_outreach_email(conn, person_id, email)
    return JSONResponse({"email": email, "cached": False})


# ─────────────────────────────────────────────
# Routes: Settings
# ─────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, conn=Depends(get_db_conn), user=Depends(require_login)):
    settings = await db.get_settings(conn)
    stats = await db.get_stats(conn)
    pipeline_counts = await db.get_pipeline_counts(conn)
    next_run = None
    job = scheduler.get_job("daily_530")
    if job:
        next_run = str(job.next_run_time)[:16] if job.next_run_time else None
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "settings": settings,
        "stats": stats, "pipeline_counts": pipeline_counts,
        "next_fetch": next_run,
    })


@app.post("/settings")
async def save_settings(
    request: Request,
    company_name: str = Form(...),
    company_context: str = Form(...),
    conn=Depends(get_db_conn), user=Depends(require_login),
):
    await db.save_settings(conn, {"company_name": company_name, "company_context": company_context})
    return RedirectResponse("/settings?saved=1", status_code=302)


# ─────────────────────────────────────────────
# Routes: Admin
# ─────────────────────────────────────────────

@app.post("/admin/refresh-news")
async def refresh_news(background_tasks: BackgroundTasks, user=Depends(require_login)):
    if _last_fetch and (time.time() - _last_fetch) < FETCH_COOLDOWN:
        remaining = int(FETCH_COOLDOWN - (time.time() - _last_fetch))
        return JSONResponse({"status": "cooldown", "retry_in_seconds": remaining})
    background_tasks.add_task(background_news_fetch)
    return JSONResponse({"status": "started"})


@app.post("/contacts/{person_id}/enrich")
async def enrich_person(person_id: int, conn=Depends(get_db_conn), user=Depends(require_login)):
    """Look up email + phone via Apollo/Hunter and save to DB."""
    person = await db.get_person_by_id(conn, person_id)
    if not person:
        raise HTTPException(status_code=404)

    # Skip if already enriched
    if person.get("email") and person.get("enrichment_status") in ("Apollo", "Hunter"):
        return JSONResponse({
            "email": person["email"], "phone": person.get("phone"),
            "linkedin_url": person.get("linkedin_url"),
            "source": person["enrichment_status"], "cached": True,
        })

    if not person.get("company"):
        raise HTTPException(status_code=400, detail="Company name required for enrichment")

    result = await enrich_contact(person["name"], person["company"])
    await db.save_enrichment(conn, person_id, result)

    return JSONResponse({
        "email": result.get("email"),
        "phone": result.get("phone"),
        "linkedin_url": result.get("linkedin_url"),
        "source": result.get("source"),
        "confidence": result.get("confidence"),
        "cached": False,
        "found": bool(result.get("email") or result.get("phone")),
    })


@app.get("/api/stats")
async def api_stats(conn=Depends(get_db_conn), user=Depends(require_login)):
    return await db.get_stats(conn)
