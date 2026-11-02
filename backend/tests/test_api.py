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
