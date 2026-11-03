"""Run ClipPulse on your laptop with no AWS account and no Twitch keys.

    python backend/dev_server.py            # then open http://localhost:8787

It serves the built frontend (frontend/dist) and the real API code, backed by the
in-memory store and a *demo* Twitch with made-up channels and clips. Nothing here
talks to the network. Data resets whenever you restart it.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import random
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from clippulse.api import App  # noqa: E402
from clippulse.config import Settings  # noqa: E402
from clippulse.scoring import format_ts  # noqa: E402
from clippulse.service import ClipService  # noqa: E402
from clippulse.store import MemoryStore  # noqa: E402

DEMO_CHANNELS = {
    "demo_sketchlab": "SketchLab (demo)",
    "demo_speedruns": "Speedruns Daily (demo)",
    "demo_chessnight": "Chess Night (demo)",
    "demo_guest": "Guest Channel (demo)",     # not pinned: a visitor can search for it to see temporary tracking
}
DEMO_QUIET = {"demo_quiet": "Quiet Channel (demo)"}   # a real-looking account that has no clips
DEMO_PINNED = ("demo_sketchlab", "demo_speedruns", "demo_chessnight")
TITLES = [
    "Chat decides the ending", "That was NOT supposed to happen", "Perfect timing on the cut",
    "The plot twist nobody saw", "One frame too early", "Clutch of the week",
    "Wait for the reaction", "Best line of the stream", "Camera angle goes crazy",
    "We could not stop laughing", "Unreal comeback", "The sound design here",
]
PALETTES = [("#7c3aed", "#db2777"), ("#0ea5e9", "#6366f1"), ("#f59e0b", "#ef4444"),
            ("#10b981", "#0ea5e9"), ("#8b5cf6", "#14b8a6"), ("#f43f5e", "#f59e0b")]


def thumbnail(seed: int, label: str) -> str:
    a, b = PALETTES[seed % len(PALETTES)]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270" viewBox="0 0 480 270">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/>'
           f'<stop offset="1" stop-color="{b}"/></linearGradient></defs><rect width="480" height="270" fill="url(#g)"/>'
           f'<circle cx="{90 + (seed * 37) % 300}" cy="{70 + (seed * 53) % 120}" r="{40 + seed % 40}" fill="#ffffff" opacity=".18"/>'
           f'<text x="24" y="246" font-family="sans-serif" font-size="22" fill="#fff" opacity=".85">{label}</text></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


class DemoTwitch:
    """Stands in for TwitchClient: deterministic fake channels whose views keep growing."""

    def __init__(self, clock):
        self._clock = clock
        self._rng = random.Random(7)
        self._clips: dict = {}
        for n, login in enumerate(DEMO_CHANNELS):
            bid = str(5000 + n)
            rows = []
            for i in range(28):
                age = self._rng.choice([0.4, 1, 2, 3, 5, 8, 12, 20, 30, 48, 70, 100, 140])
                age += self._rng.random()
                rate = self._rng.choice([2, 4, 8, 15, 30, 60, 120, 250]) * self._rng.uniform(0.6, 1.4)
                title = TITLES[(i + n * 3) % len(TITLES)]
                rows.append({
                    "id": f"demo-{login}-{i}", "url": "https://www.twitch.tv/", "title": title,
                    "broadcaster_id": bid, "broadcaster_name": DEMO_CHANNELS[login], "creator_name": f"viewer{i}",
                    "game_id": "0", "language": "en", "view_count": int(rate * (age + 2)),
                    "created_at": age, "thumbnail_url": thumbnail(i + n * 5, DEMO_CHANNELS[login].split(" (")[0]),
                    "duration": float(self._rng.choice([14, 20, 27, 30, 41, 58])),
                    "vod_offset": self._rng.randint(60, 7000), "_rate": rate,
                })
            self._clips[bid] = rows
        self._created_at_fixed = False

    def _materialize(self, now: datetime) -> None:
        if self._created_at_fixed:
            return
        for rows in self._clips.values():
            for clip in rows:
                clip["created_at"] = format_ts(now - timedelta(hours=clip["created_at"]))
        self._created_at_fixed = True

    def grow(self, hours: float = 1.0) -> None:
        for rows in self._clips.values():
            for clip in rows:
                wobble = self._rng.uniform(0.5, 1.6)
                decay = 0.5 ** (clip.get("_ticks", 0) / 12)  # momentum fades as a clip ages
                clip["view_count"] += int(clip["_rate"] * wobble * decay * hours)
                clip["_ticks"] = clip.get("_ticks", 0) + 1

    def get_user(self, login):
        if login in DEMO_QUIET:
            return {"login": login, "broadcaster_id": "9000", "display_name": DEMO_QUIET[login], "profile_image_url": ""}
        if login not in DEMO_CHANNELS:
            return None
        n = list(DEMO_CHANNELS).index(login)
        return {"login": login, "broadcaster_id": str(5000 + n), "display_name": DEMO_CHANNELS[login],
                "profile_image_url": ""}

    def get_clips(self, broadcaster_id, started_at, max_items=100):
        self._materialize(datetime.now(timezone.utc))  # ages are relative to real time, not the simulated clock
        rows = [c for c in self._clips.get(broadcaster_id, []) if c["created_at"] >= started_at]
        rows.sort(key=lambda c: -c["view_count"])
        return [{k: v for k, v in copy.deepcopy(c).items() if not k.startswith("_")} for c in rows[:max_items]]


class OffsetClock:
    def __init__(self):
        self.offset = timedelta(0)

    def __call__(self):
        return datetime.now(timezone.utc) + self.offset


def build_demo_service():
    clock = OffsetClock()
    twitch = DemoTwitch(clock)
    service = ClipService(MemoryStore(), lambda: twitch, Settings(pinned_streamers=DEMO_PINNED), clock=clock)
    # Pretend the poller has been running for six hours so trends have something to show.
    for hours_ago in range(6, -1, -1):
        clock.offset = -timedelta(hours=hours_ago)
        if hours_ago == 6:
            service.ensure_pinned()
        else:
            twitch.grow()
            service.poll_all()
    clock.offset = timedelta(0)

    def tick():  # every 20 seconds act like one hourly poll, so the UI visibly moves
        twitch.grow()
        service.poll_all()
        threading.Timer(20, tick).start()

    timer = threading.Timer(20, tick)
    timer.daemon = True
    timer.start()
    return service


def make_handler(app: App):
    """Hands every request to the same App the Lambda runs, including static file serving."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive, like the real front door

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def _handle(self):
            parsed = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            length = int(self.headers.get("content-length") or 0)
            event = {"rawPath": parsed.path, "queryStringParameters": query or None,
                     "headers": {k.lower(): v for k, v in self.headers.items()},
                     "requestContext": {"http": {"method": self.command, "path": parsed.path, "sourceIp": self.client_address[0]}},
                     "body": self.rfile.read(length).decode() if length else None}
            response = app.handle(event)
            body = response.get("body") or ""
            payload = base64.b64decode(body) if response.get("isBase64Encoded") else body.encode()
            status = response["statusCode"]
            self.send_response(status)
            for key, value in response["headers"].items():
                self.send_header(key, value)
            if status != 204:  # a 204 must not carry a body or a length
                self.send_header("content-length", str(len(payload)))
            self.end_headers()
            if status != 204 and self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--static", default=str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))
    args = parser.parse_args()
    service = build_demo_service()
    app = App(lambda: service, static_dir=Path(args.static))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    print(f"ClipPulse demo running at http://localhost:{args.port}  (made-up channels, no network)")
    server.serve_forever()


if __name__ == "__main__":
    main()
