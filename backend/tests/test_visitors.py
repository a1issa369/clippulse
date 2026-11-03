"""Per-visitor protections: a cap on how many streamers one visitor holds, ownership, removal, and search limits."""
from dataclasses import replace

import pytest

from clippulse.service import ClipService, Forbidden, LimitReached, NotFound, RateLimited
from fakes import ts


def make(settings, store, twitch, clock, **changes):
    return ClipService(store, lambda: twitch, replace(settings, **changes), clock=clock)


def stock(twitch, *logins):
    for login in logins:
        bid = twitch.add_streamer(login)
        twitch.add_clip(bid, f"{login}-1", 100, ts(2))


@pytest.fixture
def roomy(settings, store, twitch, clock):
    """Plenty of pool space so only the per-visitor rules are in play."""
    stock(twitch, "amy", "bob", "cat", "dan", "eve")
    return make(settings, store, twitch, clock, max_temp_streamers=10)


# --- client ids ---------------------------------------------------------------------------------

def test_client_id_is_stable_salted_and_hides_the_ip(settings, store, twitch, clock):
    a = make(settings, store, twitch, clock, client_salt="one")
    b = make(settings, store, twitch, clock, client_salt="two")
    assert a.client_id("1.2.3.4") == a.client_id("1.2.3.4")
    assert a.client_id("1.2.3.4") != a.client_id("1.2.3.5")
    assert a.client_id("1.2.3.4") != b.client_id("1.2.3.4")
    assert "1.2.3.4" not in a.client_id("1.2.3.4")


@pytest.mark.parametrize("ip", [None, ""])
def test_client_id_is_none_without_an_address(service, ip):
    assert service.client_id(ip) is None


# --- cap per visitor ----------------------------------------------------------------------------

def test_a_visitor_can_hold_only_a_few_streamers(roomy):
    for name in ("amy", "bob", "cat"):
        roomy.track_streamer(name, client_id="v1")
    with pytest.raises(LimitReached, match="Remove one"):
        roomy.track_streamer("dan", client_id="v1")
    assert roomy.store.get_streamer("dan") is None


def test_the_cap_is_per_visitor(roomy):
    for name in ("amy", "bob", "cat"):
        roomy.track_streamer(name, client_id="v1")
    assert roomy.track_streamer("dan", client_id="v2")["created"] is True


def test_the_cap_refusal_costs_no_lookup_tokens_or_twitch_calls(settings, store, twitch, clock):
    stock(twitch, "amy", "bob", "cat", "dan")
    svc = make(settings, store, twitch, clock, max_temp_streamers=10, lookups_per_client=3)
    for name in ("amy", "bob", "cat"):
        svc.track_streamer(name, client_id="v1")
    calls = len(twitch.calls)
    assert calls > 0
    with pytest.raises(LimitReached):
        svc.track_streamer("dan", client_id="v1")
    assert len(twitch.calls) == calls


def test_looking_at_a_streamer_someone_already_added_does_not_count_toward_the_cap(roomy):
    for name in ("amy", "bob", "cat"):
        roomy.track_streamer(name, client_id="v1")
    assert roomy.track_streamer("amy", client_id="v2")["created"] is False
    assert roomy.track_streamer("dan", client_id="v2")["created"] is True


def test_pinned_streamers_never_count_toward_the_cap(settings, store, twitch, clock):
    stock(twitch, "pin1", "amy", "bob", "cat")
    svc = make(settings, store, twitch, clock, max_temp_streamers=10, pinned_streamers=("pin1",))
    svc.ensure_pinned()
    for name in ("amy", "bob", "cat"):
        svc.track_streamer(name, client_id="v1")
    assert len(svc.list_streamers("v1")) == 4


def test_expired_streamers_free_up_the_cap(roomy, clock):
    for name in ("amy", "bob", "cat"):
        roomy.track_streamer(name, client_id="v1")
    clock.advance(hours=49)
    assert roomy.track_streamer("dan", client_id="v1")["created"] is True


# --- ownership -----------------------------------------------------------------------------------

def test_only_the_adder_sees_mine(roomy):
    roomy.track_streamer("amy", client_id="v1")
    assert [(s["login"], s["mine"]) for s in roomy.list_streamers("v1")] == [("amy", True)]
    assert roomy.list_streamers("v2")[0]["mine"] is False
    assert roomy.list_streamers()[0]["mine"] is False
    assert roomy.track_streamer("amy", client_id="v1")["streamer"]["mine"] is True


def test_the_stored_visitor_id_is_never_exposed(roomy):
    result = roomy.track_streamer("amy", client_id="secretid")
    assert "secretid" not in repr(result) and "secretid" not in repr(roomy.list_streamers("secretid"))


def test_no_client_id_means_nothing_is_mine(roomy):
    roomy.track_streamer("amy")
    assert "added_by" not in roomy.store.get_streamer("amy")
    assert roomy.list_streamers(None)[0]["mine"] is False


# --- removal -------------------------------------------------------------------------------------

def test_a_visitor_can_remove_their_own_streamer_and_their_clips_vanish(roomy):
    roomy.track_streamer("amy", client_id="v1")
    roomy.remove_streamer("Amy", "v1")
    assert roomy.list_streamers("v1") == []
    assert roomy.rising()["clips"] == []
    with pytest.raises(NotFound):
        roomy.clips_for("amy")


def test_removing_frees_a_slot_under_the_cap(roomy):
    for name in ("amy", "bob", "cat"):
        roomy.track_streamer(name, client_id="v1")
    roomy.remove_streamer("amy", "v1")
    assert roomy.track_streamer("dan", client_id="v1")["created"] is True


def test_cannot_remove_someone_elses_streamer(roomy):
    roomy.track_streamer("amy", client_id="v1")
    with pytest.raises(Forbidden):
        roomy.remove_streamer("amy", "v2")
    assert roomy.store.get_streamer("amy") is not None


def test_cannot_remove_a_pinned_streamer(settings, store, twitch, clock):
    stock(twitch, "pin1")
    svc = make(settings, store, twitch, clock, pinned_streamers=("pin1",))
    svc.ensure_pinned()
    with pytest.raises(Forbidden, match="Pinned"):
        svc.remove_streamer("pin1", "v1")
    assert svc.store.get_streamer("pin1") is not None


def test_legacy_streamers_without_flags_count_as_pinned_and_are_safe(settings, store, twitch, clock):
    store.put_streamer({"login": "old", "display_name": "Old", "user_id": "1"})
    svc = make(settings, store, twitch, clock)
    with pytest.raises(Forbidden):
        svc.remove_streamer("old", "v1")


def test_removal_needs_a_visitor_id(roomy):
    roomy.track_streamer("amy")
    with pytest.raises(Forbidden):
        roomy.remove_streamer("amy", None)
    assert roomy.store.get_streamer("amy") is not None


def test_removing_something_not_tracked_is_not_found(roomy, clock):
    with pytest.raises(NotFound):
        roomy.remove_streamer("amy", "v1")
    roomy.track_streamer("bob", client_id="v1")
    clock.advance(hours=49)
    with pytest.raises(NotFound):
        roomy.remove_streamer("bob", "v1")


def test_removal_rejects_bad_names(roomy):
    with pytest.raises(Exception):
        roomy.remove_streamer("not a name!!", "v1")


def test_removals_are_rate_limited_per_visitor(settings, store, twitch, clock):
    stock(twitch, "amy")
    svc = make(settings, store, twitch, clock, removals_per_client=2)
    for _ in range(2):
        with pytest.raises(NotFound):
            svc.remove_streamer("amy", "v1")
    with pytest.raises(RateLimited) as err:
        svc.remove_streamer("amy", "v1")
    assert err.value.retry_after > 0
    with pytest.raises(NotFound):                       # another visitor is unaffected
        svc.remove_streamer("amy", "v2")
    clock.advance(seconds=settings.client_window_seconds + 1)
    with pytest.raises(NotFound):
        svc.remove_streamer("amy", "v1")


# --- search limits -------------------------------------------------------------------------------

def test_searches_are_limited_even_for_already_tracked_streamers(settings, store, twitch, clock):
    stock(twitch, "amy")
    svc = make(settings, store, twitch, clock, searches_per_client=3)
    svc.track_streamer("amy", client_id="v1")
    svc.track_streamer("amy", client_id="v1")
    svc.track_streamer("amy", client_id="v1")
    with pytest.raises(RateLimited, match="searching too quickly"):
        svc.track_streamer("amy", client_id="v1")
    assert svc.track_streamer("amy", client_id="v2")["created"] is False
    clock.advance(seconds=settings.client_window_seconds + 1)
    assert svc.track_streamer("amy", client_id="v1")["created"] is False


def test_unidentified_visitors_share_one_search_bucket(settings, store, twitch, clock):
    stock(twitch, "amy")
    svc = make(settings, store, twitch, clock, searches_per_client=2)
    svc.track_streamer("amy")
    svc.track_streamer("amy")
    with pytest.raises(RateLimited):
        svc.track_streamer("amy")
