"""New-season detection (TVmaze-backed)."""
import main


class TestTitleMatching:
    def test_exact_match_wins_over_top_result(self):
        results = [
            {"show": {"id": 1, "name": "Man Land"}},
            {"show": {"id": 2, "name": "Landman"}},
        ]
        assert main._pick_tvmaze_match(results, "Landman")["id"] == 2

    def test_ampersand_and_case_are_ignored(self):
        results = [{"show": {"id": 9, "name": "Your Friends & Neighbors"}}]
        assert main._pick_tvmaze_match(results, "your friends and neighbors")["id"] == 9

    def test_falls_back_to_top_result(self):
        results = [{"show": {"id": 3, "name": "Something Else"}}]
        assert main._pick_tvmaze_match(results, "No Such Show")["id"] == 3

    def test_empty_results(self):
        assert main._pick_tvmaze_match([], "Anything") is None
        assert main._pick_tvmaze_match(None, "Anything") is None


class TestHasNewSeason:
    def test_new_season_available(self):
        assert main.has_new_season({"current_season": 1, "latest_season": 2}) is True

    def test_caught_up_on_latest(self):
        assert main.has_new_season({"current_season": 2, "latest_season": 2}) is False

    def test_unchecked_show_is_not_flagged(self):
        # Columns absent (never checked) must not produce a false "new season" badge.
        assert main.has_new_season({"current_season": 2}) is False
        assert main.has_new_season({"current_season": 2, "latest_season": None}) is False

    def test_user_ahead_of_tvmaze_is_not_flagged(self):
        assert main.has_new_season({"current_season": 5, "latest_season": 4}) is False


class TestWatchlistEntriesAreNotBadged:
    """Unstarted shows sit at S1E1; badging them would mean 'has >1 season'."""

    def test_unstarted_multi_season_show_is_not_badged(self):
        import main
        show = {"current_season": 1, "current_episode": 1, "latest_season": 6}
        assert main.has_new_season(show) is False

    def test_started_show_mid_season_one_is_still_badged(self):
        import main
        show = {"current_season": 1, "current_episode": 4, "latest_season": 2}
        assert main.has_new_season(show) is True

    def test_caught_up_show_with_a_newer_season_is_badged(self):
        import main
        show = {"current_season": 2, "current_episode": 99, "latest_season": 3}
        assert main.has_new_season(show) is True
