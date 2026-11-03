"""Storage layer: one interface, an in-memory implementation for tests and local
development, and a DynamoDB implementation for AWS.

DynamoDB single-table layout (all keys are strings):

    pk                 sk                           what it holds
    STREAMERS          S#<login>                    a tracked streamer (pinned, or temporary with expires_at)
    RATE#<bucket>      W#<window>                   a fixed-window counter for rate limiting (TTL cleans it up)
    B#<broadcaster>    C#<created_at>#<clip_id>     a clip plus its view-count history
    BOARD#<board_id>   I#<clip_id>                  one saved shotlist item

Keeping every clip's snapshots in a `history` list on the clip item (instead of one
item per snapshot) means a streamer's clips come back from a single Query, and the
hourly poll costs one read and one write per clip.
"""
from __future__ import annotations

import copy
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Dict, List, Optional

STREAMERS_PK = "STREAMERS"

# One atomic DynamoDB write both counts a use and refuses it once the limit is reached.
# (#ttl and #hits are placeholders because `ttl` is a reserved word.)
TAKE_TOKEN_UPDATE = "SET #ttl = :ttl ADD #hits :one"
TAKE_TOKEN_CONDITION = "attribute_not_exists(#hits) OR #hits < :limit"


class Store(ABC):
    # streamers
    @abstractmethod
    def list_streamers(self) -> List[dict]: ...
    @abstractmethod
    def get_streamer(self, login: str) -> Optional[dict]: ...
    @abstractmethod
    def put_streamer(self, streamer: dict) -> None: ...
    @abstractmethod
    def delete_streamer(self, login: str) -> bool: ...

    # rate limiting
    @abstractmethod
    def take_token(self, bucket: str, window_seconds: int, limit: int, now: float) -> bool:
        """Count one use of `bucket` in the current fixed window. False (and no count) once `limit` is reached."""

    # clips
    @abstractmethod
    def upsert_clips(self, broadcaster_id: str, clips: List[dict], now_iso: str,
                     history_points: int, ttl: int) -> None:
        """Insert clips or refresh them, appending (now_iso, view_count) to each history."""

    @abstractmethod
    def query_clips(self, broadcaster_id: str, since_iso: str) -> List[dict]:
        """Clips created at or after since_iso, newest first, each with its history."""

    # shotlist boards
    @abstractmethod
    def get_board_item(self, board_id: str, clip_id: str) -> Optional[dict]: ...
    @abstractmethod
    def put_board_item(self, board_id: str, item: dict) -> None: ...
    @abstractmethod
    def delete_board_item(self, board_id: str, clip_id: str) -> bool: ...
    @abstractmethod
    def list_board_items(self, board_id: str) -> List[dict]: ...


def merge_history(existing: Optional[dict], views: int, now_iso: str, history_points: int) -> list:
    history = [list(p) for p in (existing or {}).get("history", [])]
    if history and history[-1][0] == now_iso:
        history[-1][1] = views  # same poll timestamp: overwrite rather than duplicate
    else:
        history.append([now_iso, views])
    return history[-history_points:]


class MemoryStore(Store):
    def __init__(self) -> None:
        self._streamers: Dict[str, dict] = {}
        self._clips: Dict[str, Dict[str, dict]] = {}
        self._boards: Dict[str, Dict[str, dict]] = {}
        self._counters: Dict[tuple, int] = {}

    def list_streamers(self):
        return [copy.deepcopy(self._streamers[k]) for k in sorted(self._streamers)]

    def get_streamer(self, login):
        found = self._streamers.get(login)
        return copy.deepcopy(found) if found else None

    def put_streamer(self, streamer):
        self._streamers[streamer["login"]] = copy.deepcopy(streamer)

    def delete_streamer(self, login):
        return self._streamers.pop(login, None) is not None

    def take_token(self, bucket, window_seconds, limit, now):
        key = (bucket, int(now // window_seconds))
        if self._counters.get(key, 0) >= limit:
            return False
        self._counters[key] = self._counters.get(key, 0) + 1
        return True

    def upsert_clips(self, broadcaster_id, clips, now_iso, history_points, ttl):
        bucket = self._clips.setdefault(broadcaster_id, {})
        for clip in clips:
            history = merge_history(bucket.get(clip["id"]), clip["view_count"], now_iso, history_points)
            bucket[clip["id"]] = {**copy.deepcopy(clip), "history": history, "ttl": ttl}

    def query_clips(self, broadcaster_id, since_iso):
        rows = [c for c in self._clips.get(broadcaster_id, {}).values() if c["created_at"] >= since_iso]
        rows.sort(key=lambda c: (c["created_at"], c["id"]), reverse=True)
        return copy.deepcopy(rows)

    def get_board_item(self, board_id, clip_id):
        found = self._boards.get(board_id, {}).get(clip_id)
        return copy.deepcopy(found) if found else None

    def put_board_item(self, board_id, item):
        self._boards.setdefault(board_id, {})[item["clip_id"]] = copy.deepcopy(item)

    def delete_board_item(self, board_id, clip_id):
        return self._boards.get(board_id, {}).pop(clip_id, None) is not None

    def list_board_items(self, board_id):
        return copy.deepcopy(list(self._boards.get(board_id, {}).values()))


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def to_dynamo(value: Any) -> Any:
    """boto3's resource API rejects floats; store them as Decimals."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


def from_dynamo(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def _strip_keys(item: dict) -> dict:
    return {k: v for k, v in from_dynamo(item).items() if k not in ("pk", "sk")}


class DynamoStore(Store):
    """`table` is a boto3 DynamoDB Table resource (or anything with the same methods)."""

    def __init__(self, table) -> None:
        self._table = table

    def _query_all(self, **kwargs) -> List[dict]:
        items: List[dict] = []
        while True:
            page = self._table.query(**kwargs)
            items.extend(page.get("Items", []))
            last = page.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs["ExclusiveStartKey"] = last

    # streamers
    def list_streamers(self):
        rows = self._query_all(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
            ExpressionAttributeValues={":pk": STREAMERS_PK, ":p": "S#"},
        )
        return [_strip_keys(r) for r in rows]

    def get_streamer(self, login):
        found = self._table.get_item(Key={"pk": STREAMERS_PK, "sk": f"S#{login}"}).get("Item")
        return _strip_keys(found) if found else None

    def put_streamer(self, streamer):
        self._table.put_item(Item=to_dynamo({"pk": STREAMERS_PK, "sk": f"S#{streamer['login']}", **streamer}))

    def delete_streamer(self, login):
        resp = self._table.delete_item(Key={"pk": STREAMERS_PK, "sk": f"S#{login}"}, ReturnValues="ALL_OLD")
        return bool(resp.get("Attributes"))

    def take_token(self, bucket, window_seconds, limit, now):
        window = int(now // window_seconds)
        try:
            self._table.update_item(
                Key={"pk": f"RATE#{bucket}", "sk": f"W#{window}"},
                UpdateExpression=TAKE_TOKEN_UPDATE,
                ConditionExpression=TAKE_TOKEN_CONDITION,
                ExpressionAttributeNames={"#ttl": "ttl", "#hits": "hits"},
                ExpressionAttributeValues={":ttl": (window + 2) * window_seconds, ":one": 1, ":limit": limit},
            )
            return True
        except Exception as err:   # botocore is not imported here, so match on the error code instead
            if getattr(err, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    # clips
    @staticmethod
    def _clip_key(broadcaster_id: str, clip: dict) -> dict:
        return {"pk": f"B#{broadcaster_id}", "sk": f"C#{clip['created_at']}#{clip['id']}"}

    def upsert_clips(self, broadcaster_id, clips, now_iso, history_points, ttl):
        for clip in clips:
            key = self._clip_key(broadcaster_id, clip)
            existing = self._table.get_item(Key=key).get("Item")
            history = merge_history(from_dynamo(existing) if existing else None,
                                    clip["view_count"], now_iso, history_points)
            self._table.put_item(Item=to_dynamo({**key, **clip, "history": history, "ttl": ttl}))

    def query_clips(self, broadcaster_id, since_iso):
        rows = self._query_all(
            KeyConditionExpression="pk = :pk AND sk BETWEEN :lo AND :hi",
            ExpressionAttributeValues={":pk": f"B#{broadcaster_id}", ":lo": f"C#{since_iso}", ":hi": "C#~"},
            ScanIndexForward=False,
        )
        return [_strip_keys(r) for r in rows]

    # boards
    @staticmethod
    def _board_key(board_id: str, clip_id: str) -> dict:
        return {"pk": f"BOARD#{board_id}", "sk": f"I#{clip_id}"}

    def get_board_item(self, board_id, clip_id):
        found = self._table.get_item(Key=self._board_key(board_id, clip_id)).get("Item")
        return _strip_keys(found) if found else None

    def put_board_item(self, board_id, item):
        self._table.put_item(Item=to_dynamo({**self._board_key(board_id, item["clip_id"]), **item}))

    def delete_board_item(self, board_id, clip_id):
        resp = self._table.delete_item(Key=self._board_key(board_id, clip_id), ReturnValues="ALL_OLD")
        return bool(resp.get("Attributes"))

    def list_board_items(self, board_id):
        rows = self._query_all(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :p)",
            ExpressionAttributeValues={":pk": f"BOARD#{board_id}", ":p": "I#"},
        )
        return [_strip_keys(r) for r in rows]


def ttl_from_now(days: int, now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) + days * 86400)
