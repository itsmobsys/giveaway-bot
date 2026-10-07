/* Dashboard: live + previous giveaways. No framework, no build step.
   Pure helpers live in format.js so the browser and tests share one copy. */

import { fmtDuration, cardHTML, URGENT_MS } from "./format.js";

const REFRESH_MS = 15000;
const $ = (id) => document.getElementById(id);

/** Offset between the server clock and this browser, in ms (server - local). */
let clockSkew = 0;
const serverNow = () => Date.now() + clockSkew;

function setStatus(state, text) {
  $("status").dataset.state = state;
  $("status-text").textContent = text;
}

function render(target, items, emptyEl) {
  const grid = $(target);
  const has = items.length > 0;
  const now = serverNow();
  grid.innerHTML = has ? items.map((g) => cardHTML(g, now)).join("") : "";
  grid.hidden = !has;
  $(emptyEl).hidden = has;
}

function tickTimers() {
  const now = serverNow();
  for (const el of document.querySelectorAll(".timer")) {
    if (el.dataset.live !== "true") continue;
    const ms = Number(el.dataset.endsAt || 0) - now;
    // The clock is aria-hidden, so mutating it never interrupts a screen reader.
    el.querySelector(".clock").textContent = fmtDuration(ms);
    el.classList.toggle("urgent", ms > 0 && ms < URGENT_MS);
  }
}

let loading = false;

async function load(first = false) {
  if (loading) return;
  loading = true;
  if (first) setStatus("loading", "Loading");
  try {
    const res = await fetch("/api/giveaways", { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`API ${res.status}`);
    const data = await res.json();
    clockSkew = (data.now ?? Date.now()) + Number(res.headers.get("Age") || 0) * 1000 - Date.now();

    render("live-grid", data.live ?? [], "live-empty");
    render("prev-grid", data.previous ?? [], "prev-empty");
    $("live-count").textContent = data.live?.length ? `${data.live.length} running` : "";
    $("prev-count").textContent = data.previous?.length ? `${data.previous.length} shown` : "";
    $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
    $("err").hidden = true;
    setStatus("ok", "Live");
  } catch (err) {
    setStatus("error", "Offline");
    const box = $("err");
    box.textContent = `Could not load giveaways (${err.message}). Retrying…`;
    box.hidden = false;
  } finally {
    loading = false;
  }
}

$("refresh").addEventListener("click", () => load());

load(true);
setInterval(() => load(), REFRESH_MS);
setInterval(tickTimers, 1000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
});