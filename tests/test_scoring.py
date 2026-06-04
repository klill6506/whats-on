"""Tests for the Phase 3 signed profile and Phase 4 cosine scoring — the deterministic
math the recommender now rests on."""
# conftest.py adds the project root to sys.path and isolates the DB.
import main

DIMS = main.db.TAG_DIMENSIONS


def _tags(**overrides):
    """A tag dict that's 0 on every dimension except the ones passed in."""
    t = {d: 0 for d in DIMS}
    t.update(overrides)
    return t


def test_compute_profile_signs_by_rating():
    # rating 5 -> weight +2 (liked, crime show); rating 1 -> weight -2 (disliked, sentimental)
    rated = [
        (5, _tags(crime=5)),
        (1, _tags(sentimentality=5)),
    ]
    profile = main.compute_profile(rated)
    # denom = |+2| + |-2| = 4
    assert profile['crime'] == 2.5            # (2*5)/4
    assert profile['sentimentality'] == -2.5  # (-2*5)/4
    assert profile['comedy'] == 0.0


def test_compute_profile_rating_three_is_neutral():
    # A single rating-3 show contributes zero weight -> no usable signal -> {}
    assert main.compute_profile([(3, _tags(crime=5))]) == {}


def test_compute_profile_empty():
    assert main.compute_profile([]) == {}


def test_compute_profile_skips_none():
    rated = [(None, _tags(crime=5)), (5, None), (4, _tags(comedy=5))]
    profile = main.compute_profile(rated)
    assert profile['comedy'] == 5.0  # only the (4, comedy=5) row counts: (1*5)/1


def test_score_is_0_to_100_and_neutral_at_orthogonal():
    profile = {**{d: 0 for d in DIMS}, 'crime': 2.0}
    # Candidate orthogonal to the profile (no crime) -> cosine 0 -> 50
    assert main.score_candidate(_tags(comedy=5), profile) == 50
    # Candidate aligned with the profile -> high score
    assert main.score_candidate(_tags(crime=5), profile) > 80
    # Candidate anti-aligned (profile crime positive, candidate... can't be negative;
    # use a profile with a negative axis below)


def test_disliked_axis_pushes_candidate_away():
    """The core property: a sentimentality-heavy candidate must score lower when the
    profile weights sentimentality negatively than when it weights it positively."""
    candidate = _tags(sentimentality=5)
    profile_likes_sent = {**{d: 0 for d in DIMS}, 'sentimentality': 2.0}
    profile_hates_sent = {**{d: 0 for d in DIMS}, 'sentimentality': -2.0}

    liked = main.score_candidate(candidate, profile_likes_sent)
    disliked = main.score_candidate(candidate, profile_hates_sent)

    assert liked > disliked
    assert liked > 50 > disliked  # crosses the neutral line


def test_empty_profile_safe():
    # cosine against an all-zero / empty profile is 0 -> neutral 50, never a crash
    assert main.score_candidate(_tags(crime=5), {}) == 50
