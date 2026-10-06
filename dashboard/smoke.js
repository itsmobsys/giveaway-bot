// Tests the shared browser helpers (public/format.js) and the server card shape.
// Run: node smoke.js
import assert from "node:assert";
import { fmtDuration, ago, esc, cardHTML } from "./public/format.js";
import { card as serverCard, placeholders } from "./api/_lib/turso.js";
import pageHandler from "./api/page.js";

// -- time formatting ------------------------------------------------------
assert.strictEqual(fmtDuration(0), "00:00");
assert.strictEqual(fmtDuration(-5000), "00:00");
assert.strictEqual(fmtDuration(59_000), "00:59");
assert.strictEqual(fmtDuration(3_600_000), "1:00:00");
assert.strictEqual(fmtDuration(90_000), "01:30");
assert.strictEqual(fmtDuration(86_400_000 + 3_660_000), "1d 01:01:00");
assert.strictEqual(fmtDuration(999), "00:00");
console.log("fmtDuration ok");

assert.strictEqual(ago(30_000), "just now");
assert.strictEqual(ago(5 * 60_000), "5m ago");
assert.strictEqual(ago(3 * 3600_000), "3h ago");
assert.strictEqual(ago(50 * 3600_000), "2d ago");
console.log("ago ok");

// -- escaping (XSS: prize/host/username all flow through innerHTML) ------
assert.strictEqual(esc('<img src=x onerror="alert(1)">'), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
assert.strictEqual(esc("a & b"), "a &amp; b");
assert.strictEqual(esc(null), "");
console.log("esc ok");

// -- card rendering against the real API shape ---------------------------
const now = Date.now();
const live = {
  id: "gw_abc",
  status: "active",
  prize: "Steam $20 <card>",
  image_url: "https://cdn.example/pic.png",
  host_name: "Mod",
  entrants: { count: 14, usernames: ["Ann", "Zed", "Bo", "Cy", "Dee", "Eli", "Fay", "Gus", "Hal"] },
  chance: { winners: 1, entrants: 14, percent: 7.14, one_in: 14, text: "1 winner / 14 entrants" },
  timer: { ends_at: now + 3_600_000, ended_at: null, ms_remaining: 3_600_000, seconds_remaining: 3600, is_live: true },
};
const html = cardHTML(live, now);
assert.ok(html.includes("Steam $20 &lt;card&gt;"), "prize must be escaped");
assert.ok(!html.includes("<card>"), "no raw tag from prize");
assert.ok(html.includes("1:00:00"), "countdown rendered");
assert.ok(html.includes('data-live="true"'), "live timer marked for ticking");
assert.ok(html.includes("1 in 14"), "odds one_in");
// 14 entrants total, 8 chips shown (the API caps the list at 100 names).
assert.ok(html.includes("+6 more"), "overflow chip counts unreshown entrants");
assert.strictEqual((html.match(/class="face"/g) || []).length, 8);
assert.ok(html.includes("14 entrants"), "entrant count");
assert.ok(html.includes('<img class="card-img"'), "prize photo");
assert.ok(html.includes('alt=""') && html.includes('height="148"'), "decorative photo with reserved height");
assert.ok(html.trimStart().startsWith("<article"), "card is a landmark element, not a div");
console.log("live card ok");

// -- timer accessibility ---------------------------------------------------
assert.ok(html.includes('role="timer"') && html.includes('aria-live="off"'), "ticking timer must not announce every second");
assert.ok(html.includes('<span class="clock" aria-hidden="true">'), "visual clock hidden from screen readers");
assert.ok(/<time datetime="\d{4}-\d{2}-\d{2}T/.test(html), "absolute end time exposed once");
assert.ok(html.includes("Ends"), "accessible label for the live end time");
assert.ok(html.includes('class="timer is-live"'), "live timer styled live");
const urgent = { ...live, timer: { ...live.timer, ends_at: now + 20_000, ms_remaining: 20_000 } };
assert.ok(cardHTML(urgent, now).includes('class="timer urgent"'), "under a minute -> urgent");
assert.ok(!cardHTML(live, now).includes("urgent"), "plenty of time -> not urgent");
console.log("timer a11y ok");

// -- username escaping (usernames come from Discord) ----------------------
const evil = cardHTML(
  { ...live, entrants: { count: 1, usernames: ['<img src=x onerror="alert(1)">'] } },
  now
);
assert.ok(!evil.includes("<img src=x"), "username cannot inject markup");
assert.ok(evil.includes("&lt;img src=x"), "username is escaped");
console.log("username escaping ok");

const ended = {
  id: "gw_old", status: "ended", prize: "Nitro", image_url: null, host_name: null,
  entrants: { count: 0, usernames: [] },
  chance: { winners: 1, entrants: 0, percent: 0, one_in: null, text: "No entries yet" },
  timer: { ends_at: now - 7200_000, ended_at: now - 7200_000, ms_remaining: 0, seconds_remaining: 0, is_live: false },
};
const ehtml = cardHTML(ended, now);
assert.ok(ehtml.includes("2h ago"), "ended card shows relative time");
assert.ok(ehtml.includes("Ended"), "ended tag");
assert.strictEqual(ehtml.includes('class="card-img"'), false, "no photo -> no img tag");
assert.strictEqual(ehtml.includes("more"), false, "no entrants -> no overflow chip");
assert.ok(!ehtml.includes("data-ends-at=\"undefined\""), "no undefined in attrs");
assert.ok(ehtml.includes('class="timer ended"'), "ended timer styled ended");
assert.ok(ehtml.includes('data-live="false"'), "ended timer is not ticked");
assert.ok(ehtml.includes("Finished"), "accessible label for the finish time");
console.log("ended card ok");

assert.ok(cardHTML({ ...ended, status: "cancelled", prize: "Void drop" }, now).includes("Cancelled"));
console.log("cancelled card ok");

// -- server card shape feeds the view without gaps ------------------------
const sc = serverCard(
  { id: "gw_s", status: "active", prize: "Nitro", winner_count: 3, ends_at: now + 60_000, ended_at: null, image_url: null, host_name: "Mod" },
  12, ["Ann", "Zed"], now
);
assert.strictEqual(sc.entrants.count, 12);
assert.strictEqual(sc.chance.winners, 3);
assert.strictEqual(sc.chance.percent, 25);
assert.strictEqual(sc.chance.one_in, 4);
assert.ok(sc.timer.is_live && sc.timer.ms_remaining > 0);
const round = cardHTML(sc, now);
for (const key of ["Nitro", "25", "1 in 4", "12 entrants", "Live"]) {
  assert.ok(round.includes(key), `rendered card missing ${key}`);
}
console.log("server->view round trip ok");

// -- placeholder building (shared by the grouped queries) ------------------
assert.strictEqual(placeholders(1), "?");
assert.strictEqual(placeholders(3), "?,?,?");
assert.strictEqual(placeholders(0), "");
assert.strictEqual(placeholders(-2), "");
assert.strictEqual(placeholders(2.5), "");
console.log("placeholders ok");

// -- the page function: routing, caching, and hostile query strings --------
function mockRes() {
  const res = { statusCode: null, headers: {}, body: null };
  res.setHeader = (k, v) => {
    res.headers[String(k).toLowerCase()] = v;
  };
  res.status = (code) => {
    res.statusCode = code;
    return res;
  };
  res.send = (body) => {
    res.body = body;
    return res;
  };
  return res;
}

function fetchPage(url) {
  const res = mockRes();
  pageHandler({ url }, res);
  return res;
}

const home = fetchPage("/");
assert.strictEqual(home.statusCode, 200);
assert.ok(home.body.toLowerCase().startsWith("<!doctype html"), "index is served at /");
assert.ok(home.headers["content-type"].includes("text/html"));
assert.ok(home.headers["cache-control"].includes("s-maxage=30"));

const css = fetchPage("/styles.css");
assert.strictEqual(css.statusCode, 200);
assert.ok(css.headers["content-type"].includes("text/css"));
assert.ok(css.headers["cache-control"].includes("immutable"), "static assets stay cached");
assert.ok(fetchPage("/app.js").body.includes("REFRESH_MS"), "app.js is baked in");
assert.ok(fetchPage("/format.js").body.includes("cardHTML"), "format.js is baked in");
assert.ok(fetchPage("/admin.js").body.includes("password"), "admin.js is baked in");

const adminPage = fetchPage("/admin");
assert.strictEqual(adminPage.statusCode, 200);
assert.ok(adminPage.headers["cache-control"].includes("no-store"), "the admin page is never cached");
assert.strictEqual(fetchPage("/admin.html").statusCode, 200);

assert.strictEqual(fetchPage("/api/page?f=index.html").statusCode, 200);
assert.strictEqual(fetchPage("/?f=styles.css").statusCode, 200);
assert.strictEqual(fetchPage("/nope").statusCode, 404);
assert.strictEqual(fetchPage("/health").statusCode, 404, "the page function only serves page assets");
// Regression: a plain truthiness lookup made these resolve to Object.prototype
// values (constructor, toString, __proto__), which then crashed on file.slice
// and turned a junk URL into a 500.
for (const hostile of ["/constructor", "/toString", "/__proto__", "/valueOf"]) {
  const res = fetchPage(hostile);
  assert.strictEqual(res.statusCode, 404, hostile + " must 404, got " + res.statusCode);
  assert.strictEqual(res.body, "unknown page asset");
}
// A junk ?f= is not an error: it falls back to the pathname mapping, which
// itself can only ever name one of the six baked assets.
assert.strictEqual(fetchPage("/?f=missing.css").statusCode, 200);
for (const hostile of ["/?f=constructor", "/?f=toString", "/?f=__proto__", "/?f=hasOwnProperty"]) {
  const res = fetchPage(hostile);
  assert.strictEqual(res.statusCode, 200, hostile + " falls back to the index page");
  assert.strictEqual(typeof res.body, "string");
  assert.ok(res.body.toLowerCase().startsWith("<!doctype html"), "a real asset, never a prototype value");
}
const traversal = fetchPage("/?f=../api/admin.js");
assert.strictEqual(traversal.statusCode, 200);
assert.ok(
  !String(traversal.body).includes("duggalbadmoshnahirahalol"),
  "no query string can reach a file outside the baked set"
);
assert.ok(!String(fetchPage("/?f=../../.env").body).includes("TURSO"), "and not the environment either");
const broken = mockRes();
pageHandler({ url: "http://[::1" }, broken);
assert.strictEqual(broken.statusCode, 404, "an unparseable url is a 404, not a 500");
console.log("page routing ok");

// -- server card edge cases -----------------------------------------------
const zero = serverCard(
  { id: "gw_z", status: "active", prize: "Nothing", winner_count: 0, ends_at: now + 1000, ended_at: null },
  0, [], now
);
assert.strictEqual(zero.chance.winners, 1, "winner_count 0 falls back to 1");
assert.strictEqual(zero.chance.one_in, null);
assert.strictEqual(zero.timer.seconds_remaining, 1);
const odd = serverCard(
  { id: "gw_o", status: "ended", prize: "P", winner_count: 2, ends_at: now - 10, ended_at: now - 5 },
  3, ["a"], now
);
assert.strictEqual(odd.timer.is_live, false);
assert.strictEqual(odd.timer.ms_remaining, 0, "an ended giveaway has no time left");
assert.strictEqual(odd.timer.ended_at, now - 5);
assert.strictEqual(odd.chance.percent, 66.67);
assert.strictEqual(odd.chance.one_in, 1.5);
const overflowing = serverCard(
  { id: "gw_x", status: "active", prize: "P", winner_count: 9, ends_at: now + 1000, ended_at: null },
  2, [], now
);
assert.strictEqual(overflowing.chance.percent, 100, "odds never exceed 100%");
console.log("card edge cases ok");

console.log("ALL OK");
