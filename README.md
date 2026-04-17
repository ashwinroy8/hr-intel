# HR Intel

Top HR & Learning news from India and Middle East companies — with AI-powered people identification.

## What it does

- **Login / Sign up** — secure session-based auth
- **News Feed** — aggregates HR & L&D news from 10+ RSS sources (ET HRWorld, People Matters, HR Katha, Gulf News, Arabian Business, Khaleej Times, HR Dive, ATD, SHRM, and more)
- **AI People Extraction** — uses Claude to identify every named person in each article, with their designation, company, phone, email, and LinkedIn
- **Contacts Directory** — searchable list of all people found across articles, exportable to CSV
- **Bookmarks** — save articles for later
- **Filters** — by region (India / Middle East) and category (HR / L&D)

## Quick Start

### 1. Install dependencies

```bash
cd hr-intel
pip3 install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY (required)
# Optionally add NEWSAPI_KEY for broader news
```

### 3. Run the app

```bash
python3 -m uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — sign up and you're in!

## News Sources

### RSS (free, no API key needed)
| Source | Region | Category |
|--------|--------|----------|
| ET HRWorld | India | HR |
| People Matters | India | HR |
| HR Katha | India | HR |
| Business Today HR | India | HR |
| Gulf News Business | Middle East | HR |
| Arabian Business | Middle East | HR |
| Khaleej Times | Middle East | HR |
| HR Dive | Global | HR |
| ATD (Association for Talent Development) | Global | L&D |
| eLearning Industry | Global | L&D |
| SHRM | Global | HR |

### NewsAPI (optional, `NEWSAPI_KEY`)
Adds targeted queries for India/Middle East HR & L&D news from 80,000+ sources.

## People Extraction

Powered by **Claude Haiku** (`claude-haiku-4-5`). For each article, Claude identifies:

| Field | Source |
|-------|--------|
| Name | Extracted from article |
| Designation | Extracted from article |
| Company | Extracted from article |
| Phone | Extracted if mentioned in article |
| Email | Extracted if mentioned in article |
| LinkedIn | Extracted if linked, otherwise a LinkedIn search URL is generated |

> **Note:** Phone and email are only populated when explicitly stated in the article. For contact enrichment beyond what articles contain, services like Hunter.io, Apollo.io, or Lusha can be integrated.

## Project Structure

```
hr-intel/
├── main.py              # FastAPI app + all routes
├── database.py          # SQLite helpers
├── auth.py              # Login, sessions, password hashing
├── news_fetcher.py      # RSS + NewsAPI ingestion
├── people_extractor.py  # Claude AI people extraction
├── templates/
│   ├── base.html        # Nav, layout shell
│   ├── login.html       # Login + signup
│   ├── dashboard.html   # News feed + filters
│   ├── article.html     # Article detail + people panel
│   └── contacts.html    # People directory + CSV export
├── static/
│   └── style.css
├── requirements.txt
└── .env.example
```

## Refreshing News

- On login, news is fetched automatically in the background
- Click **Refresh News** in the dashboard to fetch latest (30 min cooldown)
- The app stores all articles in SQLite — no duplicates via URL deduplication

## Costs

| Service | Cost |
|---------|------|
| RSS feeds | Free |
| Claude Haiku (extraction) | ~$0.02/day for 100 articles |
| NewsAPI free tier | Free (100 req/day) |
| **Total to get started** | **~$0** (with free tiers) |
