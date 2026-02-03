from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import database as db

app = FastAPI(title="What's On?")

# Templates
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Seed Ken's shows on startup
@app.on_event("startup")
async def startup():
    db.seed_kens_shows()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    shows = db.get_all_shows()
    
    # Categorize shows
    tonight = []  # Current shows with new episodes / ready to watch
    catching_up = []  # Shows we're behind on
    
    day_order = {
        "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
        "Friday": 4, "Saturday": 5, "Sunday": 6, "Weekend": 6, None: 99
    }
    
    for show in shows:
        if show['status'] == 'current' or show['current_episode'] == 99:
            tonight.append(show)
        else:
            catching_up.append(show)
    
    # Sort tonight by air day
    tonight.sort(key=lambda x: day_order.get(x.get('air_day'), 99))
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "tonight": tonight,
        "catching_up": catching_up
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
    show_id = db.add_show(
        title=title,
        service=service,
        current_season=current_season,
        current_episode=current_episode,
        air_day=air_day if air_day else None,
        priority=priority,
        notes=notes if notes else None,
        status=status
    )
    return {"id": show_id, "message": "Show added"}

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
async def api_caught_up(show_id: int):
    db.update_show(show_id, current_episode=99, status="current")
    return {"message": "Marked as caught up"}

# Simple form handlers for mobile
@app.post("/mark-caught-up/{show_id}")
async def mark_caught_up(show_id: int):
    db.update_show(show_id, current_episode=99, status="current")
    return RedirectResponse(url="/", status_code=303)

@app.post("/next-episode/{show_id}")
async def next_episode(show_id: int):
    show = db.get_show(show_id)
    if show:
        new_ep = show['current_episode'] + 1
        db.update_show(show_id, current_episode=new_ep)
    return RedirectResponse(url="/", status_code=303)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
