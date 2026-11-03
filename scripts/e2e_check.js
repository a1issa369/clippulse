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

  // Growth charts on the cards, and the clip player. Twitch's player is stubbed so the check is offline,
  // but the iframe still has to pass the page's Content Security Policy (frame-src) to load at all.
  await context.route("https://clips.twitch.tv/**", (route) =>
    route.fulfill({ contentType: "text/html", body: "<!doctype html><p>stub twitch player</p>" }));
  await page.getByRole("button", { name: "Rising everywhere" }).click();
  await page.waitForSelector(".card .spark");
  assert.match(await page.locator(".card .spark").first().getAttribute("aria-label"), /^Gained .* views over/);
  const playButton = page.locator(".thumb-play").first();
  await playButton.click();
  await page.waitForSelector(".modal iframe");
  const embed = new URL(await page.locator(".modal iframe").getAttribute("src"));
  assert.equal(embed.origin + embed.pathname, "https://clips.twitch.tv/embed");
  assert.equal(embed.searchParams.get("parent"), "localhost");
  assert.match(embed.searchParams.get("clip"), /^[A-Za-z0-9_-]+$/);
  const frame = page.frames().find((f) => f.url().startsWith("https://clips.twitch.tv/"));
  await frame.waitForSelector("p");                                    // the framed page really loaded, so CSP allowed it
  assert.ok(await page.locator(".modal .spark--large").isVisible(), "growth chart in the player");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Close player");
  for (let i = 0; i < 6; i += 1) {                                     // Tab never escapes the dialog
    await page.keyboard.press("Tab");
    assert.ok(await page.evaluate(() => !!document.activeElement.closest(".modal") || document.activeElement.tagName === "IFRAME"));
  }
  await page.screenshot({ path: path.join(shots, "ui-player.png") });
  await page.keyboard.press("Escape");
  await page.waitForSelector(".modal", { state: "detached" });
  assert.ok(await playButton.evaluate((el) => el === document.activeElement), "focus returns to the play button");

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

  // Searching: anyone can look up any streamer. Bad input and unknown channels explain themselves,
  // a real channel is added as a chip and its clips load, and a visitor who keeps looking up new names is rate limited.
  const search = page.getByPlaceholder("Search any streamer");
  // Click Search, then wait for the request to finish and the button to come back, so one search never overlaps the next.
  const submit = async () => {
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/api/streamers") && r.request().method() === "POST"),
      page.getByRole("button", { name: "Search", exact: true }).click(),
    ]);
    await page.waitForFunction(() => !/Searching/.test(document.querySelector(".track button")?.textContent || ""));
  };
  assert.ok(await search.isVisible(), "the search box is public");
  await search.fill("nobody_here");
  await submit();
  await page.waitForSelector(".field-error");
  assert.match(await page.locator(".field-error").innerText(), /No Twitch channel/i);
  await search.fill("bad name!");
  await submit();
  await page.waitForFunction(() => /username/i.test(document.querySelector(".field-error")?.textContent || ""));

  await search.fill("demo_guest");
  await submit();
  await page.getByRole("button", { name: "Guest Channel (demo)", exact: true }).waitFor();
  assert.equal(await page.locator(".chip").count(), 5, "the searched streamer joined the list");
  await page.waitForSelector(".card:not(.card--skeleton)");
  assert.ok((await page.locator(".card").count()) > 0, "their clips loaded");
  assert.equal(await page.getByPlaceholder("Search any streamer").inputValue(), "", "the box clears after a search");
  await search.fill("demo_guest");                                   // searching again just finds the existing one
  await submit();
  await page.waitForFunction(() => document.querySelector(".field-error") === null);
  assert.equal(await page.locator(".chip").count(), 5, "no duplicate chip");

  // Only the visitor's own streamer has a remove button. Pinned ones never do.
  assert.equal(await page.locator(".chip-x").count(), 1, "one remove button, on the streamer this visitor added");
  await page.getByRole("button", { name: "Stop tracking Guest Channel (demo)" }).click();
  await page.waitForFunction(() => document.querySelectorAll(".chip").length === 4);
  assert.equal(await page.locator(".chip-x").count(), 0, "pinned streamers cannot be removed");
  await search.fill("demo_guest");                                   // adding it back costs a fresh lookup
  await submit();
  await page.getByRole("button", { name: "Guest Channel (demo)", exact: true }).waitFor();
  assert.equal(await page.locator(".chip").count(), 5);

  // Most Twitch accounts have no recent clips. That is said plainly and does not take a slot.
  await search.fill("demo_quiet");
  await submit();
  await page.waitForFunction(() => /no clips from the last 7 days/i.test(document.querySelector(".field-error")?.textContent || ""));
  assert.equal(await page.locator(".chip").count(), 5, "a clipless account does not become a chip");

  for (const name of ["nobody_aaa"]) {                               // 5 new lookups are allowed per visitor per window
    await search.fill(name);
    await submit();
    await page.waitForFunction(() => /No Twitch channel/.test(document.querySelector(".field-error")?.textContent || ""));
    await search.fill("");
  }
  await search.fill("nobody_ddd");
  await submit();
  await page.waitForFunction(() => /too quickly/i.test(document.querySelector(".field-error")?.textContent || ""));
  assert.match(await page.locator(".field-error").innerText(), /Try again in \d+ (seconds|minutes)/);

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

  assert.deepEqual(deleteStatuses, [204, 204], "removing a saved clip and removing an own streamer both returned 204");
  assert.deepEqual(failedResponses, [
    "POST /api/streamers 404", "POST /api/streamers 400",
    "POST /api/streamers 404", "POST /api/streamers 404", "POST /api/streamers 429",
  ], "only the deliberate bad inputs and the rate limit fail");
  await browser.close();
  if (problems.length) {
    console.error("PROBLEMS:\n" + [...new Set(problems)].join("\n"));
    process.exit(1);
  }
  console.log("e2e ok");
})().catch((err) => { console.error(err); process.exit(1); });
