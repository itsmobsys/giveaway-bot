// Browser regressions run against the actual public modules with a tiny DOM mock.
import assert from "node:assert/strict";

const previous = Object.fromEntries(
  ["document", "fetch", "sessionStorage", "CSS", "window", "setInterval", "setTimeout", "clearTimeout"]
    .map((key) => [key, globalThis[key]])
);
const restore = () => {
  for (const [key, value] of Object.entries(previous)) {
    if (value === undefined) delete globalThis[key];
    else globalThis[key] = value;
  }
};
const flush = () => new Promise((resolve) => setImmediate(resolve));

function element(id) {
  return {
    id, dataset: {}, hidden: false, disabled: false, value: "", innerHTML: "", textContent: "",
    appended: [], listeners: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(event, callback) { this.listeners[event] = callback; },
    setAttribute() {},
    append(text) { this.appended.push(text); this.textContent += text; },
    querySelectorAll() { return []; },
    querySelector() { return element("clock"); },
  };
}

try {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, element(id));
    return nodes.get(id);
  };
  const timer = element("timer");
  timer.dataset.live = "true";
  timer.clock = element("clock");
  timer.querySelector = () => timer.clock;
  const ticks = new Map();
  globalThis.document = {
    getElementById: get,
    querySelectorAll: (selector) => selector === ".timer" ? [timer] : [],
    addEventListener() {},
  };
  globalThis.setInterval = (callback, interval) => { ticks.set(interval, callback); return interval; };
  const expires = Date.now() + 60_000;
  timer.dataset.endsAt = String(expires);
  globalThis.fetch = async () => ({
    ok: true, headers: { get: (name) => name === "Age" ? "20" : null },
    json: async () => ({ now: Date.now() - 20_000, live: [], previous: [] }),
  });
  await import("./public/app.js?smoke-age");
  await flush();
  ticks.get(1000)();
  assert.match(timer.clock.textContent, /^00:5[89]$/, "cached Age corrects countdown skew");
  console.log("CDN Age clock correction ok");

  nodes.clear();
  let listCalls = 0;
  let pauseNextList = false;
  let releaseList;
  let failNextDelete = false;
  const items = Array.from({ length: 50 }, (_, n) => ({
    id: "gw_" + (n + 1), prize: "Prize " + (n + 1), status: "ended", entrants: { count: 0 },
    timer: { ended_at: Date.now() - 1000 },
  }));
  items.push({ id: "gw_last", prize: "Last prize", status: "ended", entrants: { count: 0 }, timer: { ended_at: Date.now() - 1000 } });
  const delButton = element("delete-button");
  const rowNode = element("row-node");
  get("rows").querySelectorAll = (selector) =>
    selector === "[data-del]" && get("rows").innerHTML.includes('data-del="' + delButton.dataset.del + '"')
      ? [delButton] : [];
  globalThis.document = {
    getElementById: get,
    querySelectorAll: () => [],
    querySelector: () => rowNode,
  };
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (key) => store.get(key) || null,
    setItem: (key, value) => store.set(key, value),
    removeItem: (key) => store.delete(key),
  };
  globalThis.CSS = { escape: (value) => value };
  globalThis.window = { confirm: () => true };
  globalThis.setTimeout = () => 1;
  globalThis.clearTimeout = () => {};
  globalThis.fetch = async (_url, init) => {
    const body = JSON.parse(init.body);
    const respond = (status, data) => ({ ok: status < 400, status, json: async () => data });
    if (body.action === "ping") return respond(200, { ok: true });
    if (body.action === "list") {
      listCalls++;
      const response = () => respond(200, {
        total: items.length, live: 1, previous: items.slice(body.offset, body.offset + body.limit),
      });
      if (pauseNextList) {
        pauseNextList = false;
        return new Promise((resolve) => { releaseList = () => resolve(response()); });
      }
      return response();
    }
    if (body.action === "delete") {
      if (failNextDelete) {
        failNextDelete = false;
        return respond(409, { deleted: 0, failed: [{ id: body.ids[0], error: "row <blocked>" }] });
      }
      for (const id of body.ids) {
        const index = items.findIndex((item) => item.id === id);
        if (index >= 0) items.splice(index, 1);
      }
      return respond(200, { deleted: body.ids.length, failed: [] });
    }
    throw new Error("Unexpected admin action " + body.action);
  };
  await import("./public/admin.js?smoke-admin");
  get("pw").value = "preview-test-only";
  await get("login").listeners.submit({ preventDefault() {} });
  assert.equal(get("page-count").textContent, "1–50 of 51");
  delButton.dataset.del = "gw_last";
  get("next-page").listeners.click();
  await flush();
  assert.equal(get("page-count").textContent, "51–51 of 51");
  delButton.listeners.click();
  await flush();
  assert.equal(get("page-count").textContent, "1–50 of 50", "deleting final row clamps to previous page");
  assert.equal(get("rows").innerHTML.includes("Last prize"), false);

  delButton.dataset.del = "gw_1";
  failNextDelete = true;
  const beforeFailure = listCalls;
  delButton.listeners.click();
  await flush();
  assert.equal(listCalls, beforeFailure + 1, "failed delete still reloads");
  assert.equal(get("toast").appended.at(-1), "row <blocked>", "failed[0].error appears as literal toast text");
  assert.equal(get("err").textContent, "row <blocked>");

  const beforeQueue = listCalls;
  pauseNextList = true;
  get("refresh").listeners.click();
  get("refresh").listeners.click();
  get("refresh").listeners.click();
  assert.equal(listCalls, beforeQueue + 1, "overlapping loads coalesce into one queued rerun");
  releaseList();
  await flush();
  assert.equal(listCalls, beforeQueue + 2, "queued rerun loads after busy request");
  console.log("admin pager, queued refresh, failed delete, and toast ok");
} finally {
  restore();
}
