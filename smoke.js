// Tests the shared browser helpers (public/format.js) and the server card shape.
// Run: node smoke.js
import assert from "node:assert";
import { fmtDuration, ago, esc, cardHTML } from "./public/format.js";
import { card as serverCard } from "./api/_lib/turso.js";

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

console.log("ALL OK");