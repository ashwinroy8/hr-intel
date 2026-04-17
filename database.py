"""Database layer using SQLite via aiosqlite."""
import aiosqlite
import os
from datetime import datetime
from typing import Optional, Set, List

DB_PATH = os.getenv("DB_PATH", "/data/hr_intel.db" if os.path.isdir("/data") else "hr_intel.db")

PIPELINE_STATUSES = ["New", "Contacted", "Responded", "Meeting Set", "Not Relevant"]


async def get_db():
    return await aiosqlite.connect(DB_PATH)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT,
                source_url TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                body TEXT,
                published_at TEXT,
                region TEXT,
                category TEXT,
                fetched_at TEXT DEFAULT (datetime('now')),
                people_extracted INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS article_people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                name TEXT NOT NULL,
                designation TEXT,
                company TEXT,
                phone TEXT,
                email TEXT,
                linkedin_url TEXT,
                context TEXT,
                outreach_email TEXT,
                outreach_status TEXT DEFAULT 'New',
                notes TEXT,
                enrichment_status TEXT DEFAULT 'article_only',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                article_id INTEGER NOT NULL REFERENCES articles(id),
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, article_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_articles_region ON articles(region);
            CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_people_article ON article_people(article_id);
        """)

        # Migrations for existing databases — safe to re-run
        for col, definition in [
            ("outreach_email", "TEXT"),
            ("outreach_status", "TEXT DEFAULT 'New'"),
            ("notes", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE article_people ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # Index on outreach_status — must come after the column migration
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_people_status ON article_people(outreach_status)")
        except Exception:
            pass

        # Target companies and contacts tables
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS target_companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                industry TEXT,
                region TEXT,
                signal TEXT,
                signal_summary TEXT,
                employee_size TEXT,
                article_id INTEGER,
                generated_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS target_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                name TEXT,
                title TEXT,
                role_type TEXT,
                email TEXT,
                phone TEXT,
                linkedin_url TEXT,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (company_id) REFERENCES target_companies(id)
            );
        """)

        await db.commit()


# ─────────────────────────────────────────────
# Articles
# ─────────────────────────────────────────────

async def upsert_article(db, article: dict) -> Optional[int]:
    try:
        cursor = await db.execute(
            """INSERT INTO articles (source_name, source_url, title, summary, body, published_at, region, category)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (article.get("source_name"), article["source_url"], article["title"],
             article.get("summary", ""), article.get("body", ""),
             article.get("published_at", datetime.utcnow().isoformat()),
             article.get("region", "India"), article.get("category", "HR")),
        )
        await db.commit()
        return cursor.lastrowid
    except aiosqlite.IntegrityError:
        return None


async def get_sources(db) -> list:
    """Return distinct source names that have articles, sorted alphabetically."""
    async with db.execute(
        "SELECT DISTINCT source_name FROM articles WHERE source_name IS NOT NULL ORDER BY source_name"
    ) as cursor:
        return [r[0] for r in await cursor.fetchall()]


async def get_articles(db, region: str = None, category: str = None, sources: list = None, has_people: bool = False, limit: int = 30, offset: int = 0):
    query = """SELECT a.*, (SELECT COUNT(*) FROM article_people ap WHERE ap.article_id = a.id) as people_count
               FROM articles a WHERE 1=1"""
    params = []
    if region and region != "All":
        query += " AND region = ?"
        params.append(region)
    if category and category != "All":
        query += " AND category = ?"
        params.append(category)
    if sources:
        placeholders = ",".join("?" * len(sources))
        query += f" AND source_name IN ({placeholders})"
        params.extend(sources)
    if has_people:
        query += " AND (SELECT COUNT(*) FROM article_people ap WHERE ap.article_id = a.id) > 0"
    query += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


async def get_article(db, article_id: int):
    async with db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return None


async def search_articles(db, query: str, limit: int = 20):
    q = f"%{query}%"
    async with db.execute(
        "SELECT * FROM articles WHERE title LIKE ? OR summary LIKE ? ORDER BY fetched_at DESC LIMIT ?",
        (q, q, limit),
    ) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


# ─────────────────────────────────────────────
# People
# ─────────────────────────────────────────────

async def get_article_people(db, article_id: int):
    async with db.execute(
        "SELECT * FROM article_people WHERE article_id = ? ORDER BY id", (article_id,)
    ) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


async def save_people(db, article_id: int, people: list):
    for person in people:
        await db.execute(
            """INSERT INTO article_people
               (article_id, name, designation, company, phone, email, linkedin_url, context, outreach_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'New')""",
            (article_id, person.get("name", ""), person.get("designation"),
             person.get("company"), person.get("phone"), person.get("email"),
             person.get("linkedin_url"), person.get("context")),
        )
    await db.execute("UPDATE articles SET people_extracted = 1 WHERE id = ?", (article_id,))
    await db.commit()


async def get_all_people(db, limit: int = 500, status_filter: str = None):
    query = """SELECT ap.*, a.title as article_title, a.source_url as article_url,
               a.source_name, a.region, a.id as article_id
               FROM article_people ap
               JOIN articles a ON a.id = ap.article_id"""
    params = []
    if status_filter:
        query += " WHERE ap.outreach_status = ?"
        params.append(status_filter)
    query += " ORDER BY ap.created_at DESC LIMIT ?"
    params.append(limit)
    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


async def get_person_by_id(db, person_id: int):
    async with db.execute(
        """SELECT ap.*, a.title as article_title, a.source_url as article_url, a.source_name
           FROM article_people ap JOIN articles a ON a.id = ap.article_id
           WHERE ap.id = ?""",
        (person_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
    return None


async def save_enrichment(db, person_id: int, enrichment: dict):
    """Update a person record with enriched contact data."""
    fields, params = [], []
    if enrichment.get("email"):
        fields.append("email = ?"); params.append(enrichment["email"])
    if enrichment.get("phone"):
        fields.append("phone = ?"); params.append(enrichment["phone"])
    if enrichment.get("linkedin_url"):
        fields.append("linkedin_url = ?"); params.append(enrichment["linkedin_url"])
    if enrichment.get("source"):
        fields.append("enrichment_status = ?"); params.append(enrichment["source"])
    if not fields:
        return
    params.append(person_id)
    await db.execute(f"UPDATE article_people SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()


async def save_outreach_email(db, person_id: int, outreach_email: str):
    await db.execute(
        "UPDATE article_people SET outreach_email = ? WHERE id = ?",
        (outreach_email, person_id),
    )
    await db.commit()


async def update_person_status(db, person_id: int, status: str, notes: str = ""):
    await db.execute(
        "UPDATE article_people SET outreach_status = ?, notes = ? WHERE id = ?",
        (status, notes, person_id),
    )
    await db.commit()


async def get_pipeline_counts(db) -> dict:
    counts = {s: 0 for s in PIPELINE_STATUSES}
    async with db.execute(
        "SELECT outreach_status, COUNT(*) FROM article_people GROUP BY outreach_status"
    ) as cursor:
        for row in await cursor.fetchall():
            if row[0] in counts:
                counts[row[0]] = row[1]
    return counts


# ─────────────────────────────────────────────
# Bookmarks
# ─────────────────────────────────────────────

async def toggle_bookmark(db, user_id: int, article_id: int) -> bool:
    try:
        await db.execute(
            "INSERT INTO user_bookmarks (user_id, article_id) VALUES (?, ?)",
            (user_id, article_id),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        await db.execute(
            "DELETE FROM user_bookmarks WHERE user_id = ? AND article_id = ?",
            (user_id, article_id),
        )
        await db.commit()
        return False


async def get_bookmarked_ids(db, user_id: int) -> Set[int]:
    async with db.execute(
        "SELECT article_id FROM user_bookmarks WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {r[0] for r in await cursor.fetchall()}


async def get_bookmarked_articles(db, user_id: int):
    async with db.execute(
        """SELECT a.* FROM articles a
           JOIN user_bookmarks b ON a.id = b.article_id
           WHERE b.user_id = ? ORDER BY b.created_at DESC""",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

async def get_settings(db) -> dict:
    async with db.execute("SELECT key, value FROM settings") as cursor:
        rows = await cursor.fetchall()
        result = {r[0]: r[1] for r in rows}
    if "company_name" not in result:
        result["company_name"] = "Our Skilling & Engagement Platform"
    if "company_context" not in result:
        result["company_context"] = (
            "We are a skilling and employee engagement platform that helps organisations "
            "upskill their workforce, drive a learning culture, and boost employee engagement "
            "through technology-led solutions including learning management, assessments, "
            "gamification, and engagement tools. Trusted by leading enterprises across India and Middle East."
        )
    return result


async def save_settings(db, settings: dict):
    for key, value in settings.items():
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    await db.commit()


# ─────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────

async def get_stats(db) -> dict:
    stats = {}
    async with db.execute("SELECT COUNT(*) FROM articles") as c:
        stats["total_articles"] = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM article_people") as c:
        stats["total_people"] = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(DISTINCT article_id) FROM article_people") as c:
        stats["articles_with_people"] = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM article_people WHERE outreach_email IS NOT NULL") as c:
        stats["emails_drafted"] = (await c.fetchone())[0]
    async with db.execute("SELECT COUNT(*) FROM article_people WHERE outreach_status = 'Contacted'") as c:
        stats["contacted"] = (await c.fetchone())[0]
    return stats


# ─────────────────────────────────────────────
# Target Companies
# ─────────────────────────────────────────────

async def get_today_targets(db) -> list:
    """Return today's target companies with their contacts joined."""
    from datetime import date
    today = date.today().isoformat()
    async with db.execute(
        "SELECT * FROM target_companies WHERE generated_date = ? ORDER BY id",
        (today,)
    ) as cursor:
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        companies = [dict(zip(cols, r)) for r in rows]

    for company in companies:
        async with db.execute(
            "SELECT * FROM target_contacts WHERE company_id = ? ORDER BY id",
            (company["id"],)
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            company["contacts"] = [dict(zip(cols, r)) for r in rows]

    return companies


async def save_targets(db, companies: list):
    """Delete today's existing targets and insert new ones with contacts."""
    from datetime import date
    today = date.today().isoformat()

    # Delete today's existing targets and their contacts (via cascade-like approach)
    async with db.execute(
        "SELECT id FROM target_companies WHERE generated_date = ?", (today,)
    ) as cursor:
        existing_ids = [r[0] for r in await cursor.fetchall()]

    for cid in existing_ids:
        await db.execute("DELETE FROM target_contacts WHERE company_id = ?", (cid,))
    if existing_ids:
        placeholders = ",".join("?" * len(existing_ids))
        await db.execute(f"DELETE FROM target_companies WHERE id IN ({placeholders})", existing_ids)

    for company in companies:
        cursor = await db.execute(
            """INSERT INTO target_companies
               (company_name, industry, region, signal, signal_summary, employee_size, article_id, generated_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company.get("company_name"),
                company.get("industry"),
                company.get("region"),
                company.get("signal"),
                company.get("signal_summary"),
                company.get("employee_size"),
                company.get("article_id"),
                today,
            ),
        )
        company_id = cursor.lastrowid

        for contact in company.get("contacts", []):
            await db.execute(
                """INSERT INTO target_contacts
                   (company_id, name, title, role_type, email, phone, linkedin_url, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    company_id,
                    contact.get("name"),
                    contact.get("title"),
                    contact.get("role_type"),
                    contact.get("email"),
                    contact.get("phone"),
                    contact.get("linkedin_url"),
                    contact.get("source"),
                ),
            )

    await db.commit()


async def get_target_count_today(db) -> int:
    """Count of today's target companies."""
    from datetime import date
    today = date.today().isoformat()
    async with db.execute(
        "SELECT COUNT(*) FROM target_companies WHERE generated_date = ?", (today,)
    ) as cursor:
        return (await cursor.fetchone())[0]
