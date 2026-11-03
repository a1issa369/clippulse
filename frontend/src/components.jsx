import { useEffect, useId, useRef, useState } from "react";

import { api } from "./api.js";
import { shareUrl } from "./board.js";
import {
  describeGrowth, describeVelocity, embedUrl, formatAge, formatCount, formatDuration, formatOffset, formatVelocity,
  growthSeries, safeUrl, sparkline, trendInfo, velocityBasis,
} from "./format.js";

export function Logo() {
  return (
    <svg className="logo" viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="var(--surface-2)" />
      <path d="M3 17h6l3-8 5 15 4-11 2 4h6" fill="none" stroke="var(--accent)" strokeWidth="2.4"
        strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Segmented({ label, options, value, onChange }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.value} type="button" className="seg" aria-pressed={option.value === value}
          onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function TrackForm({ onTrack, prominent = false }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inputId = useId();

  async function submit(event) {
    event.preventDefault();
    if (!value.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onTrack(value);
      setValue("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={prominent ? "track track--big" : "track"} onSubmit={submit}>
      <label htmlFor={inputId} className="sr-only">Twitch username to search for</label>
      <input id={inputId} value={value} onChange={(e) => setValue(e.target.value)} placeholder="Search any streamer"
        autoComplete="off" autoCapitalize="none" spellCheck="false" maxLength={26} aria-invalid={error ? "true" : undefined}
        aria-describedby={error ? `${inputId}-err` : undefined} />
      <button type="submit" className="btn btn--primary" disabled={busy || !value.trim()}>
        {busy ? "Searching..." : "Search"}
      </button>
      {error && <p id={`${inputId}-err`} className="field-error" role="alert">{error}</p>}
    </form>
  );
}

export function StreamerBar({ streamers, view, onView, onTrack, onRemove }) {
  const [removeError, setRemoveError] = useState(null);
  const remove = async (s) => {
    setRemoveError(null);
    try { await onRemove(s.login); } catch (err) { setRemoveError(err.message); }
  };
  return (
    <nav className="streamers" aria-label="Streamers">
      <div className="chips">
        <button type="button" className="chip" aria-pressed={view === "rising"} onClick={() => onView("rising")}>
          Rising everywhere
        </button>
        {streamers.map((s) => (
          <span key={s.login} className={s.mine ? "chip-wrap chip-wrap--mine" : "chip-wrap"}>
            <button type="button" className="chip" aria-pressed={view === s.login} onClick={() => onView(s.login)}>
              {s.display_name}
            </button>
            {s.mine && (
              <button type="button" className="chip-x" aria-label={`Stop tracking ${s.display_name}`}
                title="Stop tracking" onClick={() => remove(s)}>×</button>
            )}
          </span>
        ))}
      </div>
      {removeError && <p className="banner banner--error" role="alert">{removeError}</p>}
      <TrackForm onTrack={onTrack} />
    </nav>
  );
}

function Thumb({ clip }) {
  const [broken, setBroken] = useState(false);
  const src = clip.thumbnail_url;
  const ok = src && !broken && (src.startsWith("https://") || src.startsWith("data:image/"));
  return ok
    ? <img src={src} alt="" loading="lazy" onError={() => setBroken(true)} />
    : <div className="thumb-fallback" aria-hidden="true" />;
}

// Views over time, drawn from the snapshots ClipPulse has taken. Renders nothing until there are two.
export function Sparkline({ history, width = 120, height = 32, large = false }) {
  const points = growthSeries(history);
  const chart = sparkline(points, { width, height, pad: large ? 6 : 3 });
  if (!chart) return null;
  const label = describeGrowth(points);
  return (
    <svg className={large ? "spark spark--large" : "spark"} viewBox={`0 0 ${chart.width} ${chart.height}`}
      width={large ? undefined : width} height={large ? undefined : height} role="img" aria-label={label} preserveAspectRatio="none">
      <title>{label}</title>
      <path d={chart.area} className="spark-area" />
      <path d={chart.line} className="spark-line" vectorEffect="non-scaling-stroke" />
      <circle cx={chart.last.x} cy={chart.last.y} r={large ? 3.5 : 2.5} className="spark-dot" />
    </svg>
  );
}

export function ClipCard({ clip, saved, onSave, onRemove, onPlay }) {
  const link = safeUrl(clip.url);
  const trend = trendInfo(clip.trend);
  const title = clip.title || "Untitled clip";
  const canPlay = Boolean(embedUrl(clip.id, window.location.hostname));
  return (
    <article className="card">
      <div className="thumb">
        {canPlay ? (
          <button type="button" className="thumb-play" onClick={(event) => onPlay(clip, event.currentTarget)}
            aria-label={`Play ${title}`}>
            <Thumb clip={clip} />
            <span className="play-icon" aria-hidden="true">&#9654;</span>
          </button>
        ) : link ? <a href={link} target="_blank" rel="noopener noreferrer" tabIndex={-1} aria-hidden="true"><Thumb clip={clip} /></a> : <Thumb clip={clip} />}
        {trend && <span className={`badge badge--${trend.tone}`}>{trend.label}</span>}
        <span className="badge badge--time">{formatDuration(clip.duration)}</span>
      </div>
      <div className="card-body">
        <h3 className="card-title">
          {link ? <a href={link} target="_blank" rel="noopener noreferrer">{title}</a> : title}
        </h3>
        <p className="card-sub">{clip.streamer_name}{clip.creator_name ? ` · clipped by ${clip.creator_name}` : ""}</p>
        <div className="stats">
          <span className="velocity" title={describeVelocity(clip)}>
            <strong>{formatVelocity(clip.velocity)}</strong> views/hr
          </span>
          <span className="stat-muted">{formatCount(clip.view_count)} views · {formatAge(clip.age_hours)}</span>
        </div>
        <Sparkline history={clip.history} />
        {saved ? (
          <button type="button" className="btn btn--saved" onClick={() => onRemove(clip.id)} aria-label={`Remove ${title} from shotlist`}>
            Saved to shotlist
          </button>
        ) : (
          <button type="button" className="btn" onClick={() => onSave(clip)} aria-label={`Add ${title} to shotlist`}>
            Add to shotlist
          </button>
        )}
      </div>
    </article>
  );
}

// Plays a clip with Twitch's own embed. The iframe is only created once a viewer clicks play,
// so browsing the feed never loads anything from Twitch's player.
export function ClipModal({ clip, saved, onSave, onRemove, onClose }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const headingId = useId();
  const title = clip.title || "Untitled clip";
  const src = embedUrl(clip.id, window.location.hostname);
  const link = safeUrl(clip.url);
  const offset = formatOffset(clip.vod_offset);

  useEffect(() => {
    closeRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(event) {
      if (event.key === "Escape") { onClose(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll("a[href], button:not([disabled]), iframe")];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); document.body.style.overflow = previousOverflow; };
  }, [onClose]);

  return (
    <div className="modal-wrap" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={headingId} ref={dialogRef}>
        <header className="modal-head">
          <div>
            <h2 id={headingId}>{title}</h2>
            <p className="card-sub">{clip.streamer_name}{clip.creator_name ? ` · clipped by ${clip.creator_name}` : ""}</p>
          </div>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Close player">×</button>
        </header>
        <div className="player">
          {src ? (
            <iframe src={src} title={`Twitch clip: ${title}`} allowFullScreen referrerPolicy="strict-origin-when-cross-origin" />
          ) : <p className="empty-note">This clip cannot be embedded.</p>}
        </div>
        <div className="modal-stats">
          <p><strong>{formatVelocity(clip.velocity)}</strong> views/hr <span className="stat-muted">({velocityBasis(clip)})</span></p>
          <p className="stat-muted">{formatCount(clip.view_count)} views · {formatAge(clip.age_hours)}{offset ? ` · starts ${offset} into the VOD` : ""}</p>
        </div>
        <section className="growth" aria-label="View growth">
          <h3>Growth since ClipPulse started watching</h3>
          <Sparkline history={clip.history} width={560} height={120} large />
          <p className="stat-muted">{describeGrowth(growthSeries(clip.history))}</p>
        </section>
        <div className="modal-actions">
          {saved ? (
            <button type="button" className="btn btn--saved" onClick={() => onRemove(clip.id)}>Saved to shotlist</button>
          ) : (
            <button type="button" className="btn btn--primary" onClick={() => onSave(clip)}>Add to shotlist</button>
          )}
          {link && <a className="btn" href={link} target="_blank" rel="noopener noreferrer">Open on Twitch</a>}
        </div>
      </div>
    </div>
  );
}

export function SkeletonGrid() {
  return (
    <div className="grid" aria-hidden="true">
      {Array.from({ length: 6 }, (_, i) => (
        <div className="card card--skeleton" key={i}><div className="thumb" /><div className="card-body"><span className="sk sk--wide" /><span className="sk" /></div></div>
      ))}
    </div>
  );
}

function ShotlistItem({ item, onRemove, onSaveNote }) {
  const [draft, setDraft] = useState(item.note);
  const link = safeUrl(item.url);
  const offset = formatOffset(item.vod_offset);
  const noteId = useId();

  useEffect(() => { setDraft(item.note); }, [item.note]);

  return (
    <li className="sl-item">
      <div className="sl-head">
        <div className="sl-thumb">{item.thumbnail_url && (item.thumbnail_url.startsWith("https://") || item.thumbnail_url.startsWith("data:image/")) ? <img src={item.thumbnail_url} alt="" loading="lazy" /> : null}</div>
        <div className="sl-meta">
          <p className="sl-title">{link ? <a href={link} target="_blank" rel="noopener noreferrer">{item.title}</a> : item.title}</p>
          <p className="card-sub">{item.streamer_name} · {formatDuration(item.duration)}{offset ? ` · starts ${offset} into the VOD` : ""}</p>
        </div>
        <button type="button" className="icon-btn" onClick={() => onRemove(item.clip_id)} aria-label={`Remove ${item.title}`}>×</button>
      </div>
      <label htmlFor={noteId} className="sr-only">Editing note for {item.title}</label>
      <textarea id={noteId} rows={2} maxLength={500} value={draft} placeholder="How would you cut this? Hook, music, caption ideas..."
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { if (draft.trim() !== item.note) onSaveNote(item.clip_id, draft); }} />
    </li>
  );
}

export function Shotlist({ open, onClose, board, boardId }) {
  const closeRef = useRef(null);
  const [copied, setCopied] = useState(false);
  const headingId = useId();

  useEffect(() => {
    if (!open) return undefined;
    closeRef.current?.focus();
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  async function copyLink() {
    const url = shareUrl(window.location.origin, boardId);
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt("Copy this link", url);
    }
  }

  if (!open) return null;
  return (
    <>
      <div className="backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby={headingId}>
        <header className="drawer-head">
          <h2 id={headingId}>Shotlist <span className="count">{board.items.length}</span></h2>
          <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Close shotlist">×</button>
        </header>
        {board.error && <p className="banner banner--error" role="alert">{board.error}</p>}
        {board.items.length === 0 ? (
          <div className="drawer-empty">
            <p><strong>Nothing saved yet.</strong></p>
            <p>Add clips as you scout, jot down how you would cut each one, then export the list as a CSV for your edit.</p>
          </div>
        ) : (
          <ul className="sl-list">
            {board.items.map((item) => (
              <ShotlistItem key={item.clip_id} item={item} onRemove={board.remove} onSaveNote={board.saveNote} />
            ))}
          </ul>
        )}
        <footer className="drawer-foot">
          <a className={`btn btn--primary${board.items.length === 0 ? " btn--disabled" : ""}`} href={api.exportUrl(boardId)}
            aria-disabled={board.items.length === 0} onClick={(e) => { if (board.items.length === 0) e.preventDefault(); }}>
            Export CSV
          </a>
          <button type="button" className="btn" onClick={copyLink}>{copied ? "Link copied" : "Copy share link"}</button>
          <p className="fine">Your shotlist has no login. Anyone with the share link can view and edit it, so treat the link like a password.</p>
        </footer>
      </aside>
    </>
  );
}
