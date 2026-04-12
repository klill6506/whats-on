import os
import random
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel, field_validator
from typing import Optional

import database as db

# --- Config ---

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

TRAKT_CLIENT_ID = os.environ.get(
    "TRAKT_CLIENT_ID",
    "947255b4c65a76d7d7f29ce500d333f22de5641dbc0bf1bd701c325acfa74434"
)
TRAKT_BASE_URL = "https://api.trakt.tv"

# Ken's streaming services — used to filter recommendations
USER_SERVICES = {'Max', 'Apple TV+', 'Hulu', 'Peacock', 'Paramount+', 'Prime Video', 'Netflix'}

# How often to rebuild the recommendation cache
CACHE_MAX_AGE = timedelta(hours=12)

VALID_STATUSES = {'watching', 'current', 'hiatus', 'dropped'}
VALID_SERVICES = USER_SERVICES | {'Disney+', 'Other'}


# --- Pydantic models ---

class ShowUpdate(BaseModel):
    title: Optional[str] = None
    service: Optional[str] = None
    status: Optional[str] = None
    current_season: Optional[int] = None
    current_episode: Optional[int] = None
    air_day: Optional[str] = None
    priority: Optional[int] = None
    rating: Optional[int] = None
    notes: Optional[str] = None
    tmdb_id: Optional[int] = None
    poster_url: Optional[str] = None
    trakt_slug: Optional[str] = None

    @field_validator('status')
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f'status must be one of {VALID_STATUSES}')
        return v

    @field_validator('service')
    @classmethod
    def validate_service(cls, v):
        if v is not None and v not in VALID_SERVICES:
            raise ValueError(f'service must be one of {VALID_SERVICES}')
        return v

    @field_validator('priority')
    @classmethod
    def validate_priority(cls, v):
        if v is not None and not (1 <= v <= 5):
            raise ValueError('priority must be between 1 and 5')
        return v

    @field_validator('rating')
    @classmethod
    def validate_rating(cls, v):
        if v is not None and not (1 <= v <= 5):
            raise ValueError('rating must be between 1 and 5')
        return v

    @field_validator('current_season', 'current_episode')
    @classmethod
    def validate_positive(cls, v):
        if v is not None and v < 1:
            raise ValueError('must be >= 1')
        return v


# --- App setup ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.seed_kens_shows()
    app.state.http_client = httpx.AsyncClient(timeout=15.0)
    yield
    await app.state.http_client.aclose()

app = FastAPI(title="What's On?", lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _client(request: Request = None) -> httpx.AsyncClient:
    """Get the shared HTTP client, or fall back to creating a new one."""
    if request and hasattr(request, 'app') and hasattr(request.app, 'state'):
        return getattr(request.app.state, 'http_client', None) or httpx.AsyncClient(timeout=15.0)
    return httpx.AsyncClient(timeout=15.0)


# --- Trakt helpers ---

def trakt_headers():
    return {
        "trakt-api-key": TRAKT_CLIENT_ID,
        "trakt-api-version": "2",
        "Content-Type": "application/json"
    }

async def search_trakt(title: str, client: httpx.AsyncClient = None):
    close = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{TRAKT_BASE_URL}/search/show",
            params={"query": title},
            headers=trakt_headers()
        )
        data = resp.json()
        if data:
            show = data[0]["show"]
            return {
                "trakt_id": show.get("ids", {}).get("trakt"),
                "trakt_slug": show.get("ids", {}).get("slug"),
                "imdb_id": show.get("ids", {}).get("imdb"),
                "title": show.get("title"),
                "year": show.get("year"),
                "status": show.get("status"),
            }
    except Exception as e:
        print(f"Trakt search error: {e}")
    finally:
        if close:
            await client.aclose()
    return None

async def get_trakt_show_details(slug: str, client: httpx.AsyncClient = None):
    close = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        show_resp = await client.get(
            f"{TRAKT_BASE_URL}/shows/{slug}",
            params={"extended": "full"},
            headers=trakt_headers()
        )
        show_data = show_resp.json()

        next_ep_resp = await client.get(
            f"{TRAKT_BASE_URL}/shows/{slug}/next_episode",
            params={"extended": "full"},
            headers=trakt_headers()
        )
        next_ep = next_ep_resp.json() if next_ep_resp.status_code == 200 else None

        airs = show_data.get("airs", {})
        return {
            "title": show_data.get("title"),
            "year": show_data.get("year"),
            "status": show_data.get("status"),
            "network": show_data.get("network"),
            "air_day": airs.get("day"),
            "air_time": airs.get("time"),
            "runtime": show_data.get("runtime"),
            "genres": show_data.get("genres", []),
            "overview": show_data.get("overview"),
            "rating": show_data.get("rating"),
            "trakt_slug": slug,
            "next_episode": {
                "season": next_ep.get("season"),
                "episode": next_ep.get("number"),
                "title": next_ep.get("title"),
                "air_date": next_ep.get("first_aired"),
            } if next_ep else None,
        }
    except Exception as e:
        print(f"Trakt show details error: {e}")
    finally:
        if close:
            await client.aclose()
    return None

async def fetch_trakt_related(slug: str, client: httpx.AsyncClient, limit: int = 10):
    try:
        resp = await client.get(
            f"{TRAKT_BASE_URL}/shows/{slug}/related",
            params={"limit": limit, "extended": "full"},
            headers=trakt_headers()
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"Trakt related error for {slug}: {e}")
        return []


# --- TMDB helpers ---

async def search_tmdb(title: str, client: httpx.AsyncClient = None):
    if not TMDB_API_KEY:
        return None, None
    close = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{TMDB_BASE_URL}/search/tv",
            params={"api_key": TMDB_API_KEY, "query": title}
        )
        data = resp.json()
        if data.get("results"):
            show = data["results"][0]
            poster_path = show.get("poster_path")
            poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            return show.get("id"), poster_url
    except Exception as e:
        print(f"TMDB search error: {e}")
    finally:
        if close:
            await client.aclose()
    return None, None

async def get_tmdb_providers(tmdb_id: int, client: httpx.AsyncClient) -> list:
    """Get US streaming providers for a show from TMDB."""
    if not TMDB_API_KEY:
        return []
    try:
        resp = await client.get(
            f"{TMDB_BASE_URL}/tv/{tmdb_id}/watch/providers",
            params={"api_key": TMDB_API_KEY}
        )
        data = resp.json()
        us = data.get("results", {}).get("US", {})
        providers = []
        for p in us.get("flatrate", []):
            name = p.get("provider_name", "")
            if "HBO" in name or "Max" in name:
                providers.append("Max")
            elif "Apple" in name:
                providers.append("Apple TV+")
            elif "Hulu" in name:
                providers.append("Hulu")
            elif "Peacock" in name:
                providers.append("Peacock")
            elif "Prime" in name or "Amazon" in name:
                providers.append("Prime Video")
            elif "Netflix" in name:
                providers.append("Netflix")
            elif "Paramount" in name:
                providers.append("Paramount+")
            elif "Disney" in name:
                providers.append("Disney+")
        return list(set(providers))
    except Exception as e:
        print(f"TMDB providers error: {e}")
        return []


# ============ RECOMMENDATION ENGINE ============

async def refresh_recommendation_cache(client: httpx.AsyncClient):
    """Rebuild the recommendation cache using taste-aware scoring."""
    shows = db.get_all_shows()
    if not shows:
        return

    existing_slugs = {s.get('trakt_slug') for s in shows if s.get('trakt_slug')}
    existing_titles = {s['title'].lower() for s in shows}
    dismissed_slugs = db.get_dismissed_slugs()
    excluded = existing_slugs | dismissed_slugs

    # Step 1: Build weighted genre profile from ALL shows
    genre_weights = defaultdict(float)
    shows_with_slugs = []
    for show in shows:
        slug = show.get('trakt_slug')
        if not slug:
            # Try to find slug via search
            info = await search_trakt(show['title'], client)
            if info and info.get('trakt_slug'):
                slug = info['trakt_slug']
                db.update_show(show['id'], trakt_slug=slug)
            else:
                continue
        rating = show.get('rating') or 2  # unrated defaults to 2
        shows_with_slugs.append((show, slug, rating))

        details = await get_trakt_show_details(slug, client)
        if details:
            for genre in details.get('genres', []):
                genre_weights[genre] += rating

    # Step 2: Fetch related shows for top-rated shows (collaborative filtering)
    top_shows = sorted(shows_with_slugs, key=lambda x: x[2], reverse=True)[:5]
    candidates = {}  # slug -> {data + _score}

    for show, slug, rating in top_shows:
        related = await fetch_trakt_related(slug, client, limit=15)
        for rec in related:
            rec_slug = rec.get("ids", {}).get("slug")
            if not rec_slug or rec_slug in excluded or rec.get("title", "").lower() in existing_titles:
                continue
            if rec_slug not in candidates:
                candidates[rec_slug] = rec
                candidates[rec_slug]['_score'] = 0.0
                candidates[rec_slug]['_source'] = show['title']
            # Source weight: higher-rated source shows produce stronger signal
            candidates[rec_slug]['_score'] += rating * 2

    # Step 3: Score by genre affinity
    max_genre_weight = max(genre_weights.values()) if genre_weights else 1
    for slug, rec in candidates.items():
        genres = rec.get('genres', [])
        genre_score = sum(genre_weights.get(g, 0) for g in genres) / max_genre_weight
        rec['_score'] += genre_score * 5
        # Small random factor for rotation variety
        rec['_score'] += random.random() * 2
        # Bonus for highly-rated shows on Trakt
        trakt_rating = rec.get('rating', 0) or 0
        if trakt_rating >= 8.0:
            rec['_score'] += 3
        elif trakt_rating >= 7.0:
            rec['_score'] += 1

    # Step 4: Sort and take top 40 for TMDB enrichment
    sorted_candidates = sorted(candidates.values(), key=lambda x: x.get('_score', 0), reverse=True)[:40]

    # Step 5: Enrich with TMDB (poster + streaming providers)
    new_cache = []
    for rec in sorted_candidates:
        title = rec.get('title', '')
        tmdb_id, poster_url = await search_tmdb(title, client)

        services = []
        if tmdb_id:
            services = await get_tmdb_providers(tmdb_id, client)

        genres = ",".join(rec.get('genres', []))
        streaming = ",".join(services)
        overview = rec.get('overview', '') or ''
        if len(overview) > 300:
            overview = overview[:300] + '...'

        new_cache.append({
            'trakt_slug': rec.get('ids', {}).get('slug', ''),
            'title': title,
            'year': rec.get('year'),
            'overview': overview,
            'poster_url': poster_url,
            'score': rec.get('_score', 0),
            'source_show': rec.get('_source', ''),
            'genres': genres,
            'streaming_services': streaming,
        })

    # Step 6: Write to cache (only replace after new data is ready)
    if new_cache:
        db.clear_recommendation_cache()
        for item in new_cache:
            db.insert_cached_recommendation(**item)

    print(f"Recommendation cache refreshed: {len(new_cache)} candidates")


# ============ PAGES ============

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    shows = db.get_all_shows()

    # Categorize shows
    priority_shows = []
    backup_shows = []
    catching_up = []
    between_seasons = []

    for show in shows:
        if show['status'] == 'hiatus':
            between_seasons.append(show)
        elif show['priority'] == 3:
            backup_shows.append(show)
        elif show['current_episode'] != 99 and show['status'] == 'watching':
            catching_up.append(show)
        else:
            priority_shows.append(show)

    day_order = {
        "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3,
        "Thursday": 4, "Friday": 5, "Saturday": 6, None: 99
    }
    priority_shows.sort(key=lambda x: (day_order.get(x.get('air_day'), 99), x.get('title', '')))

    # --- Recommendations (server-side, cached) ---
    recommendations = []
    try:
        # Check if cache needs refresh
        cache_age = db.get_cache_age()
        if not cache_age or (datetime.now() - cache_age) > CACHE_MAX_AGE:
            client = _client(request)
            await refresh_recommendation_cache(client)

        recs, total = db.get_cached_recommendations(USER_SERVICES, limit=6, offset=0)
        if total > 6:
            # Random window for rotation
            offset = random.randint(0, max(0, total - 6))
            recs, _ = db.get_cached_recommendations(USER_SERVICES, limit=6, offset=offset)
        recommendations = recs
    except Exception as e:
        print(f"Error loading recommendations: {e}")

    return templates.TemplateResponse("index.html", {
        "request": request,
        "priority_shows": priority_shows,
        "backup_shows": backup_shows,
        "catching_up": catching_up,
        "between_seasons": between_seasons,
        "recommendations": recommendations,
    })


# ============ JSON API ============

@app.get("/api/shows")
async def api_shows():
    return db.get_all_shows()

@app.get("/api/shows/{show_id}")
async def api_get_show(show_id: int):
    show = db.get_show(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    return show

@app.post("/api/shows")
async def api_add_show(
    title: str = Form(...),
    service: str = Form(...),
    current_season: int = Form(1),
    current_episode: int = Form(1),
    air_day: str = Form(None),
    priority: int = Form(2),
    notes: str = Form(None),
    status: str = Form("watching")
):
    tmdb_id, poster_url = await search_tmdb(title)
    db.add_show(
        title=title, service=service,
        current_season=current_season, current_episode=current_episode,
        air_day=air_day if air_day else None,
        priority=priority, notes=notes if notes else None,
        status=status, tmdb_id=tmdb_id, poster_url=poster_url,
    )
    return RedirectResponse(url="/", status_code=303)

@app.put("/api/shows/{show_id}")
async def api_update_show(show_id: int, update: ShowUpdate):
    if not db.get_show(show_id):
        raise HTTPException(status_code=404, detail="Show not found")
    data = update.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    db.update_show(show_id, **data)
    return {"message": "Show updated"}

@app.post("/api/shows/{show_id}/watched")
async def api_mark_watched(show_id: int, season: int = Form(...), episode: int = Form(...)):
    db.mark_watched(show_id, season, episode)
    return {"message": "Episode marked as watched"}

@app.delete("/api/shows/{show_id}")
async def api_delete_show(show_id: int):
    db.delete_show(show_id)
    return {"message": "Show deleted"}

@app.post("/api/shows/{show_id}/caught-up")
async def api_mark_caught_up(show_id: int):
    db.update_show(show_id, current_episode=99, status='current')
    return {"message": "Marked as caught up"}

@app.post("/api/shows/{show_id}/rate")
async def api_rate_show(show_id: int, request: Request):
    data = await request.json()
    rating = data.get('rating')
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        raise HTTPException(status_code=400, detail="rating must be 1-5")
    db.update_show(show_id, rating=rating)
    return {"message": "Rating saved", "rating": rating}

@app.post("/api/dedup")
async def api_dedup():
    removed = db.dedup_shows()
    return {"removed": removed}

@app.post("/api/refresh-recommendations")
async def api_refresh_recommendations(request: Request):
    """Force rebuild the recommendation cache."""
    client = _client(request)
    await refresh_recommendation_cache(client)
    return {"message": "Recommendations refreshed"}


# ============ FORM HANDLERS (web UI) ============

@app.post("/edit-show/{show_id}")
async def edit_show(show_id: int, season: int = Form(...), episode: int = Form(...)):
    db.update_show(show_id, current_season=season, current_episode=episode, status='watching')
    return RedirectResponse(url="/", status_code=303)

@app.post("/next-episode/{show_id}")
async def next_episode(show_id: int):
    show = db.get_show(show_id)
    if show and show['current_episode'] != 99:
        db.update_show(show_id, current_episode=show['current_episode'] + 1, status='watching')
    return RedirectResponse(url="/", status_code=303)

@app.post("/mark-caught-up/{show_id}")
async def mark_caught_up(show_id: int):
    db.update_show(show_id, current_episode=99, status='current')
    return RedirectResponse(url="/", status_code=303)

@app.post("/mark-hiatus/{show_id}")
async def mark_hiatus(show_id: int):
    db.update_show(show_id, status='hiatus')
    return RedirectResponse(url="/", status_code=303)

@app.post("/mark-active/{show_id}")
async def mark_active(show_id: int):
    show = db.get_show(show_id)
    if show:
        db.update_show(show_id, current_season=show['current_season'] + 1,
                        current_episode=1, status='watching')
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete-show/{show_id}")
async def delete_show_ui(show_id: int):
    db.delete_show(show_id)
    return RedirectResponse(url="/", status_code=303)


# ============ POSTER / TRAKT FETCH ============

@app.post("/api/shows/{show_id}/fetch-poster")
async def fetch_poster(show_id: int):
    show = db.get_show(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    tmdb_id, poster_url = await search_tmdb(show['title'])
    if poster_url:
        db.update_show(show_id, tmdb_id=tmdb_id, poster_url=poster_url)
        return {"poster_url": poster_url}
    return {"error": "No poster found"}

@app.post("/api/fetch-all-posters")
async def fetch_all_posters():
    shows = db.get_all_shows()
    updated = 0
    for show in shows:
        if not show.get('poster_url'):
            tmdb_id, poster_url = await search_tmdb(show['title'])
            if poster_url:
                db.update_show(show['id'], tmdb_id=tmdb_id, poster_url=poster_url)
                updated += 1
    return {"updated": updated}

@app.get("/api/trakt/search")
async def api_trakt_search(q: str):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TRAKT_BASE_URL}/search/show",
                params={"query": q, "limit": 10},
                headers=trakt_headers()
            )
            data = resp.json()
            return {"results": [
                {
                    "title": item.get("show", {}).get("title"),
                    "year": item.get("show", {}).get("year"),
                    "trakt_slug": item.get("show", {}).get("ids", {}).get("slug"),
                    "trakt_id": item.get("show", {}).get("ids", {}).get("trakt"),
                    "overview": item.get("show", {}).get("overview", "")[:200],
                }
                for item in data
            ]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/trakt/show/{slug}")
async def api_trakt_show(slug: str):
    details = await get_trakt_show_details(slug)
    if details:
        return details
    raise HTTPException(status_code=404, detail="Show not found on Trakt")

@app.post("/api/shows/{show_id}/fetch-trakt")
async def fetch_trakt_for_show(show_id: int):
    show = db.get_show(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    trakt_info = await search_trakt(show['title'])
    if not trakt_info or not trakt_info.get('trakt_slug'):
        return {"error": "Show not found on Trakt"}
    details = await get_trakt_show_details(trakt_info['trakt_slug'])
    if details:
        update_data = {"trakt_slug": trakt_info['trakt_slug']}
        if details.get('air_day'):
            update_data['air_day'] = details['air_day']
        db.update_show(show_id, **update_data)
        return {"updated": True, "air_day": details.get('air_day'),
                "next_episode": details.get('next_episode'), "status": details.get('status')}
    return {"error": "Could not fetch show details"}

@app.post("/api/fetch-all-trakt")
async def fetch_all_trakt():
    shows = db.get_all_shows()
    updated = 0
    for show in shows:
        if not show.get('air_day'):
            trakt_info = await search_trakt(show['title'])
            if trakt_info and trakt_info.get('trakt_slug'):
                details = await get_trakt_show_details(trakt_info['trakt_slug'])
                if details and details.get('air_day'):
                    db.update_show(show['id'], trakt_slug=trakt_info['trakt_slug'],
                                   air_day=details['air_day'])
                    updated += 1
    return {"updated": updated}


# ============ RECOMMENDATION ACTIONS ============

@app.post("/api/recommendations/dismiss")
async def dismiss_recommendation_api(trakt_slug: str = Form(...)):
    db.dismiss_recommendation(trakt_slug)
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/recommendations/add")
async def add_recommendation(
    title: str = Form(...),
    trakt_slug: str = Form(...),
    service: str = Form("Other"),
    priority: int = Form(2)
):
    _, poster_url = await search_tmdb(title)
    air_day = None
    details = await get_trakt_show_details(trakt_slug)
    if details:
        air_day = details.get('air_day')
    db.add_show(
        title=title, service=service, current_season=1, current_episode=1,
        air_day=air_day, priority=priority, status='watching', poster_url=poster_url,
    )
    db.dismiss_recommendation(trakt_slug)
    return RedirectResponse(url="/", status_code=303)
