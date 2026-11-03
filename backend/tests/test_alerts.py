"""Surge alerts: choosing what to announce, building the Discord message, and the two Lambdas around the queue."""
import json
from dataclasses import replace

import pytest

from clippulse import alerts
from clippulse.alerts import DiscordRejected, DiscordRetryable, SqsPublisher
from clippulse.service import ClipService
from fakes import ts

DISCORD = "https://discord.com/api/webhooks/123/abc"


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def publish(self, alert):
        if self.fail:
            raise RuntimeError("queue is down")
        self.sent.append(alert)


def make(settings, store, twitch, clock, outbox, **changes):
    base = replace(settings, pinned_streamers=("amy",), max_temp_streamers=3, **changes)
    return ClipService(store, lambda: twitch, base, clock=clock, alerts=outbox)


def surging(twitch, clock, svc, views_now=900, clip_id="hot", streamer="amy", age_hours=3):
    """One clip that gains views fast between two snapshots an hour apart."""
    bid = twitch.users.get(streamer, {}).get("broadcaster_id") or twitch.add_streamer(streamer)
    twitch.add_clip(bid, clip_id, 100, ts(age_hours))
    svc.ensure_pinned()
    svc.poll_all()
    clock.advance(hours=1)
    twitch.set_views(bid, clip_id, views_now)
    svc.poll_all()
    return bid


# --- choosing what to announce -------------------------------------------------------------------

def test_a_surging_clip_on_a_pinned_streamer_is_announced_once(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out)
    surging(twitch, clock, svc)
    first = svc.announce_surges()
    assert first["sent"] == 1 and out.sent[0]["clip_id"] == "hot" and out.sent[0]["velocity"] >= 30
    again = svc.announce_surges()
    assert again["sent"] == 0 and again["skipped"] == 1 and len(out.sent) == 1


def test_slow_and_steady_clips_are_not_announced(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out)
    surging(twitch, clock, svc, views_now=110)                      # +10 views in an hour
    assert svc.announce_surges()["sent"] == 0 and out.sent == []


def test_the_velocity_bar_is_configurable(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out, surge_min_velocity=1000)
    surging(twitch, clock, svc)
    assert svc.announce_surges()["sent"] == 0


def test_visitor_streamers_never_trigger_alerts(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out)
    bid = twitch.add_streamer("guest")
    twitch.add_clip(bid, "vis", 100, ts(3))
    svc.track_streamer("guest", client_id="v1")
    clock.advance(hours=1)
    twitch.set_views(bid, "vis", 900)
    svc.poll_all()
    assert svc.clips_for("guest")["clips"][0]["trend"] == "up"     # it really is surging...
    assert svc.announce_surges()["sent"] == 0 and out.sent == []   # ...but visitors cannot make the site post


def test_a_clip_that_is_merely_fast_is_not_a_surge(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out)
    bid = twitch.add_streamer("amy")
    twitch.add_clip(bid, "big", 1000, ts(3))                       # already popular: ~200 views/hr on average
    svc.ensure_pinned()
    svc.poll_all()
    clock.advance(hours=1)
    twitch.set_views(bid, "big", 1300)                             # 300/hr now: fast, but not 1.5x its average
    svc.poll_all()
    clip = svc.clips_for("amy")["clips"][0]
    assert clip["trend"] == "steady" and clip["recent_velocity"] >= 30
    assert svc.announce_surges()["sent"] == 0


def test_old_clips_are_not_news(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out, surge_max_age_hours=2)
    surging(twitch, clock, svc, age_hours=3)
    assert svc.announce_surges()["sent"] == 0


def test_at_most_a_few_alerts_per_run_fastest_first(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out, surge_per_run=2)
    bid = surging(twitch, clock, svc, clip_id="c1", views_now=500)
    for i, views in ((2, 900), (3, 700)):
        twitch.add_clip(bid, f"c{i}", 100, ts(3))
    svc.poll_all()
    clock.advance(hours=1)
    for i, views in ((2, 900), (3, 700)):
        twitch.set_views(bid, f"c{i}", views)
    twitch.set_views(bid, "c1", 1500)
    svc.poll_all()
    result = svc.announce_surges()
    # c2 gained 800 in the last hour, c1 gained 1400 over two hours (700/hr), c3 gained 600/hr and misses the cut
    assert result["sent"] == 2 and [a["clip_id"] for a in out.sent] == ["c2", "c1"]


def test_a_daily_cap_stops_the_flood(settings, store, twitch, clock):
    out = Outbox()
    svc = make(settings, store, twitch, clock, out, surge_per_day=1, surge_per_run=5)
    bid = surging(twitch, clock, svc, clip_id="c1", views_now=900)
    result = svc.announce_surges()
    assert result["sent"] == 1
    twitch.add_clip(bid, "c2", 100, ts(3))
    svc.poll_all()
    clock.advance(hours=1)
    twitch.set_views(bid, "c2", 900)
    svc.poll_all()
    capped = svc.announce_surges()
    assert capped["sent"] == 0 and "daily" in capped["errors"][0]


def test_a_broken_queue_is_reported_not_fatal(settings, store, twitch, clock):
    svc = make(settings, store, twitch, clock, Outbox(fail=True))
    surging(twitch, clock, svc)
    result = svc.announce_surges()
    assert result["sent"] == 0 and "queue is down" in result["errors"][0]


def test_run_hourly_only_mentions_alerts_when_they_are_on(settings, store, twitch, clock):
    twitch.add_clip(twitch.add_streamer("amy"), "c1", 10, ts(2))
    off = ClipService(store, lambda: twitch, replace(settings, pinned_streamers=("amy",)), clock=clock)
    assert "alerts" not in off.run_hourly()
    on = make(settings, store, twitch, clock, Outbox())
    assert on.run_hourly()["alerts"]["sent"] == 0


# --- the Discord message -------------------------------------------------------------------------

def alert(**changes):
    base = {"clip_id": "hot", "title": "Great play", "url": "https://clips.twitch.tv/hot", "streamer": "Amy",
            "velocity": 123.4, "views": 3100, "age_hours": 3.2, "thumbnail_url": "https://img/x.jpg"}
    return {**base, **changes}


def test_message_has_the_numbers_and_never_pings_anyone():
    payload = alerts.discord_payload(alert(title="@everyone free stuff"))
    embed = payload["embeds"][0]
    assert payload["allowed_mentions"] == {"parse": []}
    assert "123 views/hr" in embed["description"] and "3.1K views" in embed["description"] and "3h old" in embed["description"]
    assert embed["url"] == "https://clips.twitch.tv/hot" and embed["image"]["url"] == "https://img/x.jpg"


def test_only_twitch_links_and_https_images_are_used():
    embed = alerts.discord_payload(alert(url="https://evil.example/x", thumbnail_url="http://img/x.jpg"))["embeds"][0]
    assert "url" not in embed and "image" not in embed


def test_long_titles_are_cut_to_fit_discord():
    assert len(alerts.discord_payload(alert(title="x" * 1000))["embeds"][0]["title"]) <= 256


def test_make_alert_keeps_only_what_the_notifier_needs():
    clip = {"id": "c1", "title": "t" * 500, "url": "u", "streamer_name": "Amy", "recent_velocity": 40.0,
            "velocity": 40.0, "view_count": 10, "age_hours": 1, "thumbnail_url": None, "history": [[1, 2]], "extra": 1}
    made = alerts.make_alert(clip)
    assert set(made) == {"clip_id", "title", "url", "streamer", "velocity", "views", "age_hours", "thumbnail_url"}
    assert len(made["title"]) == 140


# --- the notifier --------------------------------------------------------------------------------

def poster(status, calls=None):
    def post(url, body, headers):
        if calls is not None:
            calls.append((url, json.loads(body), headers))
        return status, {}
    return post


def test_notifier_posts_with_our_own_user_agent():
    calls = []
    out = alerts.notify_handler(alert(), None, webhook=lambda: DISCORD, post=poster(204, calls))
    assert out == {"posted": True, "clip_id": "hot"}
    url, body, headers = calls[0]
    assert url == DISCORD and body["embeds"] and "ClipPulse" in headers["user-agent"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_busy_discord_is_retryable(status):
    with pytest.raises(DiscordRetryable):
        alerts.notify_handler(alert(), None, webhook=lambda: DISCORD, post=poster(status))


@pytest.mark.parametrize("status", [400, 401, 404])
def test_rejected_messages_are_not_retried(status):
    with pytest.raises(DiscordRejected):
        alerts.notify_handler(alert(), None, webhook=lambda: DISCORD, post=poster(status))


@pytest.mark.parametrize("url", ["", "http://discord.com/api/webhooks/1/a", "https://evil.example/api/webhooks/1/a",
                                 "https://discord.com.evil.example/api/webhooks/1/a"])
def test_only_real_discord_webhooks_are_accepted(url):
    calls = []
    with pytest.raises(DiscordRejected):
        alerts.notify_handler(alert(), None, webhook=lambda: url, post=poster(204, calls))
    assert calls == []                                   # nothing is ever sent to another host


def test_the_state_machine_retries_only_what_the_notifier_calls_retryable():
    """The Lambda runtime reports an exception by its class name, and Step Functions matches on that name.
    DiscordRetryable must be in the Retry list; DiscordRejected must not be, so a bad webhook fails at once."""
    from pathlib import Path
    template = (Path(__file__).resolve().parents[2] / "template.yaml").read_text()
    assert alerts.DiscordRetryable.__name__ in template
    assert alerts.DiscordRejected.__name__ not in template


# --- the starter ---------------------------------------------------------------------------------

class FakeSfn:
    def __init__(self, fail_on=None, duplicate=()):
        self.started, self.fail_on, self.duplicate = [], fail_on, set(duplicate)

    def start_execution(self, stateMachineArn, name, input):
        if name in self.duplicate:
            raise type("ExecutionAlreadyExists", (Exception,), {})("exists")
        if name == self.fail_on:
            raise RuntimeError("throttled")
        self.started.append((stateMachineArn, name, json.loads(input)))


def records(*clip_ids):
    return {"Records": [{"messageId": f"m-{c}", "body": json.dumps(alert(clip_id=c))} for c in clip_ids]}


def test_starter_begins_one_execution_per_message():
    sfn = FakeSfn()
    out = alerts.start_handler(records("a", "b"), None, sfn=sfn, state_machine_arn="arn:x")
    assert out == {"batchItemFailures": []}
    assert [n for _, n, _ in sfn.started] == ["surge-a", "surge-b"] and sfn.started[0][0] == "arn:x"


def test_a_duplicate_message_is_not_an_error_and_not_a_second_post():
    sfn = FakeSfn(duplicate={"surge-a"})
    out = alerts.start_handler(records("a"), None, sfn=sfn, state_machine_arn="arn:x")
    assert out == {"batchItemFailures": []} and sfn.started == []


def test_only_failed_messages_go_back_to_the_queue():
    sfn = FakeSfn(fail_on="surge-b")
    out = alerts.start_handler(records("a", "b", "c"), None, sfn=sfn, state_machine_arn="arn:x")
    assert out == {"batchItemFailures": [{"itemIdentifier": "m-b"}]}
    assert [n for _, n, _ in sfn.started] == ["surge-a", "surge-c"]


def test_execution_names_are_valid_and_short():
    name = alerts.execution_name("Funny Clip/id?" + "x" * 200)
    assert len(name) <= 80 and all(ch.isalnum() or ch in "-_" for ch in name)


# --- the publisher -------------------------------------------------------------------------------

def test_publisher_sends_the_alert_as_json():
    class Sqs:
        def __init__(self):
            self.calls = []

        def send_message(self, **kwargs):
            self.calls.append(kwargs)
    sqs = Sqs()
    SqsPublisher("https://sqs/q", sqs).publish(alert())
    assert sqs.calls[0]["QueueUrl"] == "https://sqs/q" and json.loads(sqs.calls[0]["MessageBody"])["clip_id"] == "hot"
