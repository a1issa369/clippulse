"""Minimal Twitch Helix client using only the standard library.

Only app access tokens are needed (client credentials flow), because reading
public clips needs no viewer login. The HTTP function is injectable for tests.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_URL = "https://api.twitch.tv/helix"
TIMEOUT_SECONDS = 10

# http(method, url, headers, form) -> (status, parsed_json_or_None, response_headers)
HttpFn = Callable[[str, str, Dict[str, str], Optional[Dict[str, str]]], Tuple[int, Optional[dict], Dict[str, str]]]


class TwitchError(Exception):
    """Twitch refused or failed a request."""


class TwitchRateLimited(TwitchError):
    def __init__(self, retry_after: Optional[float] = None):
        super().__init__("Twitch rate limit reached")
        self.retry_after = retry_after


def urllib_http(method, url, headers, form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, _parse(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, _parse(err.read()), dict(err.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise TwitchError(f"could not reach Twitch: {err}") from err


def _parse(raw: bytes) -> Optional[dict]:
    try:
        return json.loads(raw.decode() or "null")
    except ValueError:
        return None


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, http: HttpFn = urllib_http,
                 clock: Callable[[], float] = time.time):
        self._id = client_id
        self._secret = client_secret
        self._http = http
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expires = 0.0

    # -- auth -------------------------------------------------------------
    def _ensure_token(self, force: bool = False) -> str:
        if self._token and not force and self._clock() < self._token_expires - 60:
            return self._token
        status, body, _ = self._http(
            "POST", TOKEN_URL, {},
            {"client_id": self._id, "client_secret": self._secret, "grant_type": "client_credentials"},
        )
        if status != 200 or not body or "access_token" not in body:
            raise TwitchError(f"token request failed with status {status}")
        self._token = body["access_token"]
        self._token_expires = self._clock() + float(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: List[Tuple[str, str]]) -> dict:
        url = f"{API_URL}/{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._ensure_token(force=attempt == 1)}", "Client-Id": self._id}
            status, body, resp_headers = self._http("GET", url, headers, None)
            if status == 401 and attempt == 0:
                continue  # token was revoked or expired early: fetch a new one and retry once
            if status == 429:
                raise TwitchRateLimited(_retry_after(resp_headers))
            if status != 200 or body is None:
                raise TwitchError(f"{path} failed with status {status}")
            return body
        raise TwitchError(f"{path} failed after refreshing the token")

    # -- endpoints --------------------------------------------------------
    def get_user(self, login: str) -> Optional[dict]:
        body = self._get("users", [("login", login)])
        rows = body.get("data") or []
        if not rows:
            return None
        row = rows[0]
        return {
            "login": row["login"],
            "broadcaster_id": row["id"],
            "display_name": row.get("display_name") or row["login"],
            "profile_image_url": row.get("profile_image_url", ""),
        }

    def get_clips(self, broadcaster_id: str, started_at: str, max_items: int = 200) -> List[dict]:
        """Clips for one broadcaster created since `started_at`, most viewed first."""
        clips: List[dict] = []
        cursor: Optional[str] = None
        while len(clips) < max_items:
            params = [("broadcaster_id", broadcaster_id), ("started_at", started_at), ("first", "100")]
            if cursor:
                params.append(("after", cursor))
            body = self._get("clips", params)
            clips.extend(normalize_clip(row) for row in body.get("data") or [])
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return clips[:max_items]


def normalize_clip(row: dict) -> dict:
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row.get("title", ""),
        "broadcaster_id": row["broadcaster_id"],
        "broadcaster_name": row.get("broadcaster_name", ""),
        "creator_name": row.get("creator_name", ""),
        "game_id": row.get("game_id", ""),
        "language": row.get("language", ""),
        "view_count": int(row.get("view_count", 0)),
        "created_at": row["created_at"],
        "thumbnail_url": row.get("thumbnail_url", ""),
        "duration": float(row.get("duration", 0)),
        "vod_offset": row.get("vod_offset"),
    }


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    lowered = {k.lower(): v for k, v in headers.items()}
    value = lowered.get("ratelimit-reset") or lowered.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
