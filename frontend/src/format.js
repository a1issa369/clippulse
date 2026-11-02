// Small pure helpers for turning API numbers into text. Covered by test/format.test.mjs.

export function formatCount(n) {
  if (!Number.isFinite(n)) return "0";
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  const [value, suffix] = abs < 1_000_000 ? [n / 1000, "K"] : [n / 1_000_000, "M"];
  const text = value >= 100 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, "");
  return text + suffix;
}

export function formatAge(hours) {
  if (!Number.isFinite(hours) || hours < 0) return "just now";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))}m ago`;
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// Where the clip starts in the original broadcast, so an editor can find it in the VOD.
export function formatOffset(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return null;
  const total = Math.max(0, Math.floor(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

export function formatVelocity(perHour) {
  if (!Number.isFinite(perHour)) return "0";
  if (perHour >= 100) return String(Math.round(perHour));
  if (perHour >= 10) return perHour.toFixed(0);
  return perHour.toFixed(1).replace(/\.0$/, "");
}

const TRENDS = {
  up: { label: "Surging", tone: "up" },
  steady: { label: "Steady", tone: "steady" },
  cooling: { label: "Cooling", tone: "cooling" },
};

export function trendInfo(trend) {
  return TRENDS[trend] ?? null;
}

// Only ever link out to https pages, even though the data comes from our own API.
export function safeUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function velocityBasis(clip) {
  return clip.recent_velocity === null || clip.recent_velocity === undefined
    ? "average since posted"
    : "gained over the last few hours";
}

export function describeVelocity(clip) {
  return `${formatVelocity(clip.velocity)} views/hr ${velocityBasis(clip)}`;
}

// ---------------------------------------------------------------- growth chart

// Turn the API's [[iso timestamp, views], ...] into sorted, de-duplicated {t, v} points.
export function growthSeries(history) {
  if (!Array.isArray(history)) return [];
  const byTime = new Map();
  for (const pair of history) {
    if (!Array.isArray(pair)) continue;
    const t = Date.parse(pair[0]);
    const v = Number(pair[1]);
    if (Number.isFinite(t) && Number.isFinite(v) && v >= 0) byTime.set(t, v);
  }
  return [...byTime.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ t, v }));
}

// SVG geometry for a small line chart. Returns null when there is nothing to draw (fewer than 2 points).
export function sparkline(points, { width = 120, height = 32, pad = 3 } = {}) {
  if (!Array.isArray(points) || points.length < 2) return null;
  const t0 = points[0].t;
  const spanT = points[points.length - 1].t - t0 || 1;
  const lo = Math.min(...points.map((p) => p.v));
  const hi = Math.max(...points.map((p) => p.v));
  const spanV = hi - lo;
  const xy = points.map((p) => ({
    x: pad + ((p.t - t0) / spanT) * (width - 2 * pad),
    // A flat series sits mid-chart instead of dividing by zero.
    y: spanV === 0 ? height / 2 : height - pad - ((p.v - lo) / spanV) * (height - 2 * pad),
  }));
  const round = (n) => Math.round(n * 10) / 10;
  const line = xy.map((p, i) => `${i === 0 ? "M" : "L"}${round(p.x)} ${round(p.y)}`).join(" ");
  const last = xy[xy.length - 1];
  const area = `${line} L${round(last.x)} ${height} L${round(xy[0].x)} ${height} Z`;
  return { line, area, last: { x: round(last.x), y: round(last.y) }, width, height };
}

// Plain-language caption, also used as the chart's accessible label.
export function describeGrowth(points) {
  if (!Array.isArray(points) || points.length < 2) return "Not enough history yet to chart growth.";
  const first = points[0];
  const last = points[points.length - 1];
  const gained = Math.max(0, last.v - first.v);
  const hours = (last.t - first.t) / 3_600_000;
  const span = hours < 1 ? `${Math.max(1, Math.round(hours * 60))} minutes` : `${Math.round(hours * 10) / 10} hours`;
  return `Gained ${formatCount(gained)} views over ${span}, from ${formatCount(first.v)} to ${formatCount(last.v)}.`;
}

// Twitch's official clip embed. The clip id goes into a URL, so only accept the characters Twitch uses,
// and the page hostname is required by Twitch as the `parent` parameter.
export function embedUrl(clipId, hostname) {
  if (typeof clipId !== "string" || !/^[A-Za-z0-9_-]{1,100}$/.test(clipId)) return null;
  if (typeof hostname !== "string" || !/^[A-Za-z0-9.-]{1,253}$/.test(hostname)) return null;
  const params = new URLSearchParams({ clip: clipId, parent: hostname, autoplay: "false" });
  return `https://clips.twitch.tv/embed?${params}`;
}
