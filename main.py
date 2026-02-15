from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import database as db
import httpx
import os

app = FastAPI(title="What's On?")

# Templates
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# TMDB API for posters
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

# Trakt API for show data
TRAKT_CLIENT_ID = os.environ.get("TRAKT_CLIENT_ID", "947255b4c65a76d7d7f29ce500d333f22de5641dbc0bf1bd701c325acfa74434")
TRAKT_BASE_URL = "https://api.trakt.tv"

def trakt_headers():
    return {
        "trakt-api-key": TRAKT_CLIENT_ID,
        "trakt-api-version": "2",
        "Content-Type": "application/json"
    }

async def search_tmdb(title: str):
    """Search TMDB for a show and return poster URL and TMDB ID"""
    if not TMDB_API_KEY:
        return None, None
    
    try:
        async with httpx.AsyncClient() as client:
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
    
    return None, None

# Seed Ken's shows on startup
@app.on_event("startup")
async def startup():
    db.seed_kens_shows()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    shows = db.get_all_shows()
    
    # Categorize shows into the 4 rows
    priority_shows = []      # Priority 1 + currently airing
    backup_shows = []        # Priority 3 - "when nothing else is on"
    catching_up = []         # Shows we're behind on (not at ep 99)
    between_seasons = []     # Shows on hiatus
    
    for show in shows:
        if show['status'] == 'hiatus':
            between_seasons.append(show)
        elif show['priority'] == 3:
            backup_shows.append(show)
        elif show['current_episode'] != 99 and show['status'] == 'watching':
            catching_up.append(show)
        else:
            priority_shows.append(show)
    
    # Sort priority shows by air day (puts current day first eventually)
    day_order = {
        "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3,
        "Thursday": 4, "Friday": 5, "Saturday": 6, None: 99
    }
    priority_shows.sort(key=lambda x: (day_order.get(x.get('air_day'), 99), x.get('title', '')))
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "priority_shows": priority_shows,
        "backup_shows": backup_shows,
        "catching_up": catching_up,
        "between_seasons": between_seasons
    })

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
    # Try to fetch poster from TMDB
    tmdb_id, poster_url = await search_tmdb(title)
    
    show_id = db.add_show(
        title=title,
        service=service,
        current_season=current_season,
        current_episode=current_episode,
        air_day=air_day if air_day else None,
        priority=priority,
        notes=notes if notes else None,
        status=status,
        tmdb_id=tmdb_id,
        poster_url=poster_url
    )
    return RedirectResponse(url="/", status_code=303)

@app.put("/api/shows/{show_id}")
async def api_update_show(show_id: int, request: Request):
    data = await request.json()
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

# Form handlers for the web UI
@app.post("/edit-show/{show_id}")
async def edit_show(show_id: int, season: int = Form(...), episode: int = Form(...)):
    db.update_show(show_id, current_season=season, current_episode=episode, status='watching')
    return RedirectResponse(url="/", status_code=303)

@app.post("/next-episode/{show_id}")
async def next_episode(show_id: int):
    show = db.get_show(show_id)
    if show:
        new_ep = show['current_episode'] + 1 if show['current_episode'] != 99 else 2
        db.update_show(show_id, current_episode=new_ep, status='watching')
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
        # Start new season
        db.update_show(
            show_id, 
            current_season=show['current_season'] + 1,
            current_episode=1,
            status='watching'
        )
    return RedirectResponse(url="/", status_code=303)

# Fetch poster for existing show
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

# Bulk fetch posters for all shows
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

# ============ TRAKT INTEGRATION ============

async def search_trakt(title: str):
    """Search Trakt for a show and return basic info"""
    try:
        async with httpx.AsyncClient() as client:
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
                    "status": show.get("status")  # returning series, ended, etc.
                }
    except Exception as e:
        print(f"Trakt search error: {e}")
    return None

async def get_trakt_show_details(slug: str):
    """Get detailed show info including air day and next episode"""
    try:
        async with httpx.AsyncClient() as client:
            # Get show details
            show_resp = await client.get(
                f"{TRAKT_BASE_URL}/shows/{slug}",
                params={"extended": "full"},
                headers=trakt_headers()
            )
            show_data = show_resp.json()
            
            # Get next episode
            next_ep_resp = await client.get(
                f"{TRAKT_BASE_URL}/shows/{slug}/next_episode",
                params={"extended": "full"},
                headers=trakt_headers()
            )
            next_ep = next_ep_resp.json() if next_ep_resp.status_code == 200 else None
            
            # Parse air day from airs info
            airs = show_data.get("airs", {})
            air_day = airs.get("day")  # e.g., "Thursday"
            air_time = airs.get("time")  # e.g., "21:00"
            
            return {
                "title": show_data.get("title"),
                "year": show_data.get("year"),
                "status": show_data.get("status"),
                "network": show_data.get("network"),
                "air_day": air_day,
                "air_time": air_time,
                "runtime": show_data.get("runtime"),
                "genres": show_data.get("genres", []),
                "overview": show_data.get("overview"),
                "rating": show_data.get("rating"),
                "trakt_slug": slug,
                "next_episode": {
                    "season": next_ep.get("season"),
                    "episode": next_ep.get("number"),
                    "title": next_ep.get("title"),
                    "air_date": next_ep.get("first_aired")
                } if next_ep else None
            }
    except Exception as e:
        print(f"Trakt show details error: {e}")
    return None

@app.get("/api/trakt/search")
async def api_trakt_search(q: str):
    """Search for a show on Trakt"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TRAKT_BASE_URL}/search/show",
                params={"query": q, "limit": 10},
                headers=trakt_headers()
            )
            data = resp.json()
            results = []
            for item in data:
                show = item.get("show", {})
                results.append({
                    "title": show.get("title"),
                    "year": show.get("year"),
                    "trakt_slug": show.get("ids", {}).get("slug"),
                    "trakt_id": show.get("ids", {}).get("trakt"),
                    "overview": show.get("overview", "")[:200]
                })
            return {"results": results}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/trakt/show/{slug}")
async def api_trakt_show(slug: str):
    """Get detailed info about a show from Trakt"""
    details = await get_trakt_show_details(slug)
    if details:
        return details
    raise HTTPException(status_code=404, detail="Show not found on Trakt")

@app.post("/api/shows/{show_id}/fetch-trakt")
async def fetch_trakt_for_show(show_id: int):
    """Fetch Trakt data for an existing show and update air day"""
    show = db.get_show(show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")
    
    # Search Trakt
    trakt_info = await search_trakt(show['title'])
    if not trakt_info or not trakt_info.get('trakt_slug'):
        return {"error": "Show not found on Trakt"}
    
    # Get full details
    details = await get_trakt_show_details(trakt_info['trakt_slug'])
    if details:
        # Update show with Trakt data
        update_data = {"trakt_slug": trakt_info['trakt_slug']}
        if details.get('air_day'):
            update_data['air_day'] = details['air_day']
        
        db.update_show(show_id, **update_data)
        return {
            "updated": True,
            "air_day": details.get('air_day'),
            "next_episode": details.get('next_episode'),
            "status": details.get('status')
        }
    
    return {"error": "Could not fetch show details"}

@app.post("/api/fetch-all-trakt")
async def fetch_all_trakt():
    """Fetch Trakt data (air days) for all shows"""
    shows = db.get_all_shows()
    updated = 0
    
    for show in shows:
        if not show.get('air_day'):  # Only fetch if no air day set
            trakt_info = await search_trakt(show['title'])
            if trakt_info and trakt_info.get('trakt_slug'):
                details = await get_trakt_show_details(trakt_info['trakt_slug'])
                if details and details.get('air_day'):
                    db.update_show(show['id'], 
                        trakt_slug=trakt_info['trakt_slug'],
                        air_day=details['air_day']
                    )
                    updated += 1
    
    return {"updated": updated}

@app.get("/api/recommendations")
async def get_recommendations():
    """Get show recommendations based on current shows, with posters"""
    shows = db.get_all_shows()
    dismissed = db.get_dismissed_recommendations()
    
    if not shows:
        return {"recommendations": [], "message": "Add some shows first!"}
    
    # Get genres from current shows via Trakt
    all_genres = []
    for show in shows[:5]:  # Check first 5 shows
        trakt_info = await search_trakt(show['title'])
        if trakt_info and trakt_info.get('trakt_slug'):
            details = await get_trakt_show_details(trakt_info['trakt_slug'])
            if details:
                all_genres.extend(details.get('genres', []))
    
    # Get most common genres
    from collections import Counter
    top_genres = [g for g, _ in Counter(all_genres).most_common(3)]
    
    if not top_genres:
        top_genres = ['drama', 'thriller']  # Fallback
    
    # Search for recommended shows
    recommendations = []
    try:
        async with httpx.AsyncClient() as client:
            # Get popular shows in those genres
            resp = await client.get(
                f"{TRAKT_BASE_URL}/shows/popular",
                params={"genres": ",".join(top_genres), "limit": 20},
                headers=trakt_headers()
            )
            popular = resp.json()
            
            # Filter out shows Ken already has and dismissed ones
            current_titles = {s['title'].lower() for s in shows}
            dismissed_slugs = {d['trakt_slug'] for d in dismissed}
            
            for show in popular:
                slug = show.get("ids", {}).get("slug")
                title = show.get('title', '')
                
                if title.lower() not in current_titles and slug not in dismissed_slugs:
                    # Fetch poster from TMDB
                    _, poster_url = await search_tmdb(title)
                    
                    recommendations.append({
                        "title": title,
                        "year": show.get("year"),
                        "trakt_slug": slug,
                        "overview": show.get("overview", "")[:150] if show.get("overview") else "",
                        "poster_url": poster_url
                    })
                    if len(recommendations) >= 6:
                        break
    except Exception as e:
        print(f"Recommendations error: {e}")
    
    return {
        "based_on_genres": top_genres,
        "recommendations": recommendations
    }

@app.post("/api/recommendations/dismiss")
async def dismiss_recommendation(trakt_slug: str = Form(...)):
    """Dismiss a recommendation so it doesn't show again"""
    db.dismiss_recommendation(trakt_slug)
    return {"dismissed": True}

@app.post("/api/recommendations/add")
async def add_recommendation(
    title: str = Form(...),
    trakt_slug: str = Form(...),
    service: str = Form("Other"),
    priority: int = Form(2)
):
    """Add a recommended show to the watchlist"""
    # Fetch poster
    _, poster_url = await search_tmdb(title)
    
    # Get air day from Trakt
    air_day = None
    details = await get_trakt_show_details(trakt_slug)
    if details:
        air_day = details.get('air_day')
    
    show_id = db.add_show(
        title=title,
        service=service,
        current_season=1,
        current_episode=1,
        air_day=air_day,
        priority=priority,
        status='watching',
        poster_url=poster_url
    )
    
    # Also dismiss it from recommendations
    db.dismiss_recommendation(trakt_slug)
    
    return RedirectResponse(url="/", status_code=303)
