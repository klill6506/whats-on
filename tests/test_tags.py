"""Tests for the taste-tag storage layer (Phase 1) and clamp validation (Phase 2).

The AI tagger itself (ai_tag_show) calls the Anthropic API and is not unit-tested
here; what's deterministic and worth locking down is the clamp logic and the
SQLite upsert/get roundtrip.
"""
# conftest.py points DATABASE_PATH at a throwaway SQLite file before this imports.
import database as db


def test_clamp_tags_bounds_and_fills():
    out = db.clamp_tags({'crime': 9, 'comedy': -3, 'pace': '4'})
    assert out['crime'] == 5          # clamped down to max
    assert out['comedy'] == 0         # clamped up to min
    assert out['pace'] == 4           # numeric string coerced
    assert out['legal'] == 0          # missing dimension defaults to 0
    assert set(out.keys()) == set(db.TAG_DIMENSIONS)


def test_clamp_tags_handles_garbage():
    out = db.clamp_tags({'crime': None, 'comedy': 'lots'})
    assert out['crime'] == 0
    assert out['comedy'] == 0


def test_upsert_and_get_roundtrip():
    tags = {d: i % 6 for i, d in enumerate(db.TAG_DIMENSIONS)}
    db.upsert_tags('roundtrip-show', source='ai', **tags)
    row = db.get_tags('roundtrip-show')
    assert row is not None
    for d in db.TAG_DIMENSIONS:
        assert row[d] == tags[d]
    assert row['source'] == 'ai'


def test_upsert_is_idempotent_overwrite():
    db.upsert_tags('dupe-show', source='ai', **{d: 1 for d in db.TAG_DIMENSIONS})
    db.upsert_tags('dupe-show', source='human', **{d: 4 for d in db.TAG_DIMENSIONS})
    row = db.get_tags('dupe-show')
    assert row['crime'] == 4
    assert row['source'] == 'human'
    matches = [r for r in db.get_all_tags() if r['trakt_slug'] == 'dupe-show']
    assert len(matches) == 1


def test_upsert_clamps_on_write():
    db.upsert_tags('clamp-show', source='ai', **{d: 99 for d in db.TAG_DIMENSIONS})
    row = db.get_tags('clamp-show')
    assert row['violence'] == 5


def test_get_tags_missing_returns_none():
    assert db.get_tags('does-not-exist') is None
