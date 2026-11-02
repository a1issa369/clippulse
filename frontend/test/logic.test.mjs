import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, createApi } from "../src/api.js";
import { BOARD_KEY, isBoardId, randomBoardId, resolveBoardId, shareUrl } from "../src/board.js";
import {
  describeVelocity, formatAge, formatCount, formatDuration, formatOffset, formatVelocity, safeUrl, trendInfo,
} from "../src/format.js";

// ---------------------------------------------------------------- format
test("formatCount", () => {
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(999), "999");
  assert.equal(formatCount(1000), "1K");
  assert.equal(formatCount(1234), "1.2K");
  assert.equal(formatCount(98765), "98.8K");
  assert.equal(formatCount(123456), "123K");
  assert.equal(formatCount(1_250_000), "1.3M");
  assert.equal(formatCount(NaN), "0");
});

test("formatAge", () => {
  assert.equal(formatAge(0.01), "1m ago");
  assert.equal(formatAge(0.5), "30m ago");
  assert.equal(formatAge(5.9), "5h ago");
  assert.equal(formatAge(26), "1d ago");
  assert.equal(formatAge(-1), "just now");
});

test("formatDuration and formatOffset", () => {
  assert.equal(formatDuration(27), "0:27");
  assert.equal(formatDuration(75.4), "1:15");
  assert.equal(formatDuration(undefined), "0:00");
  assert.equal(formatOffset(90), "1:30");
  assert.equal(formatOffset(3725), "1:02:05");
  assert.equal(formatOffset(null), null);
  assert.equal(formatOffset(undefined), null);
  assert.equal(formatOffset(0), "0:00");
});

test("formatVelocity", () => {
  assert.equal(formatVelocity(312.4), "312");
  assert.equal(formatVelocity(42.6), "43");
  assert.equal(formatVelocity(8.44), "8.4");
  assert.equal(formatVelocity(2), "2");
  assert.equal(formatVelocity(0), "0");
});

test("trendInfo and describeVelocity", () => {
  assert.equal(trendInfo("up").label, "Surging");
  assert.equal(trendInfo(null), null);
  assert.match(describeVelocity({ velocity: 30, recent_velocity: 30 }), /last few hours/);
  assert.match(describeVelocity({ velocity: 4.2, recent_velocity: null }), /average since posted/);
});

test("safeUrl only allows https", () => {
  assert.equal(safeUrl("https://clips.twitch.tv/abc"), "https://clips.twitch.tv/abc");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("http://example.com"), null);
  assert.equal(safeUrl("not a url"), null);
  assert.equal(safeUrl(undefined), null);
});

// ---------------------------------------------------------------- board ids
const ID = "0123456789abcdef0123456789abcdef";

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { data, getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = v; } };
}

test("isBoardId and randomBoardId", () => {
  assert.ok(isBoardId(ID));
  for (const bad of ["", "short", ID.toUpperCase(), ID + "0", null, 5, "g".repeat(32)]) assert.ok(!isBoardId(bad));
  const generated = randomBoardId();
  assert.ok(isBoardId(generated));
  assert.notEqual(generated, randomBoardId());
});

test("resolveBoardId prefers a valid link, then storage, then generates", () => {
  const other = "f".repeat(32);
  const storage = memoryStorage({ [BOARD_KEY]: ID });
  assert.deepEqual(resolveBoardId({ search: `?board=${other}`, storage }), { id: other, fromLink: true });
  assert.equal(storage.data[BOARD_KEY], other);

  assert.deepEqual(resolveBoardId({ search: "", storage }), { id: other, fromLink: false });
  assert.deepEqual(resolveBoardId({ search: "?board=nonsense", storage }), { id: other, fromLink: false });

  const empty = memoryStorage();
  const fresh = resolveBoardId({ search: "", storage: empty, random: () => ID });
  assert.deepEqual(fresh, { id: ID, fromLink: false });
  assert.equal(empty.data[BOARD_KEY], ID);
});

test("resolveBoardId still works when storage throws", () => {
  const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.deepEqual(resolveBoardId({ search: "", storage: broken, random: () => ID }), { id: ID, fromLink: false });
  assert.deepEqual(resolveBoardId({ search: "", storage: undefined, random: () => ID }), { id: ID, fromLink: false });
});

test("shareUrl", () => assert.equal(shareUrl("https://x.test", ID), `https://x.test/?board=${ID}`));

// ---------------------------------------------------------------- api client
function fakeFetch(status, body) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return { ok: status >= 200 && status < 300, status, json: async () => { if (body === undefined) throw new Error("no body"); return body; } };
  };
  fn.calls = calls;
  return fn;
}

test("api builds URLs and bodies", async () => {
  const f = fakeFetch(200, { ok: true });
  const api = createApi(f, "/api");
  await api.getClips("some_user", { sort: "views", days: 3 });
  await api.getRising({ hours: 24 });
  await api.trackStreamer("amy");
  await api.saveItem(ID, "clip/1", { streamer: "amy", note: undefined });
  assert.equal(f.calls[0].url, "/api/streamers/some_user/clips?sort=views&days=3");
  assert.equal(f.calls[1].url, "/api/rising?hours=24&limit=24");
  assert.deepEqual(JSON.parse(f.calls[2].init.body), { login: "amy" });
  assert.equal(f.calls[2].init.method, "POST");
  assert.equal(f.calls[3].url, `/api/boards/${ID}/items/clip%2F1`);
  assert.deepEqual(JSON.parse(f.calls[3].init.body), { streamer: "amy" });
  assert.equal(api.exportUrl(ID), `/api/boards/${ID}/export.csv`);
});

test("api returns null for 204 and maps API errors", async () => {
  assert.equal(await createApi(fakeFetch(204)).removeItem(ID, "c"), null);

  const bad = createApi(fakeFetch(404, { error: { code: "not_found", message: "No Twitch channel named x." } }));
  await assert.rejects(bad.trackStreamer("x"), (err) => err instanceof ApiError && err.status === 404
    && err.code === "not_found" && err.message === "No Twitch channel named x.");

  const html = createApi(fakeFetch(502, undefined));
  await assert.rejects(html.listStreamers(), (err) => err.status === 502 && /502/.test(err.message));
});

test("api turns network failures into a friendly error but keeps aborts", async () => {
  const offline = createApi(async () => { throw new TypeError("Failed to fetch"); });
  await assert.rejects(offline.listStreamers(), (err) => err instanceof ApiError && err.code === "network");
  const aborted = createApi(async () => { const e = new Error("x"); e.name = "AbortError"; throw e; });
  await assert.rejects(aborted.listStreamers(), (err) => err.name === "AbortError");
});
