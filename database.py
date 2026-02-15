import os
from datetime import datetime
from contextlib import contextmanager

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
            """)
    
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
            """)
    
    def _dict(row):
        return dict(row) if row else None


# ============ Common Functions ============

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
        cur.execute("SELECT * FROM shows WHERE id = %s" if DATABASE_URL else "SELECT * FROM shows WHERE id = ?", (show_id,))
        return _dict(cur.fetchone())

def add_show(title, service, current_season=1, current_episode=1, air_day=None, 
             priority=2, notes=None, status='watching', total_seasons=None, 
             episodes_in_season=None, tmdb_id=None, poster_url=None):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("""
                INSERT INTO shows (title, service, current_season, current_episode, air_day, 
                                  priority, notes, status, total_seasons, episodes_in_season,
                                  tmdb_id, poster_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (title, service, current_season, current_episode, air_day, priority, 
                  notes, status, total_seasons, episodes_in_season, tmdb_id, poster_url))
            return cur.fetchone()['id']
        else:
            cur.execute("""
                INSERT INTO shows (title, service, current_season, current_episode, air_day, 
                                  priority, notes, status, total_seasons, episodes_in_season,
                                  tmdb_id, poster_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title, service, current_season, current_episode, air_day, priority, 
                  notes, status, total_seasons, episodes_in_season, tmdb_id, poster_url))
            return cur.lastrowid

def update_show(show_id, **kwargs):
    with get_db() as conn:
        cur = conn.cursor()
        kwargs['updated_at'] = datetime.now().isoformat()
        
        if DATABASE_URL:
            set_clause = ", ".join(f"{k} = %s" for k in kwargs.keys())
            values = list(kwargs.values()) + [show_id]
            cur.execute(f"UPDATE shows SET {set_clause} WHERE id = %s", values)
        else:
            set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values()) + [show_id]
            cur.execute(f"UPDATE shows SET {set_clause} WHERE id = ?", values)

def mark_watched(show_id, season, episode):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("INSERT INTO watch_history (show_id, season, episode) VALUES (%s, %s, %s)", 
                       (show_id, season, episode))
            cur.execute("UPDATE shows SET current_season = %s, current_episode = %s, updated_at = %s WHERE id = %s",
                       (season, episode + 1, datetime.now().isoformat(), show_id))
        else:
            cur.execute("INSERT INTO watch_history (show_id, season, episode) VALUES (?, ?, ?)", 
                       (show_id, season, episode))
            cur.execute("UPDATE shows SET current_season = ?, current_episode = ?, updated_at = ? WHERE id = ?",
                       (season, episode + 1, datetime.now().isoformat(), show_id))

def delete_show(show_id):
    with get_db() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("DELETE FROM watch_history WHERE show_id = %s", (show_id,))
            cur.execute("DELETE FROM shows WHERE id = %s", (show_id,))
        else:
            cur.execute("DELETE FROM watch_history WHERE show_id = ?", (show_id,))
            cur.execute("DELETE FROM shows WHERE id = ?", (show_id,))

def dismiss_recommendation(trakt_slug):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            if DATABASE_URL:
                cur.execute("INSERT INTO dismissed_recommendations (trakt_slug) VALUES (%s) ON CONFLICT DO NOTHING", (trakt_slug,))
            else:
                cur.execute("INSERT OR IGNORE INTO dismissed_recommendations (trakt_slug) VALUES (?)", (trakt_slug,))
        except:
            pass

def get_dismissed_recommendations():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM dismissed_recommendations")
        return [_dict(row) for row in cur.fetchall()]

def seed_kens_shows():
    """Seed database with Ken's current watchlist - only if empty"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM shows")
        result = cur.fetchone()
        count = result['count'] if DATABASE_URL else result[0]
        
        if count > 0:
            return  # Already has data
    
    shows = [
        ("The Pitt", "Max", 1, 99, "Thursday", 1, "Caught up - new eps weekly", "current"),
        ("Shrinking", "Apple TV+", 3, 99, "Tuesday", 1, "Caught up", "current"),
        ("Hijack", "Apple TV+", 2, 99, "Tuesday", 1, "S2 through March 4", "current"),
        ("Will Trent", "Hulu", 4, 99, "Tuesday", 3, "Watch if nothing else on", "current"),
        ("Law & Order", "Peacock", 25, 99, "Thursday", 2, "Caught up-ish", "current"),
        ("Trying", "Apple TV+", 4, 2, None, 2, "Catching up", "watching"),
        ("Bad Sisters", "Apple TV+", 2, 6, None, 1, "S2 in progress", "watching"),
        ("Annika", "Prime Video", 1, 2, None, 1, "Bought on Prime", "watching"),
        ("Landman", "Paramount+", 1, 99, None, 2, "S1 complete - S2 TBD", "hiatus"),
        ("High Potential", "Hulu", 1, 99, "Tuesday", 3, "Watch if nothing else on", "current"),
        ("Endeavour", "Prime Video", 7, 1, "Weekend", 2, "Weekend show, nap-friendly", "watching"),
    ]
    
    for show in shows:
        add_show(
            title=show[0],
            service=show[1],
            current_season=show[2],
            current_episode=show[3],
            air_day=show[4],
            priority=show[5],
            notes=show[6],
            status=show[7]
        )

# Initialize on import
init_db()
