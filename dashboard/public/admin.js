/* Admin panel: password gate + delete previous giveaways. No framework. */
import { esc } from "./format.js";

const $ = (id) => document.getElementById(id);
const KEY = "gw_admin";
let password = sessionStorage.getItem(KEY) || "";

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

function showLogin() {
  $("login").hidden = false;
  $("panel").hidden = true;
  $("logout").hidden = true;
}

function showPanel() {
  $("login").hidden = true;
  $("panel").hidden = false;
  $("logout").hidden = false;
}

async function load() {
  const res = await fetch("/api/giveaways", { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`API ${res.status}`);
  const data = await res.json();
  const prev = data.previous ?? [];
  const list = $("prev-list");
  list.innerHTML = prev
    .map(
      (g) => `<div class="admin-item" data-id="${esc(g.id)}">
        <div>
          <div class="prize">${esc(g.prize)}</div>
          <div class="sub">${esc(g.status)} · ${g.entrants.count} entrant${g.entrants.count === 1 ? "" : "s"}</div>
        </div>
        <button class="danger" type="button" data-del="${esc(g.id)}">Delete</button>
      </div>`,
    )
    .join("");
  $("prev-empty").hidden = prev.length > 0;
  $("prev-count").textContent = prev.length ? `${prev.length} shown` : "";
  for (const btn of list.querySelectorAll("[data-del]")) {
    btn.addEventListener("click", () => remove(btn.dataset.del, btn));
  }
}

async function remove(id, btn) {
  const item = btn.closest(".admin-item");
  const name = item?.querySelector(".prize")?.textContent || id;
  if (!window.confirm(`Delete "${name}" forever? Its entry list goes with it.`)) return;
  btn.disabled = true;
  try {
    await api({ action: "delete", id });
    await load();
  } catch (err) {
    btn.disabled = false;
    const box = $("err");
    box.textContent = err.message;
    box.hidden = false;
  }
}

$("login").addEventListener("submit", async (e) => {
  e.preventDefault();
  password = $("pw").value;
  try {
    await api({ action: "ping" });
    sessionStorage.setItem(KEY, password);
    $("login-err").hidden = true;
    $("pw").value = "";
    showPanel();
    await load();
  } catch {
    password = "";
    sessionStorage.removeItem(KEY);
    const box = $("login-err");
    box.textContent = "Wrong password.";
    box.hidden = false;
  }
});

$("logout").addEventListener("click", () => {
  password = "";
  sessionStorage.removeItem(KEY);
  showLogin();
});

if (password) {
  api({ action: "ping" })
    .then(() => {
      showPanel();
      return load();
    })
    .catch(() => {
      password = "";
      sessionStorage.removeItem(KEY);
      showLogin();
    });
} else {
  showLogin();
}
