import pytest

from clippulse.scoring import (average_velocity, clip_metrics, format_ts, parse_ts, rank,
                               recent_velocity, trend_label)
from fakes import NOW, ts


def hist(*pairs):
    """pairs of (hours_ago, views) -> history list, oldest first."""
    return [[ts(h), v] for h, v in sorted(pairs, reverse=True)]


def test_timestamp_round_trip():
    assert format_ts(parse_ts("2026-11-02T12:00:00Z")) == "2026-11-02T12:00:00Z"


def test_prior_keeps_brand_new_clips_from_outranking_proven_ones():
    brand_new = average_velocity(4, 0.05)      # 4 views after three minutes
    proven = average_velocity(900, 24)
    assert proven > brand_new


def test_average_velocity_known_value():
    assert average_velocity(100, 3) == pytest.approx(20.0)  # 100 / (3 + 2)


def test_recent_velocity_uses_growth_between_snapshots():
    history = hist((2, 100), (1, 160), (0, 280))
    assert recent_velocity(history, NOW) == pytest.approx(90.0)   # (280 - 100) / 2h


def test_recent_velocity_ignores_snapshots_older_than_the_window():
    history = hist((10, 0), (5, 50), (0, 100))  # only the last 6h count: (100 - 50) / 5h
    assert recent_velocity(history, NOW) == pytest.approx(10.0)


def test_recent_velocity_needs_two_snapshots():
    assert recent_velocity(hist((0, 100)), NOW) is None
    assert recent_velocity([], NOW) is None


def test_recent_velocity_is_none_when_snapshots_are_too_close_together():
    assert recent_velocity(hist((0.2, 100), (0, 110)), NOW) is None


def test_recent_velocity_is_none_when_data_is_stale():
    assert recent_velocity(hist((9, 100), (8, 150)), NOW) is None


def test_recent_velocity_falls_back_to_previous_point_when_window_has_only_the_newest():
    history = hist((20, 10), (0, 110))  # gap larger than the window
    assert recent_velocity(history, NOW) == pytest.approx(5.0)


def test_recent_velocity_never_negative():
    assert recent_velocity(hist((2, 500), (0, 480)), NOW) == 0


def test_recent_velocity_sorts_unordered_history():
    shuffled = [[ts(0), 280], [ts(2), 100], [ts(1), 160]]
    assert recent_velocity(shuffled, NOW) == pytest.approx(90.0)


@pytest.mark.parametrize("recent,average,expected", [
    (None, 5, None), (30, 10, "up"), (0.9, 0.1, "steady"), (1.0, 0.1, "up"),
    (1, 10, "cooling"), (10, 10, "steady"),
])
def test_trend_label(recent, average, expected):
    assert trend_label(recent, average) == expected


def test_clip_metrics_prefers_recent_velocity():
    clip = {"created_at": ts(3), "view_count": 280, "history": hist((2, 100), (1, 160), (0, 280))}
    m = clip_metrics(clip, NOW)
    assert m["recent_velocity"] == pytest.approx(90.0)
    assert m["velocity"] == m["recent_velocity"]
    assert m["average_velocity"] == pytest.approx(280 / 5, abs=0.01)
    assert m["trend"] == "up"
    assert m["age_hours"] == pytest.approx(3.0)


def test_clip_metrics_falls_back_to_average_without_history():
    m = clip_metrics({"created_at": ts(3), "view_count": 100}, NOW)
    assert m["recent_velocity"] is None and m["trend"] is None
    assert m["velocity"] == m["average_velocity"] == pytest.approx(20.0)


def test_clip_created_in_the_future_has_zero_age():
    m = clip_metrics({"created_at": ts(-1), "view_count": 10}, NOW)
    assert m["age_hours"] == 0


def _c(i, velocity, views, created=0):
    return {"id": i, "velocity": velocity, "view_count": views, "created_at": ts(created)}


def test_rank_by_velocity_breaks_ties_on_views_then_id():
    clips = [_c("b", 5, 10), _c("a", 5, 10), _c("c", 5, 99), _c("d", 9, 1)]
    assert [c["id"] for c in rank(clips, "velocity")] == ["d", "c", "a", "b"]


def test_rank_by_views_and_by_new():
    clips = [_c("a", 1, 5, created=3), _c("b", 1, 50, created=1), _c("c", 1, 20, created=2)]
    assert [c["id"] for c in rank(clips, "views")] == ["b", "c", "a"]
    assert [c["id"] for c in rank(clips, "new")] == ["b", "c", "a"]


def test_rank_rejects_unknown_sort():
    with pytest.raises(ValueError):
        rank([], "random")
