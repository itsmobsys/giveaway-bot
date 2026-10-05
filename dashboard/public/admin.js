/* Admin panel: password gate, then a paged/filterable list of finished
   giveaways with single + bulk delete. No framework, no build step. */
import { ago, esc } from "./format.js";

const $ = (id) => document.getElementById(id);
const KEY = "gw_admin";
const PAGE = 50;

let password = sessionStorage.getItem(KEY) || "";

/** Loaded page + the totals the server reports for the whole table. */
const state = {
  items: [],
  total: 0,
  live: 0,
  offset: 0,
  filter: "all",
  query: "",
  selected: new Set(),
  busy: false,
};

const ICON_TRASH =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';
const ICON_GIFT =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9Z"/><path d="M3 11h18M12 7v15"/><path d="M12 7H8.5A2.5 2.5 0 1 1 11 4.5V7ZM12 7h3.5A2.5 2.5 0 1 0 13 4.5V7Z"/></svg>';

/* -- transport -- */

async function api(body) {
  const res = await fetch("/api/admin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password, ...body }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

/* -- chrome: status pill + toast -- */

function setStatus(text, kind) {
  $("status-text").textContent = text;
  $("status").dataset.state = kind;
}

let toastTimer = 0;
function toast(message, kind = "ok") {
  const box = $("toast");
  box.className = `toast ${kind}`;
  box.innerHTML =
    kind === "ok"
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>'
      : ICON_TRASH;
  box.append(esc(message));
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, 3200);
}

function showError(err) {
  $("err").textContent = err?.message || String(err);
  $("err").hidden = false;
}

/* -- confirm dialog -- */

function confirmAsk(title, text) {
  const dlg = $("confirm");
  // Every current browser has <dialog>; keep a fallback for anything odd.
  if (typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(`${title} ${text}`));
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  return new Promise((resolve) => {
    const done = (ok) => {
      $("confirm-yes").removeEventListener("click", yes);
      $("confirm-no").removeEventListener("click", no);
      dlg.removeEventListener("close", closed);
      dlg.open && dlg.close();
      resolve(ok);
    };
    const yes = () => done(true);
    const no = () => done(false);
    const closed = () => done(false);
    $("confirm-yes").addEventListener("click", yes);
    $("confirm-no").addEventListener("click", no);
    dlg.addEventListener("close", closed);
    dlg.showModal();
  });
}

/* -- views -- */

function showLogin() {
  $("login-view").hidden = false;
  $("panel").hidden = true;
  $("logout").hidden = true;
  setStatus("Locked", "ok");
}

function showPanel() {
  $("login-view").hidden = true;
  $("panel").hidden = false;
  $("logout").hidden = false;
}

/* -- data -- */

async function load() {
  if (state.busy) return;
  state.busy = true;
  $("err").hidden = true;
  setStatus("Syncing", "loading");
  try {
    const data = await api({ action: "list", limit: PAGE, offset: state.offset });
    state.items = data.previous || [];
    state.total = Number(data.total) || 0;
    state.live = Number(data.live) || 0;
    // Anything selected may have just been deleted server-side.
    const present = new Set(state.items.map((g) => g.id));
    for (const id of [...state.selected]) if (!present.has(id)) state.selected.delete(id);
    setStatus("Online", "ok");
    render();
  } catch (err) {
    setStatus("Error", "error");
    showError(err);
  } finally {
    state.busy = false;
  }
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.items.filter((g) => {
    if (state.filter !== "all" && g.status !== state.filter) return false;
    if (!q) return true;
    return `${g.prize} ${g.host_name || ""} ${g.id}`.toLowerCase().includes(q);
  });
}

function thumb(g) {
  return g.image_url
    ? `<img class="thumb" src="${esc(g.image_url)}" alt="" width="54" height="46" loading="lazy" decoding="async" />`
    : `<span class="thumb thumb-ph" aria-hidden="true">${ICON_GIFT}</span>`;
}

function row(g, now) {
  const n = Number(g.entrants?.count) || 0;
  const endedAt = Number(g.timer?.ended_at) || 0;
  const tag =
    g.status === "cancelled"
      ? '<span class="tag cancelled">Cancelled</span>'
      : '<span class="tag ended">Ended</span>';
  const when = endedAt
    ? `<time datetime="${new Date(endedAt).toISOString()}" title="${esc(new Date(endedAt).toLocaleString())}">${ago(now - endedAt)}</time>`
    : "";

  return `<div class="row${state.selected.has(g.id) ? " on" : ""}" data-id="${esc(g.id)}">
      <label class="pick">
        <input class="tick" type="checkbox" data-pick="${esc(g.id)}" aria-label="Select ${esc(g.prize)}"${state.selected.has(g.id) ? " checked" : ""} />
      </label>
      ${thumb(g)}
      <div class="row-main">
        <div class="row-prize">${esc(g.prize)}</div>
        <div class="row-meta">
          ${tag}
          ${g.host_name ? `<span>by ${esc(g.host_name)}</span>` : ""}
          <span>${n} entrant${n === 1 ? "" : "s"}</span>
          <code>${esc(g.id)}</code>
        </div>
      </div>
      <div class="row-side">
        ${when}
        <button class="btn btn-danger btn-sm" type="button" data-del="${esc(g.id)}" aria-label="Delete ${esc(g.prize)}">
          ${ICON_TRASH} Delete
        </button>
      </div>
    </div>`;
}

function render() {
  const now = Date.now();
  const list = visible();

  $("rows").innerHTML = list.map((g) => row(g, now)).join("");
  $("rows-empty").hidden = list.length > 0;
  $("rows-empty").textContent = state.items.length
    ? "No giveaway matches this filter."
    : "No finished giveaways yet.";

  const shown = list.length;
  $("shown-count").textContent = shown ? `${shown} shown` : "";
  $("stat-total").textContent = state.total;
  $("stat-live").textContent = state.live;
  $("stat-entrants").textContent = state.items.reduce((a, g) => a + (Number(g.entrants?.count) || 0), 0);

  // pager: newest first, so "Newer" walks back towards now.
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + PAGE, state.total);
  $("pager").hidden = state.total <= PAGE;
  $("page-count").textContent = `${from}–${to} of ${state.total}`;
  $("prev-page").disabled = state.offset === 0;
  $("next-page").disabled = to >= state.total;

  syncSelbar();

  for (const el of $("rows").querySelectorAll("[data-del]")) {
    el.addEventListener("click", () => removeOne(el.dataset.del));
  }
  for (const el of $("rows").querySelectorAll("[data-pick]")) {
    el.addEventListener("change", () => {
      const id = el.dataset.pick;
      if (el.checked) state.selected.add(id);
      else state.selected.delete(id);
      el.closest(".row").classList.toggle("on", el.checked);
      syncSelbar();
    });
  }
}

function syncSelbar() {
  const n = state.selected.size;
  $("selbar").hidden = n === 0;
  $("sel-count").textContent = `${n} selected`;
}

/* -- actions -- */

async function deleteIds(ids) {
  if (!ids.length) return;
  const rows = ids.map((id) => document.querySelector(`.row[data-id="${CSS.escape(id)}"]`)).filter(Boolean);
  rows.forEach((r) => r.classList.add("going"));
  try {
    const data = await api({ action: "delete", ids });
    ids.forEach((id) => state.selected.delete(id));
    const failed = data.failed || [];
    if (failed.length) toast(`${data.deleted} deleted · ${failed.length} failed`, "bad");
    else toast(`${data.deleted} giveaway${data.deleted === 1 ? "" : "s"} deleted`);
    await load();
  } catch (err) {
    rows.forEach((r) => r.classList.remove("going"));
    showError(err);
    toast(err.message, "bad");
  }
}

async function removeOne(id) {
  const g = state.items.find((x) => x.id === id);
  const name = g?.prize || id;
  const ok = await confirmAsk(
    `Delete “${name}”?`,
    "The giveaway and its entry list are removed from the database. This cannot be undone.",
  );
  if (!ok) return;
  await deleteIds([id]);
}

/* -- events -- */

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("unlock");
  btn.disabled = true;
  password = $("pw").value;
  try {
    await api({ action: "ping" });
    sessionStorage.setItem(KEY, password);
    $("login-err").hidden = true;
    $("pw").value = "";
    showPanel();
    setStatus("Online", "ok");
    await load();
  } catch {
    password = "";
    sessionStorage.removeItem(KEY);
    const box = $("login-err");
    box.textContent = "Wrong password.";
    box.hidden = false;
    showLogin();
  } finally {
    btn.disabled = false;
  }
});

$("logout").addEventListener("click", () => {
  password = "";
  sessionStorage.removeItem(KEY);
  state.selected.clear();
  state.offset = 0;
  showLogin();
});

$("refresh").addEventListener("click", load);

$("q").addEventListener("input", (e) => {
  state.query = e.target.value;
  render();
});

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    state.filter = chip.dataset.filter;
    for (const c of document.querySelectorAll(".chip")) {
      c.setAttribute("aria-pressed", String(c === chip));
    }
    render();
  });
}

$("prev-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE);
  load();
});

$("next-page").addEventListener("click", () => {
  if (state.offset + PAGE < state.total) {
    state.offset += PAGE;
    load();
  }
});

$("clear-sel").addEventListener("click", () => {
  state.selected.clear();
  render();
});

$("bulk-del").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = await confirmAsk(
    `Delete ${ids.length} giveaway${ids.length === 1 ? "" : "s"}?`,
    "The giveaways and their entry lists are removed from the database. This cannot be undone.",
  );
  if (ok) await deleteIds(ids);
});

/* -- boot -- */

if (password) {
  showPanel();
  api({ action: "ping" })
    .then(load)
    .catch(() => {
      password = "";
      sessionStorage.removeItem(KEY);
      showLogin();
    });
} else {
  showLogin();
}