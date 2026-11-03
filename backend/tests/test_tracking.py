"""Open tracking: anyone can search any streamer, within hard limits.

Pinned streamers never expire. Visitor-added ones expire, share a small fixed pool, and
every brand-new lookup is rate limited per visitor and for everyone together."""
from dataclasses import replace

import pytest

from clippulse.service import ClipService, LimitReached, NotFound, RateLimited
from clippulse.twitch import TwitchError
from fakes import ts


def make(settings, store, twitch, clock, **changes):
    return ClipService(store, lambda: twitch, replace(settings, **changes), clock=clock)


def stock(twitch, *logins):
    for login in logins:
        bid = twitch.add_streamer(login)
        twitch.add_clip(bid, f"{login}-1", 100, ts(2))


# --- visitor-added streamers are temporary --------------------------------------------------------

def test_a_searched_streamer_is_tracked_temporarily(service, twitch, clock):
    stock(twitch, "amy")
    result = service.track_streamer("amy")
    assert result["created"] is True and result["streamer"]["pinned"] is False
    stored = service.store.get_streamer("amy")
    assert stored["pinned"] is False and stored["expires_at"] > stored["last_seen_at"]
    assert stored["ttl"] > 0


def test_they_disappear_after_the_stay_and_stop_being_polled(service, twitch, clock):
    stock(twitch, "amy")
    service.track_streamer("amy")
    clock.advance(hours=47)
    assert [s["login"] for s in service.list_streamers()] == ["amy"]
    clock.advance(hours=2)                                              # 49 hours: past the 48 hour stay
    assert service.list_streamers() == []
    assert service.rising()["clips"] == []
    assert service.poll_all()["streamers"] == 0                          # no Twitch calls for the expired one
    with pytest.raises(NotFound):
        service.clips_for("amy")


def test_looking_at_a_streamer_extends_their_stay(service, twitch, clock):
    stock(twitch, "amy")
    service.track_streamer("amy")
    for _ in range(3):
        clock.advance(hours=30)                                          # would be gone after 48 hours...
        service.clips_for("amy")                                         # ...but someone keeps looking
    assert [s["login"] for s in service.list_streamers()] == ["amy"]


def test_extending_a_stay_writes_at_most_once_per_interval(service, twitch, clock):
    stock(twitch, "amy")
    service.track_streamer("amy")
    first = service.store.get_streamer("amy")["last_seen_at"]
    clock.advance(minutes=30)
    for _ in range(5):
        service.clips_for("amy")
        service.track_streamer("amy")
    assert service.store.get_streamer("amy")["last_seen_at"] == first    # inside the interval: no rewrite
    clock.advance(minutes=31)
    service.clips_for("amy")
    assert service.store.get_streamer("amy")["last_seen_at"] != first


def test_searching_an_existing_streamer_costs_no_twitch_calls_and_no_lookup_tokens(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_per_client=1)
    stock(twitch, "amy")
    service.track_streamer("amy", client_id="v1")
    calls = len(twitch.calls)
    for _ in range(10):                                                  # far past the per-visitor limit of 1
        assert service.track_streamer("amy", client_id="v1")["created"] is False
    assert len(twitch.calls) == calls


def test_an_expired_streamer_can_be_added_again(service, twitch, clock):
    stock(twitch, "amy")
    service.track_streamer("amy")
    clock.advance(hours=60)
    again = service.track_streamer("amy")
    assert again["created"] is True and [s["login"] for s in service.list_streamers()] == ["amy"]


def test_the_hourly_run_prunes_expired_streamers(service, twitch, clock):
    stock(twitch, "amy", "bob")
    service.track_streamer("amy")
    clock.advance(hours=40)
    service.track_streamer("bob")
    clock.advance(hours=10)                                              # amy is 50 hours old, bob 10
    summary = service.run_hourly()
    assert summary["pruned"] == 1 and summary["streamers"] == 1
    assert service.store.get_streamer("amy") is None and service.store.get_streamer("bob") is not None


# --- the shared pool has a hard size -----------------------------------------------------------------

def test_pool_full_and_everyone_recent_is_refused(service, twitch, clock):
    stock(twitch, "aaa", "bbb", "ccc", "ddd")                           # the test settings allow 3 visitor streamers
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)
    with pytest.raises(LimitReached):
        service.track_streamer("ddd")
    assert service.track_streamer("aaa")["created"] is False             # existing ones still resolve
    assert twitch.calls.count(("get_user", "ddd")) == 0                  # refused before spending a Twitch call


def test_pool_full_replaces_the_longest_idle_streamer_once_it_has_been_idle_long_enough(service, twitch, clock):
    stock(twitch, "aaa", "bbb", "ccc", "ddd")
    service.track_streamer("aaa")
    clock.advance(hours=1)
    service.track_streamer("bbb")
    clock.advance(hours=1)
    service.track_streamer("ccc")
    clock.advance(hours=5)                                               # aaa idle 7h, bbb 6h, ccc 5h: aaa goes first
    assert service.track_streamer("ddd")["created"] is True
    assert [s["login"] for s in service.list_streamers()] == ["bbb", "ccc", "ddd"]


def test_a_search_for_a_channel_that_does_not_exist_never_evicts_anyone(service, twitch, clock):
    stock(twitch, "aaa", "bbb", "ccc")
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)
    clock.advance(hours=10)                                              # everyone is idle, so a slot could be freed
    with pytest.raises(NotFound):
        service.track_streamer("ghostchannel")
    assert [s["login"] for s in service.list_streamers()] == ["aaa", "bbb", "ccc"]


def test_pinned_streamers_do_not_count_against_the_pool_or_get_replaced(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("pin1", "pin2"))
    stock(twitch, "pin1", "pin2", "aaa", "bbb", "ccc", "ddd")
    service.ensure_pinned()
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)                                     # pool of 3 is full of visitors; pins are extra
    clock.advance(hours=10)
    service.track_streamer("ddd")
    logins = [s["login"] for s in service.list_streamers()]
    assert logins[:2] == ["pin1", "pin2"] and len(logins) == 5


# --- rate limits ---------------------------------------------------------------------------------------

def test_each_visitor_gets_a_few_new_lookups_per_window(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_per_client=2, max_temp_streamers=50)
    stock(twitch, *[f"chan{i}" for i in range(6)])
    service.track_streamer("chan0", client_id="alice")
    service.track_streamer("chan1", client_id="alice")
    with pytest.raises(RateLimited) as err:
        service.track_streamer("chan2", client_id="alice")
    assert err.value.status == 429 and 0 < err.value.retry_after <= 600
    assert "too quickly" in err.value.message
    assert service.track_streamer("chan2", client_id="bob")["created"] is True      # someone else is unaffected
    clock.advance(seconds=600)
    assert service.track_streamer("chan3", client_id="alice")["created"] is True    # a new window


def test_unknown_names_count_against_the_limit_too(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_per_client=2)
    for name in ("nobody1", "nobody2"):
        with pytest.raises(NotFound):
            service.track_streamer(name, client_id="probe")
    with pytest.raises(RateLimited):                                     # guessing names cannot hammer Twitch
        service.track_streamer("nobody3", client_id="probe")
    assert len([c for c in twitch.calls if c[0] == "get_user"]) == 2


def test_everyone_together_shares_a_global_limit(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_global=3, max_temp_streamers=50)
    stock(twitch, *[f"chan{i}" for i in range(5)])
    for i in range(3):
        service.track_streamer(f"chan{i}", client_id=f"visitor{i}")            # all different visitors
    with pytest.raises(RateLimited) as err:
        service.track_streamer("chan3", client_id="visitor99")
    assert "lot of new streamers" in err.value.message and err.value.retry_after <= 3600
    clock.advance(hours=1)
    assert service.track_streamer("chan3", client_id="visitor99")["created"] is True


def test_a_missing_client_id_shares_one_bucket_instead_of_skipping_the_limit(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_per_client=1)
    stock(twitch, "amy", "bob")
    service.track_streamer("amy")
    with pytest.raises(RateLimited):
        service.track_streamer("bob")


def test_twitch_failure_still_spends_the_lookup(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, lookups_per_client=1)
    twitch.user_error = TwitchError("boom")
    with pytest.raises(Exception):
        service.track_streamer("amy", client_id="v")
    twitch.user_error = None
    stock(twitch, "amy")
    with pytest.raises(RateLimited):
        service.track_streamer("amy", client_id="v")


# --- the pinned list -----------------------------------------------------------------------------------

def test_pinned_streamers_are_created_ingested_and_listed_first(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("zed", "amy"))
    stock(twitch, "zed", "amy", "visitor")
    service.track_streamer("visitor")
    result = service.ensure_pinned()
    assert result["ensured"] == 2 and result["errors"] == []
    listed = service.list_streamers()
    assert [(s["login"], s["pinned"]) for s in listed] == [("amy", True), ("zed", True), ("visitor", False)]
    assert service.clips_for("amy")["clips"]                              # clips were ingested right away
    assert "expires_at" not in service.store.get_streamer("amy")


def test_pinned_streamers_never_expire(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("amy",))
    stock(twitch, "amy")
    service.ensure_pinned()
    clock.advance(days=90)
    assert [s["login"] for s in service.list_streamers()] == ["amy"]
    assert service.prune_expired() == 0


def test_a_visitor_streamer_that_gets_pinned_stops_expiring(settings, store, twitch, clock):
    stock(twitch, "amy")
    visitor_view = make(settings, store, twitch, clock)
    visitor_view.track_streamer("amy")
    assert "expires_at" in store.get_streamer("amy")
    make(settings, store, twitch, clock, pinned_streamers=("amy",)).ensure_pinned()
    saved = store.get_streamer("amy")
    assert saved["pinned"] is True and "expires_at" not in saved and "ttl" not in saved


def test_removing_a_streamer_from_the_pinned_list_demotes_it_instead_of_deleting_it(settings, store, twitch, clock):
    stock(twitch, "amy")
    make(settings, store, twitch, clock, pinned_streamers=("amy",)).ensure_pinned()
    unpinned = make(settings, store, twitch, clock, pinned_streamers=())
    assert unpinned.ensure_pinned()["demoted"] == 1
    assert unpinned.list_streamers()[0]["pinned"] is False
    clock.advance(hours=49)
    assert unpinned.list_streamers() == []


def test_streamers_saved_before_this_feature_count_as_pinned(settings, store, twitch, clock):
    store.put_streamer({"login": "old", "broadcaster_id": "9", "display_name": "Old", "last_polled_at": None})
    service = make(settings, store, twitch, clock, pinned_streamers=("old",))
    assert service.list_streamers()[0]["pinned"] is True
    clock.advance(days=30)
    assert len(service.list_streamers()) == 1
    service.ensure_pinned()                                              # listed in PinnedStreamers: becomes explicit
    assert store.get_streamer("old")["pinned"] is True


def test_pinned_problems_are_reported_not_fatal(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("ghostchannel", "amy", "bad name!"))
    stock(twitch, "amy")
    result = service.ensure_pinned()
    assert result["ensured"] == 1 and [e["login"] for e in result["errors"]] == ["ghostchannel"]
    twitch.user_error = TwitchError("down")
    result = make(settings, store, twitch, clock, pinned_streamers=("newone",)).ensure_pinned()
    assert result["ensured"] == 0 and result["errors"][0]["error"] == "down"


def test_run_hourly_reports_everything(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("amy",))
    stock(twitch, "amy", "bob")
    service.track_streamer("bob")
    summary = service.run_hourly()
    assert summary["streamers"] == 2 and summary["errors"] == [] and summary["pruned"] == 0
    assert summary["pinned"]["ensured"] == 1


# --- real accounts with nothing to show --------------------------------------------------------------------

def test_an_account_with_no_recent_clips_is_not_tracked(settings, store, twitch, clock):
    from clippulse.service import NoClips
    service = make(settings, store, twitch, clock, lookups_per_client=2)
    twitch.add_streamer("quietone", display="QuietOne")                  # exists on Twitch, has no clips
    with pytest.raises(NoClips) as err:
        service.track_streamer("quietone", client_id="v")
    assert err.value.status == 404 and err.value.code == "no_clips" and "QuietOne" in err.value.message
    assert service.list_streamers() == [] and store.get_streamer("quietone") is None
    with pytest.raises(NoClips):
        service.track_streamer("quietone", client_id="v")
    with pytest.raises(RateLimited):                                     # it still cost a lookup each time
        service.track_streamer("quietone", client_id="v")


def test_clips_older_than_the_lookback_do_not_count(service, twitch, clock):
    from clippulse.service import NoClips
    bid = twitch.add_streamer("oldtimer")
    twitch.add_clip(bid, "ancient", 5000, ts(24 * 30))                   # a month old: outside the 7 day lookback
    with pytest.raises(NoClips):
        service.track_streamer("oldtimer")


def test_a_clipless_search_does_not_push_anyone_out(service, twitch, clock):
    from clippulse.service import NoClips
    stock(twitch, "aaa", "bbb", "ccc")
    twitch.add_streamer("quietone")
    for name in ("aaa", "bbb", "ccc"):
        service.track_streamer(name)
    clock.advance(hours=10)                                              # all idle, so a slot would be freed...
    with pytest.raises(NoClips):
        service.track_streamer("quietone")
    assert [s["login"] for s in service.list_streamers()] == ["aaa", "bbb", "ccc"]   # ...but nobody was replaced


def test_a_twitch_hiccup_while_fetching_clips_still_keeps_the_streamer(service, twitch, clock):
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "c1", 10, ts(1))
    twitch.fail_for[bid] = TwitchError("temporary")
    result = service.track_streamer("amy")
    assert result["created"] is True and result["clips_ingested"] == 0
    assert [s["login"] for s in service.list_streamers()] == ["amy"]


def test_the_hourly_run_drops_visitor_streamers_that_have_no_clips_left(settings, store, twitch, clock):
    service = make(settings, store, twitch, clock, pinned_streamers=("quietpin",), temp_ttl_hours=24 * 30)
    twitch.add_streamer("quietpin")                                      # pinned: kept even with no clips
    stock(twitch, "amy")
    service.ensure_pinned()
    service.track_streamer("amy")
    clock.advance(days=8)                                                # amy's only clip is now older than 7 days
    summary = service.run_hourly()
    assert summary["emptied"] == 1
    assert [s["login"] for s in service.list_streamers()] == ["quietpin"]


def test_streamers_with_clips_survive_the_empty_check(service, twitch, clock):
    stock(twitch, "amy")
    service.track_streamer("amy")
    assert service.prune_empty() == 0 and len(service.list_streamers()) == 1
