import sqlite3
import os
from datetime import datetime, date
from pathlib import Path

DB_PATH = os.environ.get("DATABASE_PATH", "whats_on.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
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
    """)
    conn.commit()
    conn.close()

def get_all_shows():
    conn = get_db()
    shows = conn.execute("""
        SELECT * FROM shows 
        WHERE status != 'dropped'
        ORDER BY priority ASC, air_day ASC, title ASC
    """).fetchall()
    conn.close()
    return [dict(s) for s in shows]

def get_show(show_id):
    conn = get_db()
    show = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
    conn.close()
    return dict(show) if show else None

def add_show(title, service, current_season=1, current_episode=1, air_day=None, 
             priority=2, notes=None, status='watching', total_seasons=None, 
             episodes_in_season=None, tmdb_id=None, poster_url=None):
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO shows (title, service, current_season, current_episode, air_day, 
                          priority, notes, status, total_seasons, episodes_in_season,
                          tmdb_id, poster_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, service, current_season, current_episode, air_day, priority, 
          notes, status, total_seasons, episodes_in_season, tmdb_id, poster_url))
    conn.commit()
    show_id = cursor.lastrowid
    conn.close()
    return show_id

def update_show(show_id, **kwargs):
    conn = get_db()
    kwargs['updated_at'] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
    values = list(kwargs.values()) + [show_id]
    conn.execute(f"UPDATE shows SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()

def mark_watched(show_id, season, episode):
    conn = get_db()
    # Log to history
    conn.execute("""
        INSERT INTO watch_history (show_id, season, episode)
        VALUES (?, ?, ?)
    """, (show_id, season, episode))
    # Update current position
    conn.execute("""
        UPDATE shows SET current_season = ?, current_episode = ?, updated_at = ?
        WHERE id = ?
    """, (season, episode + 1, datetime.now().isoformat(), show_id))
    conn.commit()
    conn.close()

def delete_show(show_id):
    conn = get_db()
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    conn.execute("DELETE FROM watch_history WHERE show_id = ?", (show_id,))
    conn.commit()
    conn.close()

def seed_kens_shows():
    """Seed database with Ken's current watchlist"""
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    conn.close()
    
    if existing > 0:
        return  # Already seeded
    
    shows = [
        # (title, service, season, episode, air_day, priority, notes, status)
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
