"""Test doubles: a clock, a scriptable Twitch, and a small in-process DynamoDB table
that mimics the parts of boto3's Table API the store uses (including its rules:
floats are rejected, numbers come back as Decimal, queries paginate)."""
import copy
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from clippulse.scoring import format_ts
from clippulse.twitch import TwitchError, TwitchRateLimited


NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeTwitch:
    def __init__(self):
        self.users = {}
        self.clips = {}
        self.fail_for = {}        # broadcaster_id -> exception to raise from get_clips
        self.user_error = None
        self.calls = []

    def add_streamer(self, login, broadcaster_id=None, display=None):
        bid = broadcaster_id or str(1000 + len(self.users))
        self.users[login] = {"login": login, "broadcaster_id": bid,
                             "display_name": display or login.title(), "profile_image_url": f"https://img/{login}.png"}
        self.clips.setdefault(bid, [])
        return bid

    def add_clip(self, bid, clip_id, views, created_at, title=None, duration=12.5):
        self.clips[bid].append({
            "id": clip_id, "url": f"https://clips.twitch.tv/{clip_id}", "title": title or f"Clip {clip_id}",
            "broadcaster_id": bid, "broadcaster_name": "x", "creator_name": "fan", "game_id": "1",
            "language": "en", "view_count": views, "created_at": created_at,
            "thumbnail_url": f"https://thumb/{clip_id}.jpg", "duration": duration, "vod_offset": 90})

    def set_views(self, bid, clip_id, views):
        next(c for c in self.clips[bid] if c["id"] == clip_id)["view_count"] = views

    def get_user(self, login):
        self.calls.append(("get_user", login))
        if self.user_error:
            raise self.user_error
        return copy.deepcopy(self.users.get(login))

    def get_clips(self, broadcaster_id, started_at, max_items=100):
        self.calls.append(("get_clips", broadcaster_id))
        if broadcaster_id in self.fail_for:
            raise self.fail_for[broadcaster_id]
        rows = [c for c in self.clips.get(broadcaster_id, []) if c["created_at"] >= started_at]
        rows.sort(key=lambda c: -c["view_count"])
        return copy.deepcopy(rows[:max_items])


class FakeTable:
    """Supports exactly the key-condition shapes DynamoStore uses and fails loudly on others."""

    PATTERNS = [
        (re.compile(r"^pk = :pk$"), lambda m, v: (lambda sk: True)),
        (re.compile(r"^pk = :pk AND begins_with\(sk, (:\w+)\)$"),
         lambda m, v: (lambda sk: sk.startswith(v[m.group(1)]))),
        (re.compile(r"^pk = :pk AND sk BETWEEN (:\w+) AND (:\w+)$"),
         lambda m, v: (lambda sk: v[m.group(1)] <= sk <= v[m.group(2)])),
    ]

    def __init__(self, page_size=1000):
        self.items = {}
        self.page_size = page_size

    @staticmethod
    def _typed(value):
        if isinstance(value, float):
            raise TypeError("Float types are not supported. Use Decimal types instead.")
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, dict):
            return {k: FakeTable._typed(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [FakeTable._typed(v) for v in value]
        return value  # Decimal passes through

    def put_item(self, Item):
        stored = self._typed(copy.deepcopy(Item))
        self.items[(stored["pk"], stored["sk"])] = stored
        return {}

    def get_item(self, Key):
        found = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": copy.deepcopy(found)} if found else {}

    def delete_item(self, Key, ReturnValues=None):
        old = self.items.pop((Key["pk"], Key["sk"]), None)
        return {"Attributes": copy.deepcopy(old)} if (old and ReturnValues == "ALL_OLD") else {}

    def query(self, KeyConditionExpression, ExpressionAttributeValues, ScanIndexForward=True,
              ExclusiveStartKey=None):
        predicate = None
        for pattern, build in self.PATTERNS:
            match = pattern.match(KeyConditionExpression)
            if match:
                predicate = build(match, ExpressionAttributeValues)
                break
        if predicate is None:
            raise AssertionError(f"FakeTable does not support: {KeyConditionExpression}")
        pk = ExpressionAttributeValues[":pk"]
        rows = sorted((v for (p, s), v in self.items.items() if p == pk and predicate(s)),
                      key=lambda r: r["sk"], reverse=not ScanIndexForward)
        if ExclusiveStartKey:
            marker = ExclusiveStartKey["sk"]
            rows = [r for r in rows if (r["sk"] < marker if not ScanIndexForward else r["sk"] > marker)]
        page, rest = rows[: self.page_size], rows[self.page_size:]
        result = {"Items": copy.deepcopy(page)}
        if rest:
            result["LastEvaluatedKey"] = {"pk": pk, "sk": page[-1]["sk"]}
        return result


def ts(hours_ago: float, now=None) -> str:
    now = now or NOW
    return format_ts(now - timedelta(hours=hours_ago))


__all__ = ["NOW", "FakeClock", "FakeTwitch", "FakeTable", "TwitchError", "TwitchRateLimited", "ts"]
