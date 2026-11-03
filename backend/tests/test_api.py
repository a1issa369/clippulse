import base64
import json

import pytest

from clippulse.api import App, MAX_BODY_BYTES
from conftest import BOARD
from fakes import ts


@pytest.fixture
def app(service):
    return App(lambda: service)


def call(app, method, path, body=None, query=None, raw_body=None, b64=False):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path}},
             "queryStringParameters": query}
    if raw_body is not None:
        event["body"] = raw_body
    elif body is not None:
        event["body"] = json.dumps(body)
    if b64 and "body" in event:
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True
    return app.handle(event)


def data(response):
    return json.loads(response["body"])


@pytest.fixture
def live(app, twitch):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "c1", 300, ts(2), title="Great one")
    twitch.add_clip(bid, "c2", 50, ts(6))
    return app


def test_health(app):
    r = call(app, "GET", "/api/health")
    assert r["statusCode"] == 200 and data(r) == {"ok": True}
    assert r["headers"]["x-content-type-options"] == "nosniff" and r["headers"]["cache-control"] == "no-store"


def test_unknown_route_404_and_wrong_method_405(app):
    assert call(app, "GET", "/api/nope")["statusCode"] == 404
    assert call(app, "DELETE", "/api/health")["statusCode"] == 405
    assert call(app, "GET", "/")["statusCode"] == 404


def test_track_then_list_then_read_clips(live):
    created = call(live, "POST", "/api/streamers", {"login": "Amy"})
    assert created["statusCode"] == 201 and data(created)["clips_ingested"] == 2
    again = call(live, "POST", "/api/streamers", {"login": "amy"})
    assert again["statusCode"] == 200 and data(again)["created"] is False
    assert [s["login"] for s in data(call(live, "GET", "/api/streamers"))["streamers"]] == ["amy"]
    clips = data(call(live, "GET", "/api/streamers/amy/clips", query={"sort": "views", "days": "7", "limit": "1"}))
    assert [c["id"] for c in clips["clips"]] == ["c1"] and clips["clips"][0]["velocity"] > 0


def test_clips_query_defaults_and_errors(live):
    call(live, "POST", "/api/streamers", {"login": "amy"})
    assert call(live, "GET", "/api/streamers/amy/clips")["statusCode"] == 200
    bad = call(live, "GET", "/api/streamers/amy/clips", query={"sort": "nonsense"})
    assert bad["statusCode"] == 400 and data(bad)["error"]["code"] == "invalid_input"
    assert call(live, "GET", "/api/streamers/ghost/clips")["statusCode"] == 404
    assert call(live, "GET", "/api/streamers/bad%20name/clips")["statusCode"] == 400


def test_rising_endpoint(live):
    call(live, "POST", "/api/streamers", {"login": "amy"})
    body = data(call(live, "GET", "/api/rising", query={"limit": "1"}))
    assert len(body["clips"]) == 1 and body["clips"][0]["id"] == "c1"


@pytest.mark.parametrize("kwargs", [
    {"raw_body": "not json"}, {"raw_body": "[1,2]"}, {"raw_body": '"str"'}, {},
    {"raw_body": json.dumps({"login": "x" * (MAX_BODY_BYTES + 1)})},
])
def test_track_rejects_bad_bodies(app, kwargs):
    r = call(app, "POST", "/api/streamers", **kwargs)
    assert r["statusCode"] == 400


def test_base64_bodies_are_decoded(live):
    assert call(live, "POST", "/api/streamers", {"login": "amy"}, b64=True)["statusCode"] == 201


def test_board_flow_and_csv_export(live):
    call(live, "POST", "/api/streamers", {"login": "amy"})
    saved = call(live, "PUT", f"/api/boards/{BOARD}/items/c1", {"streamer": "amy", "note": "intro"})
    assert saved["statusCode"] == 200 and data(saved)["item"]["note"] == "intro"
    note_only = call(live, "PUT", f"/api/boards/{BOARD}/items/c1", {"note": "edited"})
    assert data(note_only)["item"]["note"] == "edited"
    board = data(call(live, "GET", f"/api/boards/{BOARD}"))
    assert [i["clip_id"] for i in board["items"]] == ["c1"]

    csv_response = call(live, "GET", f"/api/boards/{BOARD}/export.csv")
    assert csv_response["headers"]["content-type"].startswith("text/csv")
    assert "attachment" in csv_response["headers"]["content-disposition"]
    assert "Great one" in csv_response["body"]

    gone = call(live, "DELETE", f"/api/boards/{BOARD}/items/c1")
    assert gone["statusCode"] == 204 and gone["body"] == ""
    assert call(live, "DELETE", f"/api/boards/{BOARD}/items/c1")["statusCode"] == 204   # idempotent
    assert data(call(live, "GET", f"/api/boards/{BOARD}"))["items"] == []


def test_board_errors(live):
    assert call(live, "GET", "/api/boards/not-a-board-id")["statusCode"] == 400
    assert call(live, "PUT", f"/api/boards/{BOARD}/items/c1", {"streamer": "amy"})["statusCode"] == 404
    assert call(live, "PUT", f"/api/boards/{BOARD}/items/c1", {"note": "x"})["statusCode"] == 400


def test_unexpected_errors_return_500_without_leaking_details():
    def broken():
        raise RuntimeError("secret internal detail")
    r = call(App(broken), "GET", "/api/streamers")
    assert r["statusCode"] == 500 and "secret" not in r["body"]
    assert data(r)["error"]["code"] == "internal_error"


# --- open tracking over HTTP ------------------------------------------------------------------------

def test_the_streamer_list_shows_pinned_flags(live):
    call(live, "POST", "/api/streamers", {"login": "amy"})
    streamers = data(call(live, "GET", "/api/streamers"))["streamers"]
    assert streamers[0]["pinned"] is False and "expires_at" not in streamers[0] and "ttl" not in streamers[0]


def test_rate_limited_searches_get_429_with_retry_after(service, twitch):
    from dataclasses import replace
    from clippulse.service import ClipService
    limited = ClipService(service.store, service._twitch_provider, replace(service.settings, lookups_per_client=1),
                          clock=service._clock)
    app = App(lambda: limited)
    for name in ("amy", "bob"):
        bid = twitch.add_streamer(name)
        twitch.add_clip(bid, f"{name}-1", 10, ts(1))
    assert call(app, "POST", "/api/streamers", {"login": "amy"})["statusCode"] == 201
    r = call(app, "POST", "/api/streamers", {"login": "bob"})
    assert r["statusCode"] == 429 and data(r)["error"]["code"] == "rate_limited"
    assert 0 < int(r["headers"]["retry-after"]) <= 600


def test_visitors_are_told_apart_by_a_hash_of_their_ip(service, twitch):
    from dataclasses import replace
    from clippulse.service import ClipService
    limited = ClipService(service.store, service._twitch_provider, replace(service.settings, lookups_per_client=1),
                          clock=service._clock)
    app = App(lambda: limited)
    for name in ("amy", "bob", "kim"):
        bid = twitch.add_streamer(name)
        twitch.add_clip(bid, f"{name}-1", 10, ts(1))

    def post(login, ip):
        event = {"rawPath": "/api/streamers", "body": json.dumps({"login": login}),
                 "requestContext": {"http": {"method": "POST", "path": "/api/streamers", "sourceIp": ip}}}
        return app.handle(event)["statusCode"]

    assert post("amy", "203.0.113.7") == 201
    assert post("bob", "203.0.113.7") == 429          # same address: limited
    assert post("bob", "198.51.100.9") == 201         # different address: its own allowance
    stored = [item for item in service.store._counters] if hasattr(service.store, "_counters") else []
    assert all("203.0.113.7" not in str(key) for key in stored)   # the raw IP is never used as a key


def test_a_real_account_without_clips_gets_a_clear_404(app, twitch):
    twitch.add_streamer("quietone", display="QuietOne")
    r = call(app, "POST", "/api/streamers", {"login": "quietone"})
    assert r["statusCode"] == 404 and data(r)["error"]["code"] == "no_clips"
    assert "QuietOne" in data(r)["error"]["message"]


# --- visitors: ownership and removal over HTTP --------------------------------------------------------

def as_visitor(app, ip, method, path, body=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "path": path, "sourceIp": ip}}}
    if body is not None:
        event["body"] = json.dumps(body)
    return app.handle(event)


def test_a_visitor_can_add_see_and_remove_their_own_streamer(app, twitch):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "a1", 50, ts(1))
    added = as_visitor(app, "203.0.113.7", "POST", "/api/streamers", {"login": "amy"})
    assert added["statusCode"] == 201 and data(added)["streamer"]["mine"] is True
    mine = data(as_visitor(app, "203.0.113.7", "GET", "/api/streamers"))["streamers"]
    others = data(as_visitor(app, "198.51.100.9", "GET", "/api/streamers"))["streamers"]
    assert mine[0]["mine"] is True and others[0]["mine"] is False
    removed = as_visitor(app, "203.0.113.7", "DELETE", "/api/streamers/amy")
    assert removed["statusCode"] == 204 and not removed.get("body")
    assert data(as_visitor(app, "203.0.113.7", "GET", "/api/streamers"))["streamers"] == []


def test_deleting_someone_elses_streamer_is_403(app, twitch):
    twitch.add_clip(twitch.add_streamer("amy"), "a1", 50, ts(1))
    as_visitor(app, "203.0.113.7", "POST", "/api/streamers", {"login": "amy"})
    r = as_visitor(app, "198.51.100.9", "DELETE", "/api/streamers/amy")
    assert r["statusCode"] == 403 and data(r)["error"]["code"] == "forbidden"


def test_deleting_without_a_known_address_is_403_and_unknown_is_404(app, twitch):
    twitch.add_clip(twitch.add_streamer("amy"), "a1", 50, ts(1))
    call(app, "POST", "/api/streamers", {"login": "amy"})
    assert call(app, "DELETE", "/api/streamers/amy")["statusCode"] == 403
    assert as_visitor(app, "203.0.113.7", "DELETE", "/api/streamers/ghost")["statusCode"] == 404


def test_delete_is_rate_limited_with_retry_after(service):
    from dataclasses import replace
    from clippulse.service import ClipService
    limited = ClipService(service.store, service._twitch_provider, replace(service.settings, removals_per_client=1),
                          clock=service._clock)
    app = App(lambda: limited)
    assert as_visitor(app, "203.0.113.7", "DELETE", "/api/streamers/ghost")["statusCode"] == 404
    r = as_visitor(app, "203.0.113.7", "DELETE", "/api/streamers/ghost")
    assert r["statusCode"] == 429 and int(r["headers"]["retry-after"]) > 0
