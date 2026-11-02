"""Contract tests: every Store implementation must pass all of these."""
import pytest

from clippulse.store import DynamoStore, from_dynamo, to_dynamo
from decimal import Decimal
from fakes import FakeTable, ts

BID = "42"


def clip(i, views=10, created_hours_ago=1, duration=12.5):
    return {"id": f"c{i}", "url": f"https://clips.twitch.tv/c{i}", "title": f"Clip {i}", "view_count": views,
            "created_at": ts(created_hours_ago), "thumbnail_url": "t", "duration": duration, "vod_offset": None}


def test_streamers_round_trip_sorted_by_login(store):
    for login in ("zed", "amy", "kim"):
        store.put_streamer({"login": login, "broadcaster_id": login, "display_name": login.title()})
    assert [s["login"] for s in store.list_streamers()] == ["amy", "kim", "zed"]
    assert store.get_streamer("kim")["display_name"] == "Kim"
    assert store.get_streamer("nobody") is None


def test_put_streamer_overwrites(store):
    store.put_streamer({"login": "amy", "broadcaster_id": "1", "display_name": "Amy", "last_polled_at": None})
    store.put_streamer({"login": "amy", "broadcaster_id": "1", "display_name": "Amy", "last_polled_at": "now"})
    assert len(store.list_streamers()) == 1
    assert store.get_streamer("amy")["last_polled_at"] == "now"


def test_upsert_appends_history_and_updates_views(store):
    store.upsert_clips(BID, [clip(1, views=10)], "2026-11-02T10:00:00Z", 48, ttl=1)
    store.upsert_clips(BID, [clip(1, views=25)], "2026-11-02T11:00:00Z", 48, ttl=2)
    (row,) = store.query_clips(BID, "2000-01-01T00:00:00Z")
    assert row["view_count"] == 25
    assert row["history"] == [["2026-11-02T10:00:00Z", 10], ["2026-11-02T11:00:00Z", 25]]
    assert row["ttl"] == 2


def test_history_is_capped_to_the_newest_points(store):
    for hour in range(5):
        store.upsert_clips(BID, [clip(1, views=hour)], f"2026-11-02T0{hour}:00:00Z", 3, ttl=1)
    (row,) = store.query_clips(BID, "2000-01-01T00:00:00Z")
    assert [v for _, v in row["history"]] == [2, 3, 4]


def test_same_poll_timestamp_overwrites_instead_of_duplicating(store):
    store.upsert_clips(BID, [clip(1, views=5)], "2026-11-02T10:00:00Z", 48, ttl=1)
    store.upsert_clips(BID, [clip(1, views=7)], "2026-11-02T10:00:00Z", 48, ttl=1)
    (row,) = store.query_clips(BID, "2000-01-01T00:00:00Z")
    assert row["history"] == [["2026-11-02T10:00:00Z", 7]]


def test_query_filters_by_creation_time_and_returns_newest_first(store):
    clips = [clip(i, created_hours_ago=h) for i, h in enumerate([50, 30, 10, 5, 1])]
    store.upsert_clips(BID, clips, "2026-11-02T10:00:00Z", 48, ttl=1)
    rows = store.query_clips(BID, ts(31))
    assert [r["id"] for r in rows] == ["c4", "c3", "c2", "c1"]  # 5 clips, small pages, still complete


def test_clips_are_isolated_per_broadcaster(store):
    store.upsert_clips("1", [clip(1)], "2026-11-02T10:00:00Z", 48, ttl=1)
    store.upsert_clips("2", [clip(2)], "2026-11-02T10:00:00Z", 48, ttl=1)
    assert [r["id"] for r in store.query_clips("1", "2000-01-01T00:00:00Z")] == ["c1"]


def test_floats_and_ints_survive_a_round_trip(store):
    store.upsert_clips(BID, [clip(1, views=7, duration=12.5)], "2026-11-02T10:00:00Z", 48, ttl=1)
    (row,) = store.query_clips(BID, "2000-01-01T00:00:00Z")
    assert row["duration"] == 12.5 and isinstance(row["view_count"], int)
    assert row["vod_offset"] is None


def test_returned_data_is_a_copy(store):
    store.put_streamer({"login": "amy", "broadcaster_id": "1", "display_name": "Amy"})
    store.get_streamer("amy")["display_name"] = "changed"
    assert store.get_streamer("amy")["display_name"] == "Amy"


def item(clip_id, note="n"):
    return {"clip_id": clip_id, "title": "T", "note": note, "added_at": "2026-11-02T10:00:00Z", "duration": 3.5}


def test_board_items_crud(store):
    store.put_board_item("b1", item("x"))
    store.put_board_item("b1", item("y"))
    assert store.get_board_item("b1", "x")["note"] == "n"
    assert store.get_board_item("b1", "x")["duration"] == 3.5
    assert sorted(i["clip_id"] for i in store.list_board_items("b1")) == ["x", "y"]
    assert store.delete_board_item("b1", "x") is True
    assert store.delete_board_item("b1", "x") is False
    assert store.get_board_item("b1", "x") is None


def test_boards_are_isolated(store):
    store.put_board_item("b1", item("x"))
    assert store.list_board_items("b2") == []


def test_board_list_pages_through_many_items(store):
    for i in range(7):
        store.put_board_item("b1", item(f"c{i}"))
    assert len(store.list_board_items("b1")) == 7


# --- DynamoDB specifics ------------------------------------------------------

def test_dynamo_rows_do_not_leak_table_keys():
    store = DynamoStore(FakeTable())
    store.put_streamer({"login": "amy", "broadcaster_id": "1", "display_name": "Amy"})
    assert "pk" not in store.get_streamer("amy") and "sk" not in store.list_streamers()[0]


def test_dynamo_uses_the_documented_key_layout():
    table = FakeTable()
    store = DynamoStore(table)
    store.put_streamer({"login": "amy", "broadcaster_id": "1", "display_name": "Amy"})
    store.upsert_clips("1", [clip(1)], "2026-11-02T10:00:00Z", 48, ttl=1)
    store.put_board_item("b1", item("x"))
    keys = sorted(table.items)
    assert keys[0] == ("B#1", f"C#{ts(1)}#c1")
    assert ("BOARD#b1", "I#x") in keys and ("STREAMERS", "S#amy") in keys


def test_decimal_conversion_helpers():
    assert to_dynamo({"a": [1.5, {"b": 2.25}], "c": 3}) == {"a": [Decimal("1.5"), {"b": Decimal("2.25")}], "c": 3}
    assert from_dynamo({"a": Decimal("4"), "b": Decimal("4.5"), "c": [Decimal("1")]}) == {"a": 4, "b": 4.5, "c": [1]}
    assert isinstance(from_dynamo(Decimal("4")), int)


def test_fake_table_rejects_floats_like_boto3_does():
    with pytest.raises(TypeError):
        FakeTable().put_item(Item={"pk": "p", "sk": "s", "x": 1.5})
