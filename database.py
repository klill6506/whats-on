import os
from contextlib import contextmanager
from datetime import datetime

# Whitelist of columns that can be updated via the API
ALLOWED_FIELDS = {
    'title', 'service', 'status', 'current_season', 'current_episode',
    'total_seasons', 'episodes_in_season', 'air_day', 'priority', 'rating',
    'notes', 'tmdb_id', 'poster_url', 'trakt_slug', 'updated_at'
}

# Check for PostgreSQL (Render) or SQLite (local)
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # PostgreSQL mode (psycopg3)
    import psycopg
    from psycopg.rows import dict_row

    # Render uses postgres:// but psycopg needs postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    @contextmanager
    def get_db():
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db():
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shows (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    service TEXT NOT NULL,
                    status TEXT DEFAULT 'watching',
                    current_season INTEGER DEFAULT 1,
                    current_episode INTEGER DEFAULT 1,
                    total_seasons INTEGER,
                    episodes_in_season INTEGER,
                    air_day TEXT,
                    priority INTEGER DEFAULT 2,
                    rating INTEGER,
                    notes TEXT,
                    tmdb_id INTEGER,
                    poster_url TEXT,
                    trakt_slug TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS watch_history (
                    id SERIAL PRIMARY KEY,
                    show_id INTEGER NOT NULL REFERENCES shows(id),
                    season INTEGER NOT NULL,
                    episode INTEGER NOT NULL,
                    watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS dismissed_recommendations (
                    id SERIAL PRIMARY KEY,
                    trakt_slug TEXT UNIQUE NOT NULL,
                    dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS recommendation_cache (
                    id SERIAL PRIMARY KEY,
                    trakt_slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    overview TEXT,
                    poster_url TEXT,
                    score REAL DEFAULT 0,
                    source_show TEXT,
                    genres TEXT,
                    streaming_services TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # Migrate: add rating column if missing (existing DBs)
            _migrate_add_column(cur, 'shows', 'rating', 'INTEGER')

    def _migrate_add_column(cur, table, column, col_type):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # column already exists

    def _dict(row):
        return dict(row) if row else None

else:
    # SQLite mode (local development)
    import sqlite3

    DB_PATH = os.environ.get("DATABASE_PATH", "whats_on.db")

    @contextmanager
    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db():
        with get_db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS shows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    service TEXT NOT NULL,
                    status TEXT DEFAULT 'watching',
                    current_season INTEGER DEFAULT 1,
                    current_episode INTEGER DEFAULT 1,
                    total_seasons INTEGER,
                    episodes_in_season INTEGER,
                    air_day TEXT,
                    priority INTEGER DEFAULT 2,
                    rating INTEGER,
                    notes TEXT,
                    tmdb_id INTEGER,
                    poster_url TEXT,
                    trakt_slug TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS watch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    show_id INTEGER NOT NULL,
                    season INTEGER NOT NULL,
                    episode INTEGER NOT NULL,
                    watched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (show_id) REFERENCES shows(id)
                );

                CREATE TABLE IF NOT EXISTS dismissed_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trakt_slug TEXT UNIQUE NOT NULL,
                    dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS recommendation_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trakt_slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    overview TEXT,
                    poster_url TEXT,
                    score REAL DEFAULT 0,
                    source_show TEXT,
                    genres TEXT,
                    streaming_services TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # Migrate: add rating column if missing (existing DBs)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(shows)").fetchall()]
            if 'rating' not in columns:
                conn.execute("ALTER TABLE shows ADD COLUMN rating INTEGER")

    def _dict(row):
        return dict(row) if row else None


# ============ Common Functions ============

def _ph(n=1):
    """Return placeholder(s) for the current DB engine."""
    p = "%s" if DATABASE_URL else "?"
    return ", ".join([p] * n) if n > 1 else p


def get_all_shows():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM shows
            WHERE status != 'dropped'
            ORDER BY priority ASC, air_day ASC, title ASC
        """)
        return [_dict(row) for row in cur.fetchall()]

def get_show(show_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM shows WHERE id = {_ph()}", (show_id,))
        return _dict(cur.fetchone())

def add_show(title, service, current_season=1, current_episode=1, air_day=None,
             priority=2, notes=None, status='watching', total_seasons=None,
             episodes_in_season=None, tmdb_id=None, poster_url=None, rating=None):
    with get_db() as conn:
        cur = conn.cursor()
        ph = _ph(13)
        sql = f"""
            INSERT INTO shows (title, service, current_season, current_episode, air_day,
                              priority, notes, status, total_seasons, episodes_in_season,
                              tmdb_id, poster_url, rating)
            VALUES ({ph})
        """
        params = (title, service, current_season, current_episode, air_day, priority,
                  notes, status, total_seasons, episodes_in_season, tmdb_id, poster_url, rating)
        if DATABASE_URL:
            cur.execute(sql + " RETURNING id", params)
            return cur.fetchone()['id']
        else:
            cur.execute(sql, params)
            return cur.lastrowid

def update_show(show_id, **kwargs):
    # Filter to allowed fields only (prevents SQL injection via column names)
    kwargs = {k: v for k, v in kwargs.items() if k in ALLOWED_FIELDS}
    if not kwargs:
        return
    with get_db() as conn:
        cur = conn.cursor()
        kwargs['updated_at'] = datetime.now().isoformat()
        ph = _ph()
        set_clause = ", ".join(f"{k} = {ph}" for k in kwargs.keys())
        values = list(kwargs.values()) + [show_id]
        cur.execute(f"UPDATE shows SET {set_clause} WHERE id = {ph}", values)

def mark_watched(show_id, season, episode):
    with get_db() as conn:
        cur = conn.cursor()
        ph = _ph()
        cur.execute(f"INSERT INTO watch_history (show_id, season, episode) VALUES ({_ph(3)})",
                    (show_id, season, episode))
        cur.execute(f"UPDATE shows SET current_season = {ph}, current_episode = {ph}, updated_at = {ph} WHERE id = {ph}",
                    (season, episode + 1, datetime.now().isoformat(), show_id))

def delete_show(show_id):
    with get_db() as conn:
        cur = conn.cursor()
        ph = _ph()
        cur.execute(f"DELETE FROM watch_history WHERE show_id = {ph}", (show_id,))
        cur.execute(f"DELETE FROM shows WHERE id = {ph}", (show_id,))

def dismiss_recommendation(trakt_slug):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            if DATABASE_URL:
                cur.execute("INSERT INTO dismissed_recommendations (trakt_slug) VALUES (%s) ON CONFLICT DO NOTHING", (trakt_slug,))
            else:
                cur.execute("INSERT OR IGNORE INTO dismissed_recommendations (trakt_slug) VALUES (?)", (trakt_slug,))
        except Exception:
            pass

def get_dismissed_slugs():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT trakt_slug FROM dismissed_recommendations")
        return {row['trakt_slug'] if DATABASE_URL else row[0] for row in cur.fetchall()}

def get_dismissed_recommendations():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dismissed_recommendations")
        return [_dict(row) for row in cur.fetchall()]


# ============ Recommendation Cache ============

def clear_recommendation_cache():
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM recommendation_cache")

def insert_cached_recommendation(trakt_slug, title, year, overview, poster_url,
                                  score, source_show, genres, streaming_services):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            INSERT INTO recommendation_cache
                (trakt_slug, title, year, overview, poster_url, score, source_show, genres, streaming_services)
            VALUES ({_ph(9)})
        """, (trakt_slug, title, year, overview, poster_url, score, source_show, genres, streaming_services))

def get_cache_age():
    """Returns the cached_at of the newest row, or None if empty."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(cached_at) as latest FROM recommendation_cache")
        row = cur.fetchone()
        if row:
            val = row['latest'] if DATABASE_URL else row[0]
            if val:
                return datetime.fromisoformat(str(val).replace('Z', '+00:00').split('+')[0])
    return None

def get_cached_recommendations(user_services, limit=6, offset=0):
    """Get cached recs filtered by streaming service, excluding dismissed/watched shows."""
    dismissed = get_dismissed_slugs()
    existing = {s.get('trakt_slug') for s in get_all_shows() if s.get('trakt_slug')}
    excluded = dismissed | existing

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recommendation_cache ORDER BY score DESC")
        all_recs = [_dict(row) for row in cur.fetchall()]

    # Filter: must be on a user service, not excluded
    filtered = []
    for rec in all_recs:
        if rec['trakt_slug'] in excluded:
            continue
        services = (rec.get('streaming_services') or '').split(',')
        services = [s.strip() for s in services if s.strip()]
        if services and any(s in user_services for s in services):
            rec['_services'] = [s for s in services if s in user_services]
            filtered.append(rec)

    total = len(filtered)
    return filtered[offset:offset + limit], total

def get_recommendation_count():
    """Total number of cached recommendations."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM recommendation_cache")
        row = cur.fetchone()
        return row['cnt'] if DATABASE_URL else row[0]


# ============ Dedup ============

def dedup_shows():
    """Remove duplicate shows, keeping the one with a poster (or the most recently updated)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM shows ORDER BY title, updated_at DESC")
        all_shows = [_dict(row) for row in cur.fetchall()]

    from collections import defaultdict
    groups = defaultdict(list)
    for show in all_shows:
        groups[show['title'].strip().lower()].append(show)

    removed = 0
    for title, dupes in groups.items():
        if len(dupes) < 2:
            continue
        dupes.sort(key=lambda s: (
            bool(s.get('poster_url')),
            bool(s.get('trakt_slug')),
            s.get('updated_at') or '',
        ), reverse=True)
        keeper = dupes[0]
        for dupe in dupes[1:]:
            if not keeper.get('poster_url') and dupe.get('poster_url'):
                update_show(keeper['id'], poster_url=dupe['poster_url'])
            if not keeper.get('trakt_slug') and dupe.get('trakt_slug'):
                update_show(keeper['id'], trakt_slug=dupe['trakt_slug'])
            delete_show(dupe['id'])
            removed += 1
    return removed


# ============ Seed ============

def seed_kens_shows():
    """Seed database with Ken's current watchlist, only once ever."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT value FROM meta WHERE key = {_ph()}", ('seeded',))
        if cur.fetchone():
            return

    # (title, service, season, episode, air_day, priority, notes, status, rating)
    shows = [
        ("The Pitt", "Max", 1, 99, "Thursday", 1, "Caught up - new eps weekly", "current", 5),
        ("Shrinking", "Apple TV+", 3, 99, "Tuesday", 1, "Caught up", "current", 5),
        ("Hijack", "Apple TV+", 2, 99, "Tuesday", 1, "S2 through March 4", "current", 4),
        ("Will Trent", "Hulu", 4, 99, "Tuesday", 3, "Watch if nothing else on", "current", 3),
        ("Law & Order", "Peacock", 25, 99, "Thursday", 2, "Caught up-ish", "current", 2),
        ("Trying", "Apple TV+", 4, 2, None, 2, "Catching up", "watching", 4),
        ("Bad Sisters", "Apple TV+", 2, 6, None, 1, "S2 in progress", "watching", 4),
        ("Annika", "Prime Video", 1, 2, None, 1, "Bought on Prime", "watching", 4),
        ("Landman", "Paramount+", 1, 99, None, 2, "S1 complete - S2 TBD", "hiatus", 3),
        ("High Potential", "Hulu", 1, 99, "Tuesday", 3, "Watch if nothing else on", "current", 3),
        ("Endeavour", "Prime Video", 7, 1, "Weekend", 2, "Weekend show, nap-friendly", "watching", 4),
    ]

    for show in shows:
        add_show(
            title=show[0], service=show[1], current_season=show[2],
            current_episode=show[3], air_day=show[4], priority=show[5],
            notes=show[6], status=show[7], rating=show[8]
        )

    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", ('seeded', '1'))
        else:
            cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", ('seeded', '1'))

# Initialize on import
init_db()
