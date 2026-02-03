# What's On? 📺

A simple, mobile-first TV show tracker. No automatic syncing, no clutter from shared accounts — just your shows.

## Features

- 🟢 **Ready to Watch** — Shows you're current on with new episodes
- ⏳ **Catching Up** — Shows you're behind on
- 📱 **Mobile-first** — Designed for phone/iPad
- 🦉 **Henry Integration** — Tell Henry what you watched, he updates the tracker

## Tech Stack

- FastAPI + Jinja2
- SQLite
- Tailwind CSS (CDN)
- Deployed on Render

## API Endpoints

- `GET /` — Main dashboard
- `GET /api/shows` — List all shows
- `POST /api/shows` — Add a show
- `PUT /api/shows/{id}` — Update a show
- `DELETE /api/shows/{id}` — Delete a show
- `POST /api/shows/{id}/caught-up` — Mark show as caught up

## Local Development

```bash
pip install -r requirements.txt
python main.py
```

Then open http://localhost:8005
