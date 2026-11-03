import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { resolveBoardId } from "./board.js";
import {
  ClipCard, ClipModal, Logo, Segmented, Shotlist, SkeletonGrid, StreamerBar, TrackForm,
} from "./components.jsx";
import { useBoard, useFeed, useStreamers } from "./hooks.js";

const SORTS = [
  { value: "velocity", label: "Rising" },
  { value: "views", label: "Most viewed" },
  { value: "new", label: "Newest" },
];
const DAY_RANGES = [
  { value: 1, label: "24 hours" },
  { value: 3, label: "3 days" },
  { value: 7, label: "7 days" },
];
const RISING_WINDOWS = [
  { value: 24, label: "24 hours" },
  { value: 72, label: "3 days" },
  { value: 168, label: "7 days" },
];

function formatAsOf(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export default function App() {
  const [{ id: boardId }] = useState(() => {
    const resolved = resolveBoardId({ search: window.location.search, storage: safeStorage() });
    if (resolved.fromLink) window.history.replaceState(null, "", window.location.pathname);
    return resolved;
  });
  const streamerState = useStreamers();
  const board = useBoard(boardId);

  const [view, setView] = useState("rising");
  const [sort, setSort] = useState("velocity");
  const [days, setDays] = useState(7);
  const [hours, setHours] = useState(72);
  const [shotlistOpen, setShotlistOpen] = useState(false);
  const openerRef = useRef(null);
  const playerOpenerRef = useRef(null);
  const [playing, setPlaying] = useState(null);

  const feed = useFeed({ view, sort, days, hours });
  const savedIds = useMemo(() => new Set(board.items.map((i) => i.clip_id)), [board.items]);

  const handleTrack = useCallback(async (login) => {
    const tracked = await streamerState.track(login);
    setView(tracked);
  }, [streamerState]);

  const handleRemove = useCallback(async (login) => {
    await streamerState.remove(login);
    setView((current) => (current === login ? "rising" : current));
  }, [streamerState]);

  const closeShotlist = useCallback(() => {
    setShotlistOpen(false);
    openerRef.current?.focus();
  }, []);

  const playClip = useCallback((clip, trigger) => {
    playerOpenerRef.current = trigger;
    setPlaying(clip.id);
  }, []);

  const closePlayer = useCallback(() => {
    setPlaying(null);
    const opener = playerOpenerRef.current;
    if (opener && document.contains(opener)) opener.focus();
  }, []);

  // Look the clip up in the current feed each render so the saved state and numbers stay live.
  const playingClip = playing ? feed.clips.find((c) => c.id === playing) ?? null : null;

  // A streamer other visitors added can age out. If someone opens one that just expired, add it back for them.
  const retried = useRef(new Set());
  useEffect(() => {
    if (feed.status !== "error" || feed.errorStatus !== 404 || view === "rising" || retried.current.has(view)) return;
    retried.current.add(view);
    handleTrack(view).then(() => feed.reload()).catch(() => { /* the error banner already explains it */ });
  }, [feed.status, feed.errorStatus, view]); // eslint-disable-line react-hooks/exhaustive-deps

  const noStreamers = streamerState.status === "ready" && streamerState.streamers.length === 0;
  const asOf = formatAsOf(feed.asOf);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Logo />
          <span className="brand-name">ClipPulse</span>
        </div>
        <button ref={openerRef} type="button" className="btn btn--shotlist" onClick={() => setShotlistOpen(true)}>
          Shotlist <span className="count" aria-label={`${board.items.length} saved`}>{board.items.length}</span>
        </button>
      </header>

      <main className="main">
        <section className="hero">
          <h1>Clips worth editing, before they go stale.</h1>
          <p>ClipPulse watches how fast Twitch clips are gaining views, so you can spot the ones taking off and save them to a shotlist for your next edit.</p>
        </section>

        {streamerState.status === "error" && (
          <p className="banner banner--error" role="alert">
            {streamerState.error} <button type="button" className="link-btn" onClick={() => window.location.reload()}>Reload</button>
          </p>
        )}

        {noStreamers ? (
          <section className="empty">
            <h2>Search for a streamer</h2>
            <p>Enter any Twitch username. ClipPulse pulls their recent clips right away, then re-checks view counts every hour to see which ones are rising.</p>
            <TrackForm onTrack={handleTrack} prominent />
          </section>
        ) : (
          <>
            <StreamerBar streamers={streamerState.streamers} view={view} onView={setView} onTrack={handleTrack} onRemove={handleRemove} />
            <p className="hint">Search any Twitch streamer. You can have up to 3 of your own at a time, and remove them with the × on their chip. They are dropped after 2 days without a visit.</p>
            <div className="controls">
              {view === "rising" ? (
                <Segmented label="Time window" options={RISING_WINDOWS} value={hours} onChange={setHours} />
              ) : (
                <>
                  <Segmented label="Sort clips" options={SORTS} value={sort} onChange={setSort} />
                  <Segmented label="Clip age" options={DAY_RANGES} value={days} onChange={setDays} />
                </>
              )}
              <span className="asof" role="status">{asOf ? `Updated ${asOf}` : ""}</span>
            </div>

            {feed.status === "error" && !feed.loading && (
              <p className="banner banner--error" role="alert">
                {feed.error} <button type="button" className="link-btn" onClick={feed.reload}>Try again</button>
              </p>
            )}
            {feed.loading ? (
              <SkeletonGrid />
            ) : feed.clips.length === 0 && feed.status === "ready" ? (
              <p className="empty-note">No clips in this window yet. Try a longer range, or check back after the next hourly update.</p>
            ) : (
              <div className="grid">
                {feed.clips.map((clip) => (
                  <ClipCard key={clip.id} clip={clip} saved={savedIds.has(clip.id)} onSave={board.add} onRemove={board.remove}
                    onPlay={playClip} />
                ))}
              </div>
            )}
          </>
        )}
      </main>

      <footer className="footer">
        <p>
          Clip data comes from the Twitch API and refreshes hourly. "Views/hr" is how many views a clip gained
          recently once ClipPulse has watched it for a while, or its average since it was posted before that.
          ClipPulse is an independent project and is not affiliated with or endorsed by Twitch.
        </p>
      </footer>

      {playingClip && (
        <ClipModal clip={playingClip} saved={savedIds.has(playingClip.id)} onSave={board.add} onRemove={board.remove}
          onClose={closePlayer} />
      )}

      <Shotlist open={shotlistOpen} onClose={closeShotlist} board={board} boardId={boardId} />
    </div>
  );
}

function safeStorage() {
  try { return window.localStorage; } catch { return undefined; }
}
