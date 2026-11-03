"""Runtime settings, read from environment variables set by the SAM template."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class Settings:
    table_name: str = "clippulse"
    client_id_param: str = "/clippulse/twitch_client_id"
    client_secret_param: str = "/clippulse/twitch_client_secret"
    pinned_streamers: Tuple[str, ...] = ()   # always tracked, never expire (set by the PinnedStreamers template parameter)
    max_temp_streamers: int = 10     # visitor-added streamers tracked at once: caps Twitch calls and DynamoDB writes
    temp_ttl_hours: int = 48         # a visitor-added streamer is dropped this long after anyone last looked at them
    temp_min_keep_hours: int = 6     # ...and when the pool is full, only streamers idle this long can be replaced
    touch_interval_minutes: int = 60 # looking at a streamer extends their stay, but writes at most this often
    max_per_client: int = 3          # visitor streamers one visitor (hashed IP) can have added at a time
    searches_per_client: int = 30    # every search, tracked streamer or not, per visitor per client window
    removals_per_client: int = 10    # removals per visitor per client window
    client_salt: str = ""            # mixed into the IP hash so stored ids cannot be reversed with a table of IPs
    lookups_per_client: int = 5      # brand-new streamer lookups allowed per visitor (hashed IP) per window
    client_window_seconds: int = 600
    lookups_global: int = 30         # brand-new streamer lookups allowed for everyone together per window
    global_window_seconds: int = 3600
    surge_queue_url: str = ""        # empty turns surge alerts off (set by the template when a Discord webhook is configured)
    surge_min_velocity: float = 30.0 # views per hour a clip must be gaining before it is worth announcing
    surge_max_age_hours: int = 48    # older clips are not news
    surge_per_run: int = 5           # most alerts one hourly run can send
    surge_per_day: int = 20          # most alerts per day, however many clips surge
    surge_dedupe_days: int = 14      # a clip is announced once in this long
    clips_per_streamer: int = 100    # Twitch returns most-viewed first, so one page is the top 100
    lookback_days: int = 7           # how far back the poller asks Twitch for clips
    fresh_window_hours: int = 6      # second, short query so brand-new low-view clips are not crowded out
    history_points: int = 48         # view-count snapshots kept per clip (two days hourly)
    clip_ttl_days: int = 14          # DynamoDB TTL, refreshed every time a clip is polled
    board_ttl_days: int = 180        # shotlist items expire if the board is never touched
    max_board_items: int = 200


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    defaults = Settings()

    def num(name: str, default: int) -> int:
        raw = env.get(name)
        return int(raw) if raw not in (None, "") else default

    return Settings(
        table_name=env.get("TABLE_NAME", defaults.table_name),
        client_id_param=env.get("TWITCH_CLIENT_ID_PARAM", defaults.client_id_param),
        client_secret_param=env.get("TWITCH_CLIENT_SECRET_PARAM", defaults.client_secret_param),
        pinned_streamers=tuple(
            name.strip().lower().lstrip("@") for name in env.get("PINNED_STREAMERS", "").split(",") if name.strip()),
        max_temp_streamers=num("MAX_STREAMERS", defaults.max_temp_streamers),   # env name kept for existing stacks
        temp_ttl_hours=num("TEMP_TTL_HOURS", defaults.temp_ttl_hours),
        temp_min_keep_hours=num("TEMP_MIN_KEEP_HOURS", defaults.temp_min_keep_hours),
        touch_interval_minutes=num("TOUCH_INTERVAL_MINUTES", defaults.touch_interval_minutes),
        max_per_client=num("MAX_PER_CLIENT", defaults.max_per_client),
        searches_per_client=num("SEARCHES_PER_CLIENT", defaults.searches_per_client),
        removals_per_client=num("REMOVALS_PER_CLIENT", defaults.removals_per_client),
        client_salt=env.get("CLIENT_SALT", defaults.client_salt),
        lookups_per_client=num("LOOKUPS_PER_CLIENT", defaults.lookups_per_client),
        surge_queue_url=env.get("SURGE_QUEUE_URL", defaults.surge_queue_url),
        surge_min_velocity=float(env.get("SURGE_MIN_VELOCITY") or defaults.surge_min_velocity),
        surge_max_age_hours=num("SURGE_MAX_AGE_HOURS", defaults.surge_max_age_hours),
        surge_per_run=num("SURGE_PER_RUN", defaults.surge_per_run),
        surge_per_day=num("SURGE_PER_DAY", defaults.surge_per_day),
        surge_dedupe_days=num("SURGE_DEDUPE_DAYS", defaults.surge_dedupe_days),
        client_window_seconds=num("CLIENT_WINDOW_SECONDS", defaults.client_window_seconds),
        lookups_global=num("LOOKUPS_GLOBAL", defaults.lookups_global),
        global_window_seconds=num("GLOBAL_WINDOW_SECONDS", defaults.global_window_seconds),
        clips_per_streamer=num("CLIPS_PER_STREAMER", defaults.clips_per_streamer),
        lookback_days=num("LOOKBACK_DAYS", defaults.lookback_days),
        fresh_window_hours=num("FRESH_WINDOW_HOURS", defaults.fresh_window_hours),
        history_points=num("HISTORY_POINTS", defaults.history_points),
        clip_ttl_days=num("CLIP_TTL_DAYS", defaults.clip_ttl_days),
        board_ttl_days=num("BOARD_TTL_DAYS", defaults.board_ttl_days),
        max_board_items=num("MAX_BOARD_ITEMS", defaults.max_board_items),
    )
