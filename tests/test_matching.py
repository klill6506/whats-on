"""Tests for _pick_trakt_match — the Trakt search disambiguation that prevents
e.g. 'Landman' resolving to 'man-land'."""
# conftest.py adds the project root to sys.path.
import main


def _r(title, slug, year=None):
    return {"show": {"title": title, "year": year, "ids": {"slug": slug}}}


def test_prefers_exact_title_over_first_result():
    results = [
        _r("Man-Land", "man-land"),       # Trakt's noisy top hit
        _r("Landman", "landman", 2024),   # the real match
    ]
    assert main._pick_trakt_match(results, "Landman")["ids"]["slug"] == "landman"


def test_exact_match_is_case_insensitive():
    results = [_r("LAW & ORDER", "law-order", 1990)]
    assert main._pick_trakt_match(results, "law & order")["ids"]["slug"] == "law-order"


def test_keeps_trakt_order_among_exact_matches():
    # Two shows both titled "Trying"; Trakt ranks the popular one first — keep that.
    results = [
        _r("Trying", "trying", 2020),
        _r("Trying", "trying-2023", 2023),
    ]
    assert main._pick_trakt_match(results, "Trying")["ids"]["slug"] == "trying"


def test_falls_back_to_first_when_no_exact():
    results = [_r("Something Else", "something-else")]
    assert main._pick_trakt_match(results, "Nonexistent")["ids"]["slug"] == "something-else"


def test_empty_results_returns_none():
    assert main._pick_trakt_match([], "Whatever") is None
    assert main._pick_trakt_match(None, "Whatever") is None
