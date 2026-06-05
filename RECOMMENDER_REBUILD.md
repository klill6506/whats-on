# What's On — Recommender Rebuild Brief

For: Claude Code, working locally in the `whats-on` repo → push → Render.
Goal: Replace the genre-based recommender with a taste-dimension model. Deterministic
match score (math), AI for tagging + reviews.

> **Execution rule:** Do this phase by phase, stopping for review. Phase 1+2, then run
> `/api/admin/retag` locally and show the actual tag numbers for a few shows before going
> further. Then Phase 3+4 (check ranking is stable and disliked shows push results away).
> Then Phase 5+6. Test locally, then commit and push to let Render redeploy.

## Current state (do NOT rebuild these — reuse them)

- Stack: FastAPI + Jinja2, dual SQLite (local) / Postgres (Render) via `database.py`.
- DB helpers: `get_db()`, `init_db()`, `_ph(n)` placeholder helper, `ALLOWED_FIELDS`
  whitelist. Any new table must be added to both the Postgres and SQLite branches of `init_db()`.
- External APIs (keep): Trakt — `search_trakt`, `get_trakt_show_details`, `fetch_trakt_related`.
  TMDB — `search_tmdb`, `get_tmdb_providers`.
- Existing tables (keep): `shows` (has `rating` 1–5, `trakt_slug`, `service`, `status`),
  `watch_history`, `dismissed_recommendations`, `recommendation_cache`, `meta`.
- The function to replace: `refresh_recommendation_cache(client)` in `main.py`.
- `USER_SERVICES` stays hardcoded — single-user is fine for V1.

## Why the current engine underperforms (the problems to fix)

1. Taste model is Trakt genres only → too coarse; everything is "crime/drama."
2. Dislikes don't subtract — `genre_weights[genre] += rating`, unrated defaults to 2. No negative signal.
3. `random.random() * 2` is injected into the score → rankings are partly noise.
4. Candidates come only from Trakt "related" + a community-rating popularity bonus → fights personalization.

## The 11 taste dimensions (0–5 each)

`crime, legal, comedy, darkness, prestige, pace, sentimentality, mystery, ending_quality, violence, rewatchability`

Note on polarity: these are descriptive (how much of this trait the show has), not "good/bad."
Direction is learned from Ken's ratings in the profile step, so e.g. high `sentimentality`
will end up negatively weighted for him without hardcoding that.

## Phase 1 — Taste-tag storage

Add a `show_tags` table keyed by `trakt_slug` (so both library shows and recommendation
candidates can be tagged once and reused):

```
show_tags(
  trakt_slug TEXT PRIMARY KEY,
  crime INT, legal INT, comedy INT, darkness INT, prestige INT,
  pace INT, sentimentality INT, mystery INT, ending_quality INT,
  violence INT, rewatchability INT,
  source TEXT,          -- 'ai' | 'human' | 'ai+edited'
  tagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Add to both `init_db()` branches. Add `get_tags(slug)`, `upsert_tags(slug, **dims, source)`,
`get_all_tags()` to `database.py`.

## Phase 2 — AI tagger (server-side)

- New env var: `ANTHROPIC_API_KEY` (set locally + on Render). Use the official `anthropic`
  Python SDK; pull the current model id from the docs — don't hardcode a stale one.
- `async def ai_tag_show(title, overview, genres) -> dict`: prompt Claude to return JSON only
  with the 11 integer scores (0–5). Validate/clamp before storing.
- Admin endpoint `POST /api/admin/retag`: for every library show + every cached candidate
  missing tags, fetch overview (already have it from Trakt/TMDB), call `ai_tag_show`,
  `upsert_tags(..., source='ai')`. Idempotent — skip slugs already tagged.
- Run this locally once to tag the existing ~11 shows (pennies). Tags live in the DB;
  consider committing a `seed_tags.json` so a fresh Render DB isn't empty.
- Keep a simple admin UI later to hand-edit a row (`source='ai+edited'`). Human edits are the moat.

## Phase 3 — Signed taste profile

`def build_taste_profile() -> dict[dim, float]`:

- For each rated show with tags: `weight = rating - 3` (5→+2, 4→+1, 3→0, 2→−1, 1→−2).
- For each dimension: `profile[dim] = sum(weight * tag[dim]) / sum(|weight|)` (skip if denominator 0).
- Result is a signed preference vector. Dislikes now actively push away.
- Encourage negative signal: rate a couple of abandoned/disliked shows 1–2 so the vector isn't all-positive.

## Phase 4 — New scoring (replaces genre block + random + popularity bonus)

In the rewritten `refresh_recommendation_cache`:

- Keep candidate discovery via `fetch_trakt_related` (fine as a source), but score every
  candidate by cosine similarity between its tag vector and `build_taste_profile()`, scaled to 0–100.
- Delete the `random.random()` line and the Trakt community-rating bonus (or demote it to a
  tiny tiebreaker only).
- Candidates without tags → tag them on the fly (Phase 2) before scoring.
- Store the 0–100 score in `recommendation_cache.score`.

## Phase 5 — AI review + explanation (cached)

Add columns to `recommendation_cache`: `match_score INT, verdict TEXT, risk TEXT,
watch_plan TEXT, why_recommended TEXT, why_might_fail TEXT`.

- why_recommended / why_might_fail: generated from math, not AI — list the top +/− dimension
  contributions (e.g. "high on crime + legal, low sentimentality" / "slower pace than your
  usual"). Cheap, deterministic, trustworthy.
- verdict / risk / watch_plan: one Claude call in the Henry voice (dry, classy, snarky) given
  the score + tags + overview. Cache the text in the row; only regenerate when the profile
  changes materially or the row is rebuilt. Do NOT call the API on page load.

Target output shape per rec:

```
Match: 91
Verdict: Squarely in your lane — adult, plot-driven, not trying to emotionally blackmail you into calling it art.
Risk:    Starts slower than it needs to.
Plan:    Give it two episodes; bail with honor if you're not in.
```

## Phase 6 — UI (`templates/index.html`)

On each recommendation card, surface: match score (prominent), verdict, the why/why-not
lines, and the existing poster/streaming chips. Keep the current mobile-first look.

## Explicitly OUT of scope for V1

- Auth / multi-user / family profiles → that's the Supabase V2 conversation. Don't let it creep in.
- ML / embeddings — the transparent dimension model is the point; keep it explainable.

## Definition of done

- `random.random()` gone from scoring; ranking is stable across refreshes.
- Disliked shows measurably pull recommendations away from their traits.
- Every rec shows a 0–100 match score + a real "why" + a Henry-voice verdict.
- Tags persist in DB; re-tagging is idempotent; API key is server-side only.
- Builds and deploys clean on Render (both DB branches updated).
