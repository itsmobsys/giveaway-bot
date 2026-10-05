/* Pure formatting + rendering helpers, shared by the browser page and the tests. */

/** Human countdown: "45:12", "2:03:07", "1d 04:05:09". */
export function fmtDuration(ms) {
  if (ms <= 0) return "00:00";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (d > 0) return `${d}d ${pad(h)}:${pad(m)}:${pad(sec)}`;
  if (h > 0) return `${h}:${pad(m)}:${pad(sec)}`;
  return `${pad(m)}:${pad(sec)}`;
}

/** Relative past time: "just now", "5m ago", "3h ago", "2d ago". */
export function ago(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

/** Escape untrusted text (prize, host, username all reach innerHTML). */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

function face(name) {
  const label = String(name || "?").trim();
  return (
    `<span class="face"><i aria-hidden="true">${esc(label.slice(0, 1).toUpperCase())}</i>` +
    `<span title="${esc(label)}">${esc(label)}</span></span>`
  );
}

/** Under a minute left gets the urgent treatment. */
export const URGENT_MS = 60000;

/**
 * One giveaway card, showing only the 4 dashboard fields:
 * prize, entrants, chance, timer. `now` is the server-corrected clock in ms.
 */
export function cardHTML(g, now) {
  const live = g.status === "active";
  const statusTag =
    g.status === "cancelled"
      ? '<span class="tag cancelled">Cancelled</span>'
      : live
        ? '<span class="tag live">Live</span>'
        : '<span class="tag ended">Ended</span>';

  const count = Number(g.entrants?.count) || 0;
  const names = g.entrants?.usernames ?? [];
  const shown = names.slice(0, 8);
  const more = count - shown.length;
  const chance = g.chance ?? {};
  const pct = Math.max(0, Math.min(100, Number(chance.percent) || 0));

  const endsAt = Number(g.timer?.ends_at) || 0;
  const endedAt = Number(g.timer?.ended_at) || 0;
  const stamp = live ? endsAt : endedAt;
  const msLeft = endsAt - now;

  // The photo repeats the prize name in the heading right below it, so it is
  // decorative: empty alt keeps it out of the screen-reader flow.
  const img = g.image_url
    ? `<img class="card-img" src="${esc(g.image_url)}" alt="" width="570" height="148" loading="lazy" decoding="async" />`
    : "";

  const clockText = live ? fmtDuration(Number(g.timer?.ms_remaining) || 0) : ago(now - (endedAt || now));
  const timeLabel = stamp
    ? `<span class="sr-only">${live ? "Ends" : "Finished"}
         <time datetime="${new Date(stamp).toISOString()}">${new Date(stamp).toLocaleString()}</time></span>`
    : "";

  const timerState = live ? (msLeft > 0 && msLeft < URGENT_MS ? " urgent" : " is-live") : " ended";

  return `<article class="card">
  ${img}
  <div class="card-body">
    <h3 class="prize">${esc(g.prize)}</h3>
    <div class="meta">
      ${statusTag}
      ${g.host_name ? `<span class="tag">by ${esc(g.host_name)}</span>` : ""}
    </div>

    <div class="odds">
      <div class="odds-row">
        <span>Your chance</span>
        <span class="odds-num">${pct.toFixed(pct < 1 && pct > 0 ? 2 : 1)}<small>%</small></span>
      </div>
      <div class="bar"><i style="width:${pct}%"></i></div>
      <div class="odds-row"><span>${esc(chance.text ?? "")}</span>${
        chance.one_in ? `<span>1 in ${esc(chance.one_in)}</span>` : ""
      }</div>
    </div>

    <div class="timer${timerState}" data-ends-at="${live ? endsAt : ""}" data-live="${live}" role="timer" aria-live="off">
      <div>
        <span class="clock" aria-hidden="true">${clockText}</span>
        <small aria-hidden="true">${live ? "remaining" : "finished"}</small>
        ${timeLabel}
      </div>
    </div>

    <div class="entrants-head">
      <span>${count} entrant${count === 1 ? "" : "s"}</span>
    </div>
    ${
      shown.length
        ? `<div class="faces">${shown.map(face).join("")}${
            more > 0 ? `<span class="more">+${more} more</span>` : ""
          }</div>`
        : ""
    }
  </div>
</article>`;
}
