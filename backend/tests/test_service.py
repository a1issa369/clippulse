import csv
import io

import pytest

from clippulse.service import (LimitReached, NotFound, UpstreamBusy, UpstreamError, ValidationError,
                               csv_safe)
from clippulse.twitch import TwitchError, TwitchRateLimited
from conftest import BOARD
from fakes import ts


def seeded(twitch, login="rdcworld1", n=3):
    bid = twitch.add_streamer(login, display=login.upper())
    for i in range(n):
        twitch.add_clip(bid, f"{login}-{i}", views=100 * (i + 1), created_at=ts(i + 1))
    return bid


# --- tracking ----------------------------------------------------------------

def test_track_streamer_creates_and_ingests(service, twitch):
    seeded(twitch)
    result = service.track_streamer("  @RDCworld1 ")
    assert result["created"] is True and result["clips_ingested"] == 3
    assert result["streamer"]["display_name"] == "RDCWORLD1"
    assert result["streamer"]["last_polled_at"] is not None
    assert [s["login"] for s in service.list_streamers()] == ["rdcworld1"]


def test_tracking_twice_is_idempotent_and_does_not_call_twitch_again(service, twitch):
    seeded(twitch)
    service.track_streamer("rdcworld1")
    calls = len(twitch.calls)
    again = service.track_streamer("rdcworld1")
    assert again["created"] is False and len(twitch.calls) == calls


@pytest.mark.parametrize("bad", ["", "ab", "has space", "x" * 26, "drop;table", None, 5, "../etc"])
def test_track_streamer_rejects_bad_logins(service, bad):
    with pytest.raises(ValidationError):
        service.track_streamer(bad)


def test_track_unknown_channel_is_not_found(service):
    with pytest.raises(NotFound):
        service.track_streamer("ghostchannel")


def test_streamer_limit(service, twitch):
    for name in ("aaa", "bbb", "ccc", "ddd"):
        seeded(twitch, name, n=1)
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)
    with pytest.raises(LimitReached):
        service.track_streamer("ddd")
    assert service.track_streamer("aaa")["created"] is False  # existing ones still resolve at the cap


def test_twitch_failures_map_to_upstream_errors(service, twitch):
    twitch.user_error = TwitchError("boom")
    with pytest.raises(UpstreamError):
        service.track_streamer("someone")
    twitch.user_error = TwitchRateLimited(5)
    with pytest.raises(UpstreamBusy):
        service.track_streamer("someone")


def test_streamer_stays_tracked_if_first_ingest_fails(service, twitch):
    bid = seeded(twitch)
    twitch.fail_for[bid] = TwitchError("temporary")
    result = service.track_streamer("rdcworld1")
    assert result["created"] is True and result["clips_ingested"] == 0
    assert len(service.list_streamers()) == 1


# --- polling and ranking ------------------------------------------------------

def test_polling_builds_history_and_recent_velocity(service, twitch, clock):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "rocket", views=100, created_at=ts(3))       # will surge
    twitch.add_clip(bid, "giant", views=5000, created_at=ts(40))      # huge but flat
    service.track_streamer("amy")
    clock.advance(hours=1)
    twitch.set_views(bid, "rocket", 400)
    twitch.set_views(bid, "giant", 5010)
    assert service.poll_all() == {"streamers": 1, "clips": 2, "errors": []}

    by_velocity = service.clips_for("amy", days=7, sort="velocity")["clips"]
    assert [c["id"] for c in by_velocity] == ["rocket", "giant"]
    assert by_velocity[0]["recent_velocity"] == pytest.approx(300.0)
    assert by_velocity[0]["trend"] == "up"
    assert by_velocity[1]["recent_velocity"] == pytest.approx(10.0)
    assert [c["id"] for c in service.clips_for("amy", sort="views")["clips"]] == ["giant", "rocket"]


def test_poll_continues_after_a_streamer_error_but_stops_on_rate_limit(service, twitch):
    a = seeded(twitch, "aaa", 1)
    seeded(twitch, "bbb", 1)
    seeded(twitch, "ccc", 1)
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)
    twitch.fail_for[a] = TwitchError("flaky")
    summary = service.poll_all()
    assert summary["streamers"] == 2 and [e["login"] for e in summary["errors"]] == ["aaa"]

    twitch.fail_for.clear()
    twitch.fail_for[twitch.users["bbb"]["broadcaster_id"]] = TwitchRateLimited(30)
    summary = service.poll_all()
    assert summary["streamers"] == 1                     # aaa succeeded, bbb limited, ccc never attempted
    assert [e["login"] for e in summary["errors"]] == ["bbb"]


def test_fresh_clips_are_captured_even_when_older_hits_fill_the_page(twitch, clock, store):
    from clippulse.config import Settings
    from clippulse.service import ClipService
    svc = ClipService(store, lambda: twitch, Settings(clips_per_streamer=3), clock=clock)
    bid = twitch.add_streamer("busy")
    for i in range(5):                                   # five big older clips fill the top-3 page
        twitch.add_clip(bid, f"hit-{i}", 10_000 + i, ts(30 + i))
    twitch.add_clip(bid, "just-posted", 2, ts(0.5))      # 2 views: invisible to a views-sorted top 3
    result = svc.track_streamer("busy")
    ids = [c["id"] for c in svc.clips_for("busy", sort="new")["clips"]]
    assert "just-posted" in ids
    assert result["clips_ingested"] == 4                 # top 3 of the week plus the fresh one, no duplicates


def test_a_clip_in_both_windows_is_stored_once(service, twitch):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "both", 50, ts(1))              # inside the week window and the fresh window
    assert service.track_streamer("amy")["clips_ingested"] == 1
    (clip,) = service.clips_for("amy")["clips"]
    assert clip["id"] == "both"


def test_clips_for_filters_by_days_and_limit(service, twitch):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "new", 10, ts(5))
    twitch.add_clip(bid, "old", 10, ts(24 * 5))
    service.track_streamer("amy")
    assert [c["id"] for c in service.clips_for("amy", days=1)["clips"]] == ["new"]
    assert len(service.clips_for("amy", days=7)["clips"]) == 2
    assert len(service.clips_for("amy", days=7, limit=1)["clips"]) == 1
    assert service.clips_for("amy", days=999)["clips"]  # clamped to the lookback window, not an error


def test_clips_for_validates_input(service, twitch):
    seeded(twitch)
    service.track_streamer("rdcworld1")
    with pytest.raises(ValidationError):
        service.clips_for("rdcworld1", sort="chaos")
    with pytest.raises(ValidationError):
        service.clips_for("rdcworld1", days="abc")
    with pytest.raises(NotFound):
        service.clips_for("untracked")


def test_rising_merges_streamers_and_respects_window(service, twitch):
    a = twitch.add_streamer("aaa")
    b = twitch.add_streamer("bbb")
    twitch.add_clip(a, "a-fast", 600, ts(2))
    twitch.add_clip(b, "b-slow", 60, ts(2))
    twitch.add_clip(b, "b-stale", 90000, ts(100))     # outside the 48h window
    service.track_streamer("aaa")
    service.track_streamer("bbb")
    result = service.rising(hours=48, limit=10)
    assert [c["id"] for c in result["clips"]] == ["a-fast", "b-slow"]
    assert result["clips"][0]["streamer"] == "aaa"
    assert result["as_of"] is not None


def test_rising_on_an_empty_database(service):
    assert service.rising() == {"as_of": None, "clips": []}


# --- boards -------------------------------------------------------------------

@pytest.fixture
def tracked(service, twitch):
    seeded(twitch, "amy", n=4)
    service.track_streamer("amy")
    return service


def test_save_item_snapshots_clip_from_stored_data(tracked, clock):
    item = tracked.save_item(BOARD, "amy-1", "amy", "  cold open candidate ")
    assert item["title"] == "Clip amy-1" and item["url"].startswith("https://clips.twitch.tv/")
    assert item["note"] == "cold open candidate" and item["views_at_save"] == 200
    assert "ttl" not in item
    assert tracked.get_board(BOARD)["items"][0]["clip_id"] == "amy-1"


def test_saving_again_updates_only_the_note(tracked):
    tracked.save_item(BOARD, "amy-1", "amy", "first")
    updated = tracked.save_item(BOARD, "amy-1", None, "second")
    assert updated["note"] == "second"
    assert len(tracked.get_board(BOARD)["items"]) == 1
    assert tracked.save_item(BOARD, "amy-1", None, None)["note"] == "second"   # no note given: unchanged


def test_new_item_needs_a_tracked_streamer_and_a_known_clip(tracked):
    with pytest.raises(ValidationError):
        tracked.save_item(BOARD, "amy-1", None, "n")
    with pytest.raises(NotFound):
        tracked.save_item(BOARD, "amy-1", "someoneelse", "n")
    with pytest.raises(NotFound):
        tracked.save_item(BOARD, "not-a-real-clip", "amy", "n")


def test_board_validation(tracked):
    with pytest.raises(ValidationError):
        tracked.get_board("short")
    with pytest.raises(ValidationError):
        tracked.get_board("G" * 32)  # right length, not hex
    with pytest.raises(ValidationError):
        tracked.save_item(BOARD, "bad clip id!", "amy", "")
    with pytest.raises(ValidationError):
        tracked.save_item(BOARD, "amy-1", "amy", "x" * 501)
    with pytest.raises(ValidationError):
        tracked.save_item(BOARD, "amy-1", "amy", 123)


def test_board_item_limit(tracked):
    for i in range(3):
        tracked.save_item(BOARD, f"amy-{i}", "amy", "")
    with pytest.raises(LimitReached):
        tracked.save_item(BOARD, "amy-3", "amy", "")
    assert tracked.save_item(BOARD, "amy-0", None, "edit ok at the cap")["note"] == "edit ok at the cap"


def test_remove_item_is_idempotent(tracked):
    tracked.save_item(BOARD, "amy-1", "amy", "")
    assert tracked.remove_item(BOARD, "amy-1") is True
    assert tracked.remove_item(BOARD, "amy-1") is False
    assert tracked.get_board(BOARD)["items"] == []


def test_items_are_listed_in_the_order_they_were_saved(tracked, clock):
    for clip_id in ("amy-2", "amy-0", "amy-1"):
        tracked.save_item(BOARD, clip_id, "amy", "")
        clock.advance(minutes=1)
    assert [i["clip_id"] for i in tracked.get_board(BOARD)["items"]] == ["amy-2", "amy-0", "amy-1"]


def test_export_csv_columns_quoting_and_formula_safety(service, twitch):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "evil", 5, ts(1), title='=HYPERLINK("http://x","click")')
    twitch.add_clip(bid, "plain", 7, ts(2), title='He said "wow", then left')
    service.track_streamer("amy")
    service.save_item(BOARD, "evil", "amy", "+SUM(A1)")
    service.save_item(BOARD, "plain", "amy", "line1\nline2")
    rows = list(csv.reader(io.StringIO(service.export_csv(BOARD))))
    assert rows[0] == ["streamer", "title", "clip_url", "clip_created_at", "clip_length_s",
                       "vod_offset_s", "views_when_saved", "note", "saved_at"]
    evil, plain = rows[1], rows[2]
    assert evil[1].startswith("'=") and evil[7] == "'+SUM(A1)"
    assert plain[1] == 'He said "wow", then left' and plain[7] == "line1\nline2"
    assert plain[4] == "12.5" and plain[5] == "90" and plain[6] == "7"


def test_export_of_an_empty_board_is_just_headers(service):
    assert service.export_csv(BOARD).count("\n") == 1


@pytest.mark.parametrize("value,expected", [("=1+1", "'=1+1"), ("-5", "'-5"), ("@a", "'@a"), ("ok", "ok"), (5, 5), ("", "")])
def test_csv_safe(value, expected):
    assert csv_safe(value) == expected
