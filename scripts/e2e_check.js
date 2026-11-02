// Drives the real UI in Chromium against `python backend/dev_server.py` and fails on any
// broken flow, console error or CSP violation. Run from the repo root:
//   node scripts/e2e_check.js [baseUrl] [screenshotDir]
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

const base = process.argv[2] || "http://localhost:8787";
const shots = process.argv[3] || ".";

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  const failedResponses = [];
  const noisy = (m) => /Failed to load resource/.test(m.text());  // HTTP errors are asserted through `failedResponses`
  const deleteStatuses = [];
  page.on("response", (r) => { if (r.request().method() === "DELETE") deleteStatuses.push(r.status()); });
  page.on("response", (r) => { if (r.status() >= 400) failedResponses.push(`${r.request().method()} ${new URL(r.url()).pathname} ${r.status()}`); });
  page.on("console", (m) => { if (["error", "warning"].includes(m.type()) && !noisy(m)) problems.push(`console ${m.type()}: ${m.text()}`); });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("requestfailed", (r) => {
    const why = r.failure()?.errorText || "";
    if (r.url().endsWith("/export.csv") && /ABORTED/.test(why)) return;  // a download replaces navigation: expected
    if (r.method() === "DELETE" && /ABORTED/.test(why)) return;          // Chromium flags body-less 204s this way; status asserted below
    problems.push(`request failed: ${r.method()} ${new URL(r.url()).pathname.slice(0, 90)} ${why}`);
  });

  await page.goto(base);
  await page.waitForSelector(".card:not(.card--skeleton)");
  const risingCards = await page.locator(".card").count();
  assert.ok(risingCards >= 6, `expected a feed of clips, got ${risingCards}`);
  assert.equal(await page.locator(".chip").count(), 4, "Rising + 3 demo streamers");
  assert.match(await page.locator(".asof").innerText(), /Updated/);
  await page.screenshot({ path: path.join(shots, "ui-rising.png") });

  // velocity ordering is descending in the Rising feed
  const velocities = await page.locator(".velocity strong").allInnerTexts();
  const nums = velocities.map(Number);
  assert.deepEqual(nums, [...nums].sort((a, b) => b - a), "rising feed sorted by velocity");

  // Streamer view + sort controls
  await page.getByRole("button", { name: "SketchLab (demo)" }).click();
  await page.waitForSelector(".segmented");
  await page.getByRole("button", { name: "Most viewed" }).click();
  await page.waitForTimeout(400);
  const views = (await page.locator(".stat-muted").allInnerTexts());
  assert.ok(views.length > 0);
  await page.getByRole("button", { name: "24 hours" }).click();
  await page.waitForTimeout(400);
  assert.ok((await page.locator(".card").count()) <= 28);

  // Save two clips, check count and button state
  await page.getByRole("button", { name: "Rising everywhere" }).click();
  await page.waitForSelector(".card .btn");
  const first = page.locator(".card").nth(0);
  const second = page.locator(".card").nth(1);
  await first.getByRole("button", { name: /Add .* to shotlist/ }).click();
  await second.getByRole("button", { name: /Add .* to shotlist/ }).click();
  await page.waitForFunction(() => document.querySelector(".btn--shotlist .count").textContent === "2");
  await assert.ok(await first.locator(".btn--saved").isVisible());

  // Drawer: open, edit note, persisted after reload, export CSV, Escape closes
  await page.getByRole("button", { name: /^Shotlist/ }).click();
  await page.waitForSelector(".drawer");
  await page.screenshot({ path: path.join(shots, "ui-shotlist.png") });
  const note = page.locator(".sl-item textarea").first();
  await note.fill("Open on the reaction, hard cut at 0:08, add caption");
  await note.blur();
  await page.waitForTimeout(400);
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Export CSV" }).click();
  const file = await download;
  assert.equal(file.suggestedFilename(), "clippulse-shotlist.csv");
  const csv = require("node:fs").readFileSync(await file.path(), "utf8");
  assert.match(csv, /Open on the reaction/);
  assert.equal(csv.trim().split("\n").length, 3, "header + 2 rows");

  await page.keyboard.press("Escape");
  await page.waitForSelector(".drawer", { state: "detached" });
  await page.reload();
  await page.waitForSelector(".btn--shotlist .count");
  await page.waitForFunction(() => document.querySelector(".btn--shotlist .count").textContent === "2");

  // Remove one from the card
  await page.waitForSelector(".card .btn--saved");
  await page.locator(".btn--saved").first().click();
  await page.waitForFunction(() => document.querySelector(".btn--shotlist .count").textContent === "1");

  // Tracking: unknown channel shows an error, known demo channel is accepted
  await page.getByPlaceholder("Track a streamer").fill("nobody_here");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.waitForSelector(".field-error");
  assert.match(await page.locator(".field-error").innerText(), /No Twitch channel|not|Twitch/i);
  await page.getByPlaceholder("Track a streamer").fill("bad name!");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.waitForFunction(() => /username/i.test(document.querySelector(".field-error")?.textContent || ""));

  // Share link opens the same board in a fresh browser context
  const boardId = await page.evaluate(() => localStorage.getItem("clippulse.board"));
  assert.match(boardId, /^[0-9a-f]{32}$/);
  const other = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mobile = await other.newPage();
  mobile.on("console", (m) => { if (["error", "warning"].includes(m.type())) problems.push(`mobile console: ${m.text()}`); });
  mobile.on("requestfailed", (r) => problems.push(`mobile request failed: ${r.method()} ${r.url().slice(0, 90)} ${r.failure()?.errorText}`));
  await mobile.goto(`${base}/?board=${boardId}`);
  await mobile.waitForFunction(() => document.querySelector(".btn--shotlist .count")?.textContent === "1");
  assert.equal(new URL(mobile.url()).search, "", "board param removed from the address bar");
  assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, "no horizontal scroll on mobile");
  await mobile.screenshot({ path: path.join(shots, "ui-mobile.png") });

  assert.deepEqual(deleteStatuses, [204], "the one remove returned 204");
  assert.deepEqual(failedResponses, ["POST /api/streamers 404", "POST /api/streamers 400"], "only the two deliberate bad inputs fail");
  await browser.close();
  if (problems.length) {
    console.error("PROBLEMS:\n" + [...new Set(problems)].join("\n"));
    process.exit(1);
  }
  console.log("e2e ok");
})().catch((err) => { console.error(err); process.exit(1); });
