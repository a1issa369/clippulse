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

export function describeVelocity(clip) {
  const basis = clip.recent_velocity === null || clip.recent_velocity === undefined
    ? "average since posted"
    : "gained over the last few hours";
  return `${formatVelocity(clip.velocity)} views/hr ${basis}`;
}
