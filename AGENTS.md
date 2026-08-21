# AGENTS.md — What's On

*Project-specific rules. User-level conventions come from `~/.Codex/AGENTS.md` — do not duplicate here.*

---

## What this is

"What's On?" — a personal, mobile-first TV show tracker for Ken. Tracks what he's
watching, what he's caught up on, what's on hiatus, and surfaces taste-aware
recommendations. Single-user, no auth, no clutter. **This is a standalone personal
app — NOT part of the Sherpa tax suite.**

## Tech stack

- **Backend:** FastAPI (Python) — `main.py` (routes) + `database.py` (data layer)
- **Frontend:** Server-rendered Jinja2, single template `templates/index.html`; Tailwind CSS via CDN
- **Database:** Dual-mode — SQLite when no `DATABASE_URL` (local + Coolify prod), PostgreSQL (psycopg3) when set (legacy Render prod)
- **Hosting:** Coolify on kenlill.com — https://whatson.kenlill.com (Dockerfile build,
  SQLite on a `/data` volume). Render (`render.yaml`) is the legacy deploy, running in
  parallel until Ken suspends it; it runs Postgres via a dashboard `DATABASE_URL`.
  Migration playbook: `dev\sherpa-server\MIGRATION-RENDER-TO-COOLIFY.md`
- **Dependencies:** pip (`requirements.txt`) — not Poetry

## Session startup

Read all four root files (AGENTS.md, MEMORY.md, STATUS.md, DECISIONS.md) at the
start of substantive work. No other required reading.

## Conventions

- This app does **not** inherit the Sherpa-suite database/color rules from
  `~/.Codex/AGENTS.md`. Specifically:
  - **No shared Supabase DB.** It owns its own SQLite/Postgres database. No `firm_id`,
    no central `clients` table.
  - **No tax data-entry color system** (red/yellow/green). UI colors here are
    service-brand colors (Max, Hulu, etc.) in `index.html`.
- Tests, if added, go in `tests/` as `test_{module}.py` (pytest) — per global rules.
- Keep the SQLite/Postgres dual-path working: `database.py` branches on `DATABASE_URL`.
  Any new query must use the `_ph()` placeholder helper so it works on both engines.

## Do not redesign without asking

- The category logic on the home page (priority / backup / catching-up / between-seasons)
  and the `current_episode = 99` "caught up" sentinel. Both are load-bearing.
- The recommendation engine scoring in `refresh_recommendation_cache()` — taste-aware,
  genre-weighted, collaborative via Trakt "related." Tune deliberately, not casually.
- The mobile-first single-page `index.html` layout and service-brand colors.

## Known constraints

- **External APIs:** Trakt (recommendations, air days, related shows) and TMDB
  (posters, US streaming providers). TMDB requires `TMDB_API_KEY`; without it, poster
  and provider lookups silently no-op. Trakt has a hardcoded fallback client ID.
- **Python version:** `render.yaml` pins 3.11; global standard is 3.12+; local is 3.14.
  History shows psycopg/Python-version churn — verify compatibility before bumping.
- `update_show()` only writes columns in `ALLOWED_FIELDS` (SQL-injection guard) — add
  new columns there too.
