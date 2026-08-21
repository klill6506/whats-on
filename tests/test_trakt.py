"""Trakt response handling — a revoked client ID answers 403 on every endpoint."""
import httpx
import main


def _resp(status, body=b"Forbidden"):
    return httpx.Response(status, content=body, request=httpx.Request("GET", "https://api.trakt.tv/x"))


def test_403_returns_none_and_records_error():
    assert main._trakt_json(_resp(403), "search") is None
    assert "403" in main.TRAKT_LAST_ERROR


def test_ok_parses_and_clears_error():
    main.TRAKT_LAST_ERROR = "stale"
    assert main._trakt_json(_resp(200, b'[{"show": {"title": "The Pitt"}}]'), "search") == [{"show": {"title": "The Pitt"}}]
    assert main.TRAKT_LAST_ERROR is None


def test_pick_match_tolerates_none():
    assert main._pick_trakt_match(None, "Landman") is None
