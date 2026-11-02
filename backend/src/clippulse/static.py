"""Serves the built React site from inside the Lambda, so ClipPulse works with nothing
but API Gateway in front of it (no CloudFront, no S3 website needed).

The frontend build is copied into `static/` next to this file before deploying
(see scripts/deploy.sh). Responses carry the same security headers the CloudFront
policy would add, and text assets are gzipped when the browser accepts it.
"""
from __future__ import annotations

import base64
import gzip
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-src https://clips.twitch.tv; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
COMPRESSIBLE = (".html", ".js", ".css", ".svg", ".json", ".txt", ".map")


class StaticSite:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._cache: Dict[Tuple[str, bool], Tuple[bytes, Dict[str, str]]] = {}

    def serve(self, method: str, raw_path: str, accept_encoding: str = "") -> Dict[str, Any]:
        target = self._resolve(raw_path)
        if target is None:
            return self._plain(404, "Not found" if self.root.is_dir() else
                               "The website has not been bundled into this deployment. Run scripts/deploy.sh.")
        use_gzip = target.suffix in COMPRESSIBLE and "gzip" in accept_encoding.lower()
        body, headers = self._load(target, use_gzip)
        response: Dict[str, Any] = {"statusCode": 200, "headers": dict(headers)}
        if method == "HEAD":
            response["body"] = ""
        elif use_gzip or target.suffix not in CONTENT_TYPES or not CONTENT_TYPES[target.suffix].startswith(
                ("text/", "application/json", "image/svg")):
            response["body"] = base64.b64encode(body).decode("ascii")
            response["isBase64Encoded"] = True
        else:
            response["body"] = body.decode("utf-8")
        return response

    # -- internals -----------------------------------------------------------
    def _resolve(self, raw_path: str) -> Optional[Path]:
        path = urllib.parse.unquote(raw_path or "/")
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        if "\x00" in relative:
            return None
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            return None
        return target

    def _load(self, target: Path, use_gzip: bool) -> Tuple[bytes, Dict[str, str]]:
        key = (str(target), use_gzip)
        if key not in self._cache:
            data = target.read_bytes()
            headers = {
                "content-type": CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
                # Hashed bundle names never change, so they can be cached forever; index.html must be
                # re-checked so a new deploy is picked up immediately.
                "cache-control": ("public, max-age=31536000, immutable" if "/assets/" in target.as_posix()
                                  else "no-cache"),
                **SECURITY_HEADERS,
            }
            if target.suffix in COMPRESSIBLE:
                headers["vary"] = "accept-encoding"
            if use_gzip:
                data = gzip.compress(data, compresslevel=6, mtime=0)
                headers["content-encoding"] = "gzip"
            self._cache[key] = (data, headers)
        return self._cache[key]

    @staticmethod
    def _plain(status: int, text: str) -> Dict[str, Any]:
        return {"statusCode": status, "body": text,
                "headers": {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store", **SECURITY_HEADERS}}
