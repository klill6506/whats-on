import asyncio
import json
import math
import os
import random
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from pathlib import Path
from dotenv import load_dotenv

# Load .env (next to this file) before importing modules that read env vars at import
# time (database.py). Explicit path so it works regardless of cwd. override=True so the
# local .env wins over a stale/empty shell var of the same name. On Render there's no
# .env file, so this is a harmless no-op and Render's injected env vars are untouched.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

import anthropic
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


# --- AI taste tagger (Anthropic) ---

# Default to the most capable model; override with ANTHROPIC_MODEL (e.g. claude-haiku-4-5
# to save cost). Model IDs are current as of the claude-api skill cache (2026-05).
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

_anthropic_client = None

def get_anthropic() -> anthropic.AsyncAnthropic:
    """Lazily build the async Anthropic client. Raises if the key is missing so the
    failure is obvious rather than a 500 deep in a request."""
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env (local) or the Render "
                "dashboard (production) before tagging or generating reviews."
            )
        # Some environments export an empty ANTHROPIC_AUTH_TOKEN, which the SDK would
        # turn into an illegal 'Bearer ' header (preferred over x-api-key). Drop it so
        # the explicit api_key is used.
        if not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        # max_retries above the default 2 — a full cache refresh fires dozens of calls and
        # transient 529 (overloaded) responses are common in a batch.
        _anthropic_client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=5)
    return _anthropic_client


# JSON schema for structured output. Numeric min/max aren't supported by structured
# outputs, so the 0-5 bound is enforced in code via db.clamp_tags().
_TAG_SCHEMA = {
    "type": "object",
    "properties": {d: {"type": "integer"} for d in db.TAG_DIMENSIONS},
    "required": db.TAG_DIMENSIONS,
    "additionalProperties": False,
}

_TAG_SYSTEM = """You are a TV taste analyst. Rate a show on 11 descriptive dimensions, each an integer 0-5.

These describe HOW MUCH of a trait the show has — they are NOT good/bad judgments:
- crime: criminal activity, investigations, the underworld
- legal: courtroom, lawyers, the justice system
- comedy: how funny / light it is
- darkness: bleak, grim, morally heavy tone
- prestige: elevated "prestige TV" craft and ambition
- pace: how propulsive it is (0 = slow burn, 5 = breakneck)
- sentimentality: emotional, heart-tugging, earnest feeling
- mystery: puzzles, whodunits, unanswered questions driving the plot
- ending_quality: how satisfying and well-constructed its episode/season endings are
- violence: on-screen violence and intensity
- rewatchability: how much it rewards repeat viewing

0 = none of this trait, 5 = saturated with it. Return only the JSON object with all 11 integer fields."""


async def ai_tag_show(title, overview, genres) -> dict:
    """Ask Claude to score a show on the 11 taste dimensions. Returns a validated,
    clamped dict of dimension -> int(0-5)."""
    client = get_anthropic()
    genre_str = ", ".join(genres) if genres else "unknown"
    user = f"Title: {title}\nGenres: {genre_str}\nOverview: {overview or 'No overview available.'}"
    resp = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=400,
        system=_TAG_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _TAG_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return db.clamp_tags(json.loads(text))


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

def _pick_trakt_match(results, title):
    """Choose the best show from Trakt search results. Prefer an exact (case-insensitive)
    title match, keeping Trakt's own ordering (results come back ranked by relevance/
    popularity, so the well-known show wins). Fall back to Trakt's top result if no exact
    match. Avoids the old data[0] behavior that matched e.g. 'Landman' -> 'man-land'."""
    shows = [r["show"] for r in (results or []) if r.get("show")]
    if not shows:
        return None
    q = title.strip().lower()
    for s in shows:
        if (s.get("title") or "").strip().lower() == q:
            return s
    return shows[0]

async def search_trakt(title: str, client: httpx.AsyncClient = None):
    close = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.get(
            f"{TRAKT_BASE_URL}/search/show",
            params={"query": title, "limit": 10},
            headers=trakt_headers()
        )
        show = _pick_trakt_match(resp.json(), title)
        if show:
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

# --- Phase 3: signed taste profile ---

def compute_profile(rated):
    """Pure: given [(rating, tags_dict), ...] build the signed taste vector.
    weight = rating - 3; profile[dim] = sum(weight * tag[dim]) / sum(|weight|).
    Returns {} when there's no usable signal (denominator 0)."""
    num = {d: 0.0 for d in db.TAG_DIMENSIONS}
    denom = 0.0
    for rating, tags in rated:
        if rating is None or tags is None:
            continue
        w = rating - 3
        denom += abs(w)
        for d in db.TAG_DIMENSIONS:
            num[d] += w * (tags.get(d) or 0)
    if denom == 0:
        return {}
    return {d: num[d] / denom for d in db.TAG_DIMENSIONS}


def build_taste_profile():
    """Signed taste profile from rated library shows that have tags."""
    tags_by_slug = {t['trakt_slug']: t for t in db.get_all_tags()}
    rated = []
    for show in db.get_all_shows():
        slug, rating = show.get('trakt_slug'), show.get('rating')
        if not slug or rating is None:
            continue
        tags = tags_by_slug.get(slug)
        if tags:
            rated.append((rating, tags))
    return compute_profile(rated)


def cosine_similarity(a, b):
    """Cosine similarity over the taste dimensions, in [-1, 1]. 0 if either vector is zero."""
    dims = db.TAG_DIMENSIONS
    dot = sum((a.get(d) or 0) * (b.get(d) or 0) for d in dims)
    na = math.sqrt(sum((a.get(d) or 0) ** 2 for d in dims))
    nb = math.sqrt(sum((b.get(d) or 0) ** 2 for d in dims))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def score_candidate(candidate_tags, profile):
    """Map cosine similarity [-1, 1] to a 0-100 match score. 50 = orthogonal/neutral."""
    cos = cosine_similarity(candidate_tags, profile)
    return round((cos + 1) / 2 * 100)


# --- Phase 5: explanations (math) + AI review (Henry voice) ---

DIM_LABELS = {
    'crime': 'crime', 'legal': 'legal/courtroom', 'comedy': 'comedy',
    'darkness': 'a dark tone', 'prestige': 'prestige polish', 'pace': 'a fast pace',
    'sentimentality': 'sentimentality', 'mystery': 'mystery',
    'ending_quality': 'strong endings', 'violence': 'violence',
    'rewatchability': 'rewatchability',
}


def _join_labels(dims_list):
    labels = [DIM_LABELS.get(d, d) for d in dims_list]
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + ", and " + labels[-1]


def explain_match(candidate_tags, profile):
    """Deterministic, math-derived (why_recommended, why_might_fail) from the candidate's
    tag vector vs the signed taste profile. No AI — cheap and trustworthy."""
    if not profile:
        return ("", "")
    dims = db.TAG_DIMENSIONS
    c = {d: candidate_tags.get(d) or 0 for d in dims}
    p = {d: profile.get(d) or 0 for d in dims}

    # Recommend: candidate delivers a trait Ken likes (profile positive, candidate high).
    liked = sorted([d for d in dims if p[d] > 0.3 and c[d] >= 3],
                   key=lambda d: p[d] * c[d], reverse=True)[:3]
    why = f"High on {_join_labels(liked)}." if liked else "A change of pace from your usual."

    # Might fail: (a) leans into a disliked trait; (b) light on a strongly-liked trait.
    disliked = sorted([d for d in dims if p[d] < -0.3 and c[d] >= 3],
                      key=lambda d: p[d] * c[d])[:2]
    missing = sorted([d for d in dims if p[d] > 1.0 and c[d] <= 2],
                     key=lambda d: p[d], reverse=True)[:2]
    parts = []
    if disliked:
        parts.append(f"leans into {_join_labels(disliked)}")
    if missing:
        parts.append(f"lighter on {_join_labels(missing)} than your usual")
    if parts:
        s = "; ".join(parts)
        why_not = s[0].upper() + s[1:] + "."
    else:
        why_not = "Nothing obvious working against it."
    return (why, why_not)


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string"},
        "risk": {"type": "string"},
        "watch_plan": {"type": "string"},
    },
    "required": ["verdict", "risk", "watch_plan"],
    "additionalProperties": False,
}

_REVIEW_SYSTEM = """You are Henry, the wry mascot of Ken's personal TV tracker. Voice: dry,
classy, a little snarky, never cruel, economical. You're given a show, how strongly it
matches Ken's taste (0-100), and its trait scores (0-5). Write three short lines:
- verdict: one sentence on whether it's his lane and why (blunt is fine).
- risk: one sentence naming the single most likely reason he bails.
- watch_plan: one sentence of concrete advice (e.g. "Give it two episodes; bail with honor if you're not in.").
Each line under ~25 words. No emoji, no preamble. Return only the JSON."""


async def ai_review(title, overview, match_score, tags):
    """One Henry-voice call -> {verdict, risk, watch_plan}. Cached by the caller; never
    invoked on page load."""
    client = get_anthropic()
    trait_str = ", ".join(f"{d} {tags.get(d, 0)}" for d in db.TAG_DIMENSIONS)
    user = (f"Show: {title}\nMatch score: {match_score}/100\nTraits (0-5): {trait_str}\n"
            f"Overview: {overview or 'No overview available.'}")
    resp = await client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=300,
        system=_REVIEW_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _REVIEW_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    return {
        "verdict": (data.get("verdict") or "").strip(),
        "risk": (data.get("risk") or "").strip(),
        "watch_plan": (data.get("watch_plan") or "").strip(),
    }


# --- Phase 4: cosine-based cache refresh ---

async def refresh_recommendation_cache(client: httpx.AsyncClient):
    """Rebuild the recommendation cache by scoring Trakt 'related' candidates against
    Ken's signed taste profile via cosine similarity. Deterministic: no random factor and
    no popularity bonus baked into the score (Trakt community rating is a tiebreaker only)."""
    shows = db.get_all_shows()
    if not shows:
        return

    profile = build_taste_profile()

    existing_titles = {s['title'].lower() for s in shows}
    excluded = {s.get('trakt_slug') for s in shows if s.get('trakt_slug')} | db.get_dismissed_slugs()

    # Candidate discovery: Trakt "related" for the top-rated library shows.
    rated_with_slug = [s for s in shows if s.get('trakt_slug') and s.get('rating')]
    top_shows = sorted(rated_with_slug, key=lambda s: s['rating'], reverse=True)[:5]

    candidates = {}  # slug -> rec dict from Trakt
    for show in top_shows:
        related = await fetch_trakt_related(show['trakt_slug'], client, limit=15)
        for rec in related:
            rec_slug = rec.get('ids', {}).get('slug')
            if not rec_slug or rec_slug in excluded or rec.get('title', '').lower() in existing_titles:
                continue
            candidates.setdefault(rec_slug, rec)

    # Score each candidate by cosine similarity to the profile; tag on the fly if missing.
    scored = []
    for slug, rec in candidates.items():
        tags = db.get_tags(slug)
        if not tags:
            try:
                t = await ai_tag_show(rec.get('title', ''), rec.get('overview', ''), rec.get('genres', []))
                db.upsert_tags(slug, source='ai', **t)
                tags = db.get_tags(slug)
            except Exception as e:
                print(f"On-the-fly tag failed for {slug}: {e}")
                continue
        match_score = score_candidate(tags, profile) if profile else 50
        scored.append((match_score, rec, tags))

    # Deterministic ordering: score desc, Trakt rating as a tiny tiebreaker, then slug.
    scored.sort(
        key=lambda x: (x[0], x[1].get('rating') or 0, x[1].get('ids', {}).get('slug', '')),
        reverse=True,
    )
    top = scored[:40]

    # Reuse cached Henry-voice reviews when the score barely moved (avoid re-calling the
    # API for every candidate on every refresh). Keyed by slug.
    old_reviews = {r['trakt_slug']: r for r in db.get_all_cached_recommendations()}

    new_cache = []
    for match_score, rec, tags in top:
        slug = rec.get('ids', {}).get('slug', '')
        title = rec.get('title', '')
        tmdb_id, poster_url = await search_tmdb(title, client)
        services = await get_tmdb_providers(tmdb_id, client) if tmdb_id else []
        overview = rec.get('overview') or ''
        if len(overview) > 300:
            overview = overview[:300] + '...'

        # Math-based explanations (deterministic, no API).
        why_rec, why_fail = explain_match(tags, profile)

        # Henry-voice verdict/risk/plan: reuse if cached and score is within 5; else generate.
        prev = old_reviews.get(slug)
        if prev and prev.get('verdict') and abs((prev.get('match_score') or -999) - match_score) <= 5:
            verdict, risk, watch_plan = prev['verdict'], prev.get('risk'), prev.get('watch_plan')
        else:
            try:
                rv = await ai_review(title, overview, match_score, tags)
                verdict, risk, watch_plan = rv['verdict'], rv['risk'], rv['watch_plan']
            except Exception as e:
                print(f"Review generation failed for {slug}: {e}")
                verdict = risk = watch_plan = None

        new_cache.append({
            'trakt_slug': slug,
            'title': title,
            'year': rec.get('year'),
            'overview': overview,
            'poster_url': poster_url,
            'score': match_score,
            'source_show': '',  # superseded by the math-based "why" below
            'genres': ",".join(rec.get('genres', [])),
            'streaming_services': ",".join(services),
            'match_score': match_score,
            'verdict': verdict,
            'risk': risk,
            'watch_plan': watch_plan,
            'why_recommended': why_rec,
            'why_might_fail': why_fail,
        })

    if new_cache:
        db.clear_recommendation_cache()
        for item in new_cache:
            db.insert_cached_recommendation(**item)

    print(f"Recommendation cache refreshed: {len(new_cache)} candidates "
          f"(profile dims set: {len(profile)})")


# ============ PAGES ============

_refresh_in_progress = False


async def _background_refresh(client):
    """Run a cache refresh without blocking the request that triggered it. Guarded so
    concurrent page loads don't fire overlapping refreshes (a refresh makes dozens of
    API calls)."""
    global _refresh_in_progress
    if _refresh_in_progress:
        return
    _refresh_in_progress = True
    try:
        await refresh_recommendation_cache(client)
    except Exception as e:
        print(f"Background refresh failed: {e}")
    finally:
        _refresh_in_progress = False


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
        # If the cache is stale, refresh it in the BACKGROUND so the page never blocks on
        # the recommendation engine's API calls (tagging + reviews). This request serves
        # whatever is currently cached; the next load picks up the fresh data.
        cache_age = db.get_cache_age()
        if (not cache_age or (datetime.now() - cache_age) > CACHE_MAX_AGE) and not _refresh_in_progress:
            asyncio.create_task(_background_refresh(_client(request)))

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


@app.post("/api/admin/retag")
async def admin_retag(request: Request):
    """Tag every library show + cached candidate that's missing taste tags.
    Idempotent — slugs already in show_tags are skipped. Run once locally to seed
    the existing library (pennies). Requires ANTHROPIC_API_KEY."""
    client = _client(request)
    tagged, skipped, failed = 0, 0, 0
    results = []

    # --- Library shows ---
    for show in db.get_all_shows():
        slug = show.get('trakt_slug')
        if not slug:
            info = await search_trakt(show['title'], client)
            if info and info.get('trakt_slug'):
                slug = info['trakt_slug']
                db.update_show(show['id'], trakt_slug=slug)
        if not slug:
            failed += 1
            results.append({"title": show['title'], "status": "no_trakt_slug"})
            continue
        if db.get_tags(slug):
            skipped += 1
            continue
        details = await get_trakt_show_details(slug, client)
        overview = details.get('overview') if details else None
        genres = details.get('genres', []) if details else []
        try:
            tags = await ai_tag_show(show['title'], overview, genres)
            db.upsert_tags(slug, source='ai', **tags)
            tagged += 1
            results.append({"title": show['title'], "slug": slug, **tags})
        except Exception as e:
            print(f"Tagging failed for {show['title']}: {e}")
            failed += 1
            results.append({"title": show['title'], "status": f"error: {e}"})

    # --- Cached recommendation candidates ---
    for rec in db.get_all_cached_recommendations():
        slug = rec.get('trakt_slug')
        if not slug or db.get_tags(slug):
            skipped += 1
            continue
        genres = [g.strip() for g in (rec.get('genres') or '').split(',') if g.strip()]
        try:
            tags = await ai_tag_show(rec['title'], rec.get('overview'), genres)
            db.upsert_tags(slug, source='ai', **tags)
            tagged += 1
        except Exception as e:
            print(f"Tagging failed for candidate {rec.get('title')}: {e}")
            failed += 1

    return {"tagged": tagged, "skipped": skipped, "failed": failed, "shows": results}


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
