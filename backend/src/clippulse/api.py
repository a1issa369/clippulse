"""AWS Lambda entry point for the HTTP API (API Gateway payload format 2.0).

All API routes live under /api. Anything else is the React site: served from S3 by
CloudFront when that is enabled, or by this same function (see static.py) when it is not.
Either way the browser talks to a single origin, so no CORS is needed.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .service import ClipService, RateLimited, ServiceError, ValidationError
from .static import StaticSite

logger = logging.getLogger("clippulse.api")
logger.setLevel(logging.INFO)

MAX_BODY_BYTES = 4096
SEC_HEADERS = {"x-content-type-options": "nosniff", "cache-control": "no-store"}

Event = Dict[str, Any]
Response = Dict[str, Any]


def json_response(status: int, body: Any = None) -> Response:
    headers = {"content-type": "application/json", **SEC_HEADERS}
    return {"statusCode": status, "headers": headers, "body": "" if body is None else json.dumps(body)}


def error_response(status: int, code: str, message: str) -> Response:
    return json_response(status, {"error": {"code": code, "message": message}})


class App:
    def __init__(self, service_provider: Callable[[], ClipService], static_dir: Optional[Path] = None):
        self._service_provider = service_provider
        self._site = StaticSite(static_dir if static_dir is not None else Path(__file__).parent / "static")
        self._routes: List[Tuple[str, "re.Pattern[str]", Callable[..., Response]]] = []
        self._add("GET", r"/api/health", self._health)
        self._add("GET", r"/api/streamers", self._list_streamers)
        self._add("DELETE", r"/api/streamers/(?P<login>[^/]+)", self._remove_streamer)
        self._add("POST", r"/api/streamers", self._track_streamer)
        self._add("GET", r"/api/streamers/(?P<login>[^/]+)/clips", self._streamer_clips)
        self._add("GET", r"/api/rising", self._rising)
        self._add("GET", r"/api/boards/(?P<board>[^/]+)", self._get_board)
        self._add("GET", r"/api/boards/(?P<board>[^/]+)/export\.csv", self._export)
        self._add("PUT", r"/api/boards/(?P<board>[^/]+)/items/(?P<clip>[^/]+)", self._save_item)
        self._add("DELETE", r"/api/boards/(?P<board>[^/]+)/items/(?P<clip>[^/]+)", self._remove_item)

    def _add(self, method: str, pattern: str, func: Callable[..., Response]) -> None:
        self._routes.append((method, re.compile(f"^{pattern}$"), func))

    # -- dispatch ---------------------------------------------------------
    def handle(self, event: Event) -> Response:
        http = event.get("requestContext", {}).get("http", {})
        method = (http.get("method") or "GET").upper()
        path = event.get("rawPath") or http.get("path") or "/"
        if path != "/api" and not path.startswith("/api/"):
            return self._website(method, path, event)
        try:
            path_matched = False
            for route_method, pattern, func in self._routes:
                match = pattern.match(path)
                if not match:
                    continue
                path_matched = True
                if route_method == method:
                    return func(event, **match.groupdict())
            if path_matched:
                return error_response(405, "method_not_allowed", "Method not allowed.")
            return error_response(404, "not_found", "No such route.")
        except ServiceError as err:
            response = error_response(err.status, err.code, err.message)
            if isinstance(err, RateLimited):
                response["headers"]["retry-after"] = str(err.retry_after)
            return response
        except Exception:  # never leak internals to the caller
            logger.exception("unhandled error for %s %s", method, path)
            return error_response(500, "internal_error", "Something went wrong.")

    def _website(self, method: str, path: str, event: Event) -> Response:
        """Everything outside /api is the React site (the Lambda doubles as the web server)."""
        if method not in ("GET", "HEAD"):
            return error_response(405, "method_not_allowed", "Method not allowed.")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        return self._site.serve(method, path, headers.get("accept-encoding", ""))

    @property
    def service(self) -> ClipService:
        return self._service_provider()

    # -- handlers ---------------------------------------------------------
    def _health(self, event: Event) -> Response:
        return json_response(200, {"ok": True})

    def _list_streamers(self, event: Event) -> Response:
        return json_response(200, {"streamers": self.service.list_streamers(self._client(event))})

    def _track_streamer(self, event: Event) -> Response:
        body = _json_body(event)
        result = self.service.track_streamer(body.get("login"), client_id=self._client(event))
        return json_response(201 if result["created"] else 200, result)

    def _remove_streamer(self, event: Event, login: str) -> Response:
        self.service.remove_streamer(login, self._client(event))
        return json_response(204)

    def _client(self, event: Event) -> Optional[str]:
        """The caller's anonymous visitor id (salted IP hash), or None when API Gateway did not say."""
        ip = (event.get("requestContext", {}).get("http", {}) or {}).get("sourceIp")
        return self.service.client_id(ip)

    def _streamer_clips(self, event: Event, login: str) -> Response:
        q = _query(event)
        return json_response(200, self.service.clips_for(
            login, days=q.get("days", 7), sort=q.get("sort", "velocity"), limit=q.get("limit", 50)))

    def _rising(self, event: Event) -> Response:
        q = _query(event)
        return json_response(200, self.service.rising(hours=q.get("hours", 48), limit=q.get("limit", 20)))

    def _get_board(self, event: Event, board: str) -> Response:
        return json_response(200, self.service.get_board(board))

    def _export(self, event: Event, board: str) -> Response:
        text = self.service.export_csv(board)
        headers = {"content-type": "text/csv; charset=utf-8",
                   "content-disposition": 'attachment; filename="clippulse-shotlist.csv"', **SEC_HEADERS}
        return {"statusCode": 200, "headers": headers, "body": text}

    def _save_item(self, event: Event, board: str, clip: str) -> Response:
        body = _json_body(event, allow_empty=True)
        item = self.service.save_item(board, clip, body.get("streamer"), body.get("note"))
        return json_response(200, {"item": item})

    def _remove_item(self, event: Event, board: str, clip: str) -> Response:
        self.service.remove_item(board, clip)
        return json_response(204)


def _query(event: Event) -> Dict[str, str]:
    return event.get("queryStringParameters") or {}


def _json_body(event: Event, allow_empty: bool = False) -> Dict[str, Any]:
    raw: Optional[str] = event.get("body")
    if raw and event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if not raw:
        if allow_empty:
            return {}
        raise ValidationError("A JSON body is required.")
    if len(raw.encode()) > MAX_BODY_BYTES:
        raise ValidationError("Request body is too large.")
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValidationError("Body must be valid JSON.")
    if not isinstance(parsed, dict):
        raise ValidationError("Body must be a JSON object.")
    return parsed


_app: Optional[App] = None


def handler(event: Event, context: Any = None) -> Response:
    global _app
    if _app is None:
        from .wiring import build_service  # imported lazily so tests never need boto3
        _app = App(build_service)
    return _app.handle(event)
