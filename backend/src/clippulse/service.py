"""Business logic. Knows nothing about HTTP or AWS: it takes a Store and a Twitch
client factory, so the same code runs in Lambda, in tests, and in the dev server."""
from __future__ import annotations

import csv
import io
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .config import Settings
from .scoring import clip_metrics, format_ts, parse_ts, rank
from .store import Store, ttl_from_now
from .twitch import TwitchError, TwitchRateLimited

logger = logging.getLogger("clippulse.service")

LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")
BOARD_RE = re.compile(r"^[0-9a-f]{32}$")  # a uuid4 hex string: unguessable, so it acts as the key
CLIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
MAX_NOTE_LENGTH = 500
HISTORY_SHOWN = 24  # snapshots sent to the browser per clip (a day of hourly polls)
SORTS = ("velocity", "views", "new")


class ServiceError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(ServiceError):
    status, code = 400, "invalid_input"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class LimitReached(ServiceError):
    status, code = 409, "limit_reached"


class RateLimited(ServiceError):
    status, code = 429, "rate_limited"

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamError(ServiceError):
    status, code = 502, "twitch_unavailable"


class UpstreamBusy(ServiceError):
    status, code = 503, "twitch_busy"


class ClipService:
    def __init__(self, store: Store, twitch_provider: Callable[[], Any], settings: Settings,
                 clock: Optional[Callable[[], datetime]] = None):
        self.store = store
        self._twitch_provider = twitch_provider
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return self._clock()

    # -- validation -------------------------------------------------------
    @staticmethod
    def normalize_login(raw: Any) -> str:
        login = raw.strip().lower().lstrip("@") if isinstance(raw, str) else ""
        if not LOGIN_RE.match(login):
            raise ValidationError("Enter a Twitch username: 3 to 25 letters, numbers or underscores.")
        return login

    @staticmethod
    def validate_board_id(board_id: str) -> str:
        if not isinstance(board_id, str) or not BOARD_RE.match(board_id):
            raise ValidationError("Invalid board id.")
        return board_id

    # -- ingest -----------------------------------------------------------
    def ingest(self, streamer: dict) -> int:
        """Snapshot one streamer's clips.

        Twitch returns clips most-viewed first and we only read one page, so a busy channel's
        brand-new clips (the ones we care about most) could be crowded out by older hits.
        A second, short-window query guarantees the freshest clips are always captured.
        """
        now = self._now()
        twitch = self._twitch_provider()
        cap = self.settings.clips_per_streamer
        week = twitch.get_clips(
            streamer["broadcaster_id"], format_ts(now - timedelta(days=self.settings.lookback_days)), max_items=cap)
        fresh = twitch.get_clips(
            streamer["broadcaster_id"], format_ts(now - timedelta(hours=self.settings.fresh_window_hours)), max_items=cap)
        clips = list({clip["id"]: clip for clip in [*week, *fresh]}.values())
        self.store.upsert_clips(
            streamer["broadcaster_id"], clips, format_ts(now), self.settings.history_points,
            ttl_from_now(self.settings.clip_ttl_days, now.timestamp()))
        self.store.put_streamer({**streamer, "last_polled_at": format_ts(now)})
        return len(clips)

    def poll_all(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"streamers": 0, "clips": 0, "errors": []}
        for streamer in self._active_streamers():
            try:
                summary["clips"] += self.ingest(streamer)
                summary["streamers"] += 1
            except TwitchRateLimited as err:
                summary["errors"].append({"login": streamer["login"], "error": str(err)})
                break  # every remaining call would be refused too
            except TwitchError as err:
                summary["errors"].append({"login": streamer["login"], "error": str(err)})
        return summary

    def run_hourly(self) -> Dict[str, Any]:
        """What the scheduled poller does: apply the pinned list, drop expired visitor streamers, snapshot the rest."""
        pinned = self.ensure_pinned()
        pruned = self.prune_expired()
        summary = self.poll_all()
        summary["errors"] = pinned.pop("errors") + summary["errors"]
        return {**summary, "pinned": pinned, "pruned": pruned}

    # -- who is tracked ---------------------------------------------------
    # Two kinds of streamer share one table:
    #   pinned     chosen by the site owner (PinnedStreamers), never expire
    #   temporary  added by any visitor's search; dropped `temp_ttl_hours` after anyone last looked at them
    # Items saved before this existed have neither flag; they count as pinned until the next hourly run sorts them.
    @staticmethod
    def _is_pinned(streamer: dict) -> bool:
        return bool(streamer.get("pinned", streamer.get("expires_at") is None))

    def _is_active(self, streamer: dict, now: datetime) -> bool:
        return self._is_pinned(streamer) or streamer["expires_at"] > format_ts(now)

    def _active_streamers(self) -> List[dict]:
        now = self._now()
        return [s for s in self.store.list_streamers() if self._is_active(s, now)]

    def _temporary_stay(self, now: datetime) -> Dict[str, Any]:
        expires = now + timedelta(hours=self.settings.temp_ttl_hours)
        # `ttl` lets DynamoDB clean the item up eventually; correctness never depends on it (expires_at does).
        return {"pinned": False, "last_seen_at": format_ts(now), "expires_at": format_ts(expires),
                "ttl": int(expires.timestamp()) + 7 * 86400}

    def _touch(self, streamer: dict, now: datetime) -> dict:
        """Looking at a temporary streamer extends their stay. Rewritten at most once per interval."""
        if self._is_pinned(streamer):
            return streamer
        seen = streamer.get("last_seen_at")
        if seen and now - parse_ts(seen) < timedelta(minutes=self.settings.touch_interval_minutes):
            return streamer
        streamer = {**streamer, **self._temporary_stay(now)}
        self.store.put_streamer(streamer)
        return streamer

    def ensure_pinned(self) -> Dict[str, Any]:
        """Make the store match the PinnedStreamers setting: add or promote what is listed, demote what is not."""
        now = self._now()
        wanted = []
        for raw in self.settings.pinned_streamers:
            try:
                wanted.append(self.normalize_login(raw))
            except ValidationError:
                logger.warning("ignoring invalid pinned streamer %r", raw)
        result: Dict[str, Any] = {"ensured": 0, "demoted": 0, "errors": []}
        for streamer in self.store.list_streamers():
            if self._is_pinned(streamer) and streamer["login"] not in wanted:
                self.store.put_streamer({**streamer, **self._temporary_stay(now)})
                result["demoted"] += 1
        for login in wanted:
            try:
                existing = self.store.get_streamer(login)
                if existing:
                    if not existing.get("pinned") or "expires_at" in existing:
                        pinned = {k: v for k, v in existing.items() if k not in ("expires_at", "last_seen_at", "ttl")}
                        self.store.put_streamer({**pinned, "pinned": True})
                else:
                    user = self._twitch_provider().get_user(login)
                    if not user:
                        result["errors"].append({"login": login, "error": "no such Twitch channel"})
                        continue
                    streamer = {**user, "added_at": format_ts(now), "last_polled_at": None, "pinned": True}
                    self.store.put_streamer(streamer)
                    self.ingest(streamer)
                result["ensured"] += 1
            except TwitchError as err:
                result["errors"].append({"login": login, "error": str(err)})
        return result

    def prune_expired(self) -> int:
        now = self._now()
        gone = [s["login"] for s in self.store.list_streamers() if not self._is_active(s, now)]
        for login in gone:
            self.store.delete_streamer(login)
        return len(gone)

    # -- streamers --------------------------------------------------------
    def track_streamer(self, raw_login: Any, client_id: Optional[str] = None) -> Dict[str, Any]:
        """Search for a streamer. Anyone can; new ones are tracked for a while, within hard limits."""
        login = self.normalize_login(raw_login)
        now = self._now()
        existing = self.store.get_streamer(login)
        if existing and self._is_active(existing, now):
            existing = self._touch(existing, now)
            return {"streamer": _public_streamer(existing), "created": False, "clips_ingested": 0}

        # A streamer we are not tracking: this is the expensive path, so every limit applies here.
        victim = self._room_for_one_more(now)
        self._take_lookup(client_id or "unknown", now)
        try:
            user = self._twitch_provider().get_user(login)
        except TwitchRateLimited:
            raise UpstreamBusy("Twitch is rate limiting us. Try again in a minute.")
        except TwitchError:
            raise UpstreamError("Could not reach Twitch. Try again shortly.")
        if not user:
            raise NotFound(f"No Twitch channel named {login}.")
        if victim:
            self.store.delete_streamer(victim["login"])
        streamer = {**user, "added_at": format_ts(now), "last_polled_at": None, **self._temporary_stay(now)}
        self.store.put_streamer(streamer)
        try:
            ingested = self.ingest(streamer)
        except TwitchError:
            ingested = 0  # the hourly poll will fill it in
        return {"streamer": _public_streamer(self.store.get_streamer(login) or streamer),
                "created": True, "clips_ingested": ingested}

    def _room_for_one_more(self, now: datetime) -> Optional[dict]:
        """None when there is a free slot; otherwise the idle streamer to replace, or LimitReached."""
        temps = [s for s in self._active_streamers() if not self._is_pinned(s)]
        if len(temps) < self.settings.max_temp_streamers:
            return None
        cutoff = format_ts(now - timedelta(hours=self.settings.temp_min_keep_hours))
        idle = [s for s in temps if s.get("last_seen_at", "") <= cutoff]
        if not idle:
            raise LimitReached("ClipPulse is already tracking as many visitor-added streamers as it allows, "
                               "and all of them were looked at recently. Try one of the streamers already "
                               "listed, or come back in a few hours.")
        return min(idle, key=lambda s: (s.get("last_seen_at", ""), s["login"]))

    def _take_lookup(self, client_id: str, now: datetime) -> None:
        cfg, t = self.settings, now.timestamp()
        if not self.store.take_token(f"lookup-client-{client_id}", cfg.client_window_seconds, cfg.lookups_per_client, t):
            wait = _seconds_left(t, cfg.client_window_seconds)
            raise RateLimited(f"You are adding streamers too quickly. Try again in {_wait_text(wait)}.", wait)
        if not self.store.take_token("lookup-global", cfg.global_window_seconds, cfg.lookups_global, t):
            wait = _seconds_left(t, cfg.global_window_seconds)
            raise RateLimited("ClipPulse has looked up a lot of new streamers in the last hour. "
                              f"Try again in {_wait_text(wait)}.", wait)

    def list_streamers(self) -> List[dict]:
        rows = self._active_streamers()
        rows.sort(key=lambda s: (not self._is_pinned(s), s["login"]))
        return [_public_streamer(s) for s in rows]

    # -- reading clips ----------------------------------------------------
    def clips_for(self, raw_login: Any, days: int = 7, sort: str = "velocity", limit: int = 50) -> Dict[str, Any]:
        login = self.normalize_login(raw_login)
        sort = _check_sort(sort)
        days = _clamp(days, 1, self.settings.lookback_days)
        limit = _clamp(limit, 1, 100)
        now = self._now()
        streamer = self.store.get_streamer(login)
        if not streamer or not self._is_active(streamer, now):
            raise NotFound(f"{login} is not being tracked right now.")
        streamer = self._touch(streamer, now)
        rows = self.store.query_clips(streamer["broadcaster_id"], format_ts(now - timedelta(days=days)))
        clips = rank((_enrich(c, streamer, now) for c in rows), sort)[:limit]
        return {"streamer": _public_streamer(streamer), "as_of": streamer.get("last_polled_at"), "clips": clips}

    def rising(self, hours: int = 48, limit: int = 20) -> Dict[str, Any]:
        hours = _clamp(hours, 1, self.settings.lookback_days * 24)
        limit = _clamp(limit, 1, 100)
        now = self._now()
        since = format_ts(now - timedelta(hours=hours))
        pool: List[dict] = []
        newest_poll: Optional[str] = None
        for streamer in self._active_streamers():
            polled = streamer.get("last_polled_at")
            if polled and (newest_poll is None or polled > newest_poll):
                newest_poll = polled
            for row in self.store.query_clips(streamer["broadcaster_id"], since):
                pool.append(_enrich(row, streamer, now))
        return {"as_of": newest_poll, "clips": rank(pool, "velocity")[:limit]}

    # -- shotlist boards --------------------------------------------------
    def get_board(self, board_id: str) -> Dict[str, Any]:
        return {"board_id": self.validate_board_id(board_id), "items": self._sorted_items(board_id)}

    def _sorted_items(self, board_id: str) -> List[dict]:
        items = [_public_item(i) for i in self.store.list_board_items(board_id)]
        items.sort(key=lambda i: (i["added_at"], i["clip_id"]))
        return items

    def save_item(self, board_id: str, clip_id: str, streamer_login: Optional[str], note: Optional[str]) -> dict:
        self.validate_board_id(board_id)
        if not isinstance(clip_id, str) or not CLIP_ID_RE.match(clip_id):
            raise ValidationError("Invalid clip id.")
        if note is not None:
            if not isinstance(note, str):
                raise ValidationError("Note must be text.")
            note = note.strip()
            if len(note) > MAX_NOTE_LENGTH:
                raise ValidationError(f"Notes can be at most {MAX_NOTE_LENGTH} characters.")
        now = self._now()
        ttl = ttl_from_now(self.settings.board_ttl_days, now.timestamp())

        existing = self.store.get_board_item(board_id, clip_id)
        if existing:
            if note is not None:
                existing["note"] = note
            existing["ttl"] = ttl
            self.store.put_board_item(board_id, existing)
            return _public_item(existing)

        if len(self.store.list_board_items(board_id)) >= self.settings.max_board_items:
            raise LimitReached(f"A shotlist holds at most {self.settings.max_board_items} clips.")
        login = self.normalize_login(streamer_login)
        streamer = self.store.get_streamer(login)
        if not streamer:
            raise NotFound(f"{login} is not tracked yet.")
        since = format_ts(now - timedelta(days=self.settings.lookback_days))
        clip = next((c for c in self.store.query_clips(streamer["broadcaster_id"], since) if c["id"] == clip_id), None)
        if not clip:
            raise NotFound("That clip is not in ClipPulse's recent data.")
        item = {
            "clip_id": clip["id"], "streamer": streamer["login"], "streamer_name": streamer["display_name"],
            "title": clip["title"], "url": clip["url"], "thumbnail_url": clip["thumbnail_url"],
            "clip_created_at": clip["created_at"], "duration": clip["duration"],
            "vod_offset": clip.get("vod_offset"), "views_at_save": clip["view_count"],
            "note": note or "", "added_at": format_ts(now), "ttl": ttl,
        }
        self.store.put_board_item(board_id, item)
        return _public_item(item)

    def remove_item(self, board_id: str, clip_id: str) -> bool:
        self.validate_board_id(board_id)
        if not isinstance(clip_id, str) or not CLIP_ID_RE.match(clip_id):
            raise ValidationError("Invalid clip id.")
        return self.store.delete_board_item(board_id, clip_id)

    def export_csv(self, board_id: str) -> str:
        self.validate_board_id(board_id)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["streamer", "title", "clip_url", "clip_created_at", "clip_length_s",
                         "vod_offset_s", "views_when_saved", "note", "saved_at"])
        for i in self._sorted_items(board_id):
            writer.writerow([csv_safe(v) for v in (
                i["streamer_name"], i["title"], i["url"], i["clip_created_at"], i["duration"],
                "" if i["vod_offset"] is None else i["vod_offset"], i["views_at_save"], i["note"], i["added_at"])])
        return out.getvalue()


# -- helpers ----------------------------------------------------------------

def csv_safe(value: Any) -> Any:
    """Stop spreadsheet apps from running a clip title like =HYPERLINK(...) as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _check_sort(sort: str) -> str:
    if sort not in SORTS:
        raise ValidationError(f"sort must be one of: {', '.join(SORTS)}.")
    return sort


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError("Expected a whole number.")
    return max(low, min(high, number))


def _public_streamer(streamer: dict) -> dict:
    keys = ("login", "display_name", "profile_image_url", "last_polled_at")
    public = {k: streamer.get(k) for k in keys}
    public["pinned"] = ClipService._is_pinned(streamer)
    return public


def _seconds_left(now: float, window_seconds: int) -> int:
    return max(1, math.ceil(window_seconds - (now % window_seconds)))


def _wait_text(seconds: int) -> str:
    return f"{seconds} seconds" if seconds < 90 else f"{math.ceil(seconds / 60)} minutes"


def _public_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "ttl"}


def _enrich(clip: dict, streamer: dict, now: datetime) -> dict:
    public = {k: clip.get(k) for k in (
        "id", "url", "title", "creator_name", "view_count", "created_at",
        "thumbnail_url", "duration", "vod_offset")}
    public["streamer"] = streamer["login"]
    public["streamer_name"] = streamer["display_name"]
    # Recent view-count snapshots as [timestamp, views] pairs, so the UI can draw a growth chart.
    public["history"] = [[ts, int(views)] for ts, views in clip.get("history", [])][-HISTORY_SHOWN:]
    public.update(clip_metrics(clip, now))
    return public
