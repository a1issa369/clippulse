from urllib.parse import parse_qs, urlparse

import pytest

from clippulse.twitch import TwitchClient, TwitchError, TwitchRateLimited, normalize_clip


class ScriptedHttp:
    """Plays back canned responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, method, url, headers, form):
        self.requests.append((method, url, dict(headers), form))
        return self.responses.pop(0)


def token(value="tok", expires=3600):
    return (200, {"access_token": value, "expires_in": expires}, {})


def clip_row(i, views=1):
    return {"id": f"c{i}", "url": f"https://clips.twitch.tv/c{i}", "title": "t", "broadcaster_id": "9",
            "view_count": views, "created_at": "2026-11-01T00:00:00Z", "duration": 20.5}


def make(*responses, now=1000.0):
    http = ScriptedHttp(*responses)
    clock = [now]
    return TwitchClient("id", "secret", http=http, clock=lambda: clock[0]), http, clock


def test_token_is_fetched_once_and_reused():
    client, http, _ = make(token(), (200, {"data": []}, {}), (200, {"data": []}, {}))
    client.get_user("a")
    client.get_user("b")
    assert [r[0] for r in http.requests] == ["POST", "GET", "GET"]
    assert http.requests[0][3] == {"client_id": "id", "client_secret": "secret", "grant_type": "client_credentials"}
    assert http.requests[1][2] == {"Authorization": "Bearer tok", "Client-Id": "id"}


def test_token_is_refreshed_shortly_before_it_expires():
    client, http, clock = make(token("one", 3600), (200, {"data": []}, {}),
                               token("two", 3600), (200, {"data": []}, {}))
    client.get_user("a")
    clock[0] += 3600 - 30  # inside the 60 second safety margin
    client.get_user("a")
    assert http.requests[-1][2]["Authorization"] == "Bearer two"


def test_a_401_triggers_one_token_refresh_and_retry():
    client, http, _ = make(token("old"), (401, {}, {}), token("new"),
                           (200, {"data": [{"id": "1", "login": "a", "display_name": "A"}]}, {}))
    assert client.get_user("a")["broadcaster_id"] == "1"
    assert http.requests[-1][2]["Authorization"] == "Bearer new"


def test_two_401s_in_a_row_give_up():
    client, _, _ = make(token(), (401, {}, {}), token(), (401, {}, {}))
    with pytest.raises(TwitchError):
        client.get_user("a")


def test_rate_limit_raises_with_retry_after():
    client, _, _ = make(token(), (429, {}, {"Ratelimit-Reset": "12"}))
    with pytest.raises(TwitchRateLimited) as info:
        client.get_user("a")
    assert info.value.retry_after == 12.0


def test_token_failure_is_reported():
    client, _, _ = make((400, {"message": "invalid client"}, {}))
    with pytest.raises(TwitchError):
        client.get_user("a")


def test_unknown_user_returns_none():
    client, _, _ = make(token(), (200, {"data": []}, {}))
    assert client.get_user("ghost") is None


def test_get_clips_sends_expected_query_and_paginates():
    page1 = (200, {"data": [clip_row(1), clip_row(2)], "pagination": {"cursor": "NEXT"}}, {})
    page2 = (200, {"data": [clip_row(3)], "pagination": {}}, {})
    client, http, _ = make(token(), page1, page2)
    clips = client.get_clips("9", "2026-10-26T00:00:00Z")
    assert [c["id"] for c in clips] == ["c1", "c2", "c3"]
    first = parse_qs(urlparse(http.requests[1][1]).query)
    second = parse_qs(urlparse(http.requests[2][1]).query)
    assert first == {"broadcaster_id": ["9"], "started_at": ["2026-10-26T00:00:00Z"], "first": ["100"]}
    assert second["after"] == ["NEXT"]


def test_get_clips_stops_at_max_items():
    page = (200, {"data": [clip_row(i) for i in range(5)], "pagination": {"cursor": "MORE"}}, {})
    client, http, _ = make(token(), page)
    assert len(client.get_clips("9", "2026-10-26T00:00:00Z", max_items=3)) == 3
    assert len(http.requests) == 2  # token + one page, no second page requested


def test_normalize_clip_coerces_types_and_fills_defaults():
    clip = normalize_clip({"id": "x", "url": "u", "broadcaster_id": "1", "created_at": "t",
                           "view_count": "7", "duration": 5})
    assert clip["view_count"] == 7 and clip["duration"] == 5.0
    assert clip["title"] == "" and clip["vod_offset"] is None
