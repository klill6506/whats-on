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
