"""Clip velocity math. Pure functions, no I/O, so the ranking rules are easy to test.

Two numbers describe how fast a clip is gaining views, both in views per hour:

* average velocity: total views divided by (age + PRIOR_HOURS). The prior keeps a
  three-minute-old clip with 4 views from outranking a day-old clip with 900.
* recent velocity: growth between our own hourly snapshots over the last few hours.
  This is what actually says "rising right now". It needs at least two snapshots.

The ranking value is recent velocity when we have it, otherwise average velocity.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence, Tuple

PRIOR_HOURS = 2.0
RECENT_WINDOW_HOURS = 6.0
MIN_SPAN_HOURS = 0.5
MAX_STALENESS_HOURS = 3.0

History = Sequence[Tuple[str, int]]


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_hours(created_at: str, now: datetime) -> float:
    return max(0.0, (now - parse_ts(created_at)).total_seconds() / 3600)


def average_velocity(views: int, age_h: float) -> float:
    return views / (age_h + PRIOR_HOURS)


def recent_velocity(
    history: History,
    now: datetime,
    window_hours: float = RECENT_WINDOW_HOURS,
    max_staleness_hours: float = MAX_STALENESS_HOURS,
) -> Optional[float]:
    """Views per hour gained across our latest snapshots, or None if we can't tell."""
    if len(history) < 2:
        return None
    points = sorted((parse_ts(ts), int(views)) for ts, views in history)
    last_t, last_v = points[-1]
    if (now - last_t) > timedelta(hours=max_staleness_hours):
        return None  # poller has not seen this clip lately, so the trend is out of date
    cutoff = last_t - timedelta(hours=window_hours)
    in_window = [p for p in points if p[0] >= cutoff]
    first_t, first_v = in_window[0]
    if first_t == last_t:  # only the newest snapshot is inside the window
        first_t, first_v = points[-2]
    span = (last_t - first_t).total_seconds() / 3600
    if span < MIN_SPAN_HOURS:
        return None
    return max(0, last_v - first_v) / span


def trend_label(recent: Optional[float], average: float) -> Optional[str]:
    if recent is None:
        return None
    if recent >= 1.0 and recent >= 1.5 * average:
        return "up"
    if recent <= 0.5 * average:
        return "cooling"
    return "steady"


def clip_metrics(clip: dict, now: datetime) -> dict:
    """Return the derived numbers the API shows next to a stored clip."""
    age_h = age_hours(clip["created_at"], now)
    views = int(clip["view_count"])
    average = average_velocity(views, age_h)
    recent = recent_velocity(clip.get("history", []), now)
    return {
        "age_hours": round(age_h, 2),
        "average_velocity": round(average, 2),
        "recent_velocity": None if recent is None else round(recent, 2),
        "velocity": round(recent if recent is not None else average, 2),
        "trend": trend_label(recent, average),
    }


def rank(clips: Iterable[dict], sort: str) -> list:
    """Sort enriched clips. Ties break on views, then id, so output is deterministic."""
    keys = {
        "velocity": lambda c: (-c["velocity"], -c["view_count"], c["id"]),
        "views": lambda c: (-c["view_count"], c["id"]),
        "new": lambda c: (_neg_ts(c["created_at"]), c["id"]),
    }
    if sort not in keys:
        raise ValueError(f"unknown sort: {sort}")
    return sorted(clips, key=keys[sort])


def _neg_ts(value: str) -> float:
    return -parse_ts(value).timestamp()
