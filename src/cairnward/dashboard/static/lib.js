// Small helpers. Elements are built with textContent only: data is never parsed as HTML.

let csrf = "";
export function setCsrf(token) { csrf = token || ""; }

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    opts.headers["X-CSRF-Token"] = csrf;
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body ?? {});
  }
  const resp = await fetch(path, opts);
  if (resp.status === 401) {
    showSignedOut();
    throw new ApiError(401, "not signed in");
  }
  const type = resp.headers.get("content-type") || "";
  const data = type.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) throw new ApiError(resp.status, (data && data.detail) || String(data));
  return data;
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Append children (arrays flattened, null/false skipped, strings as text).
export function put(el, ...children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function table(columns, rows, { onRow, selected } = {}) {
  const head = h("thead", {}, h("tr", {}, columns.map(c =>
    h("th", { scope: "col", class: c.num ? "num" : null }, c.label || h("span", { class: "sr" }, c.srLabel || "")))));
  const body = h("tbody", {}, rows.map((r, i) => {
    const tr = h("tr", { class: selected === i ? "selected" : null },
      columns.map(c => h("td", { class: [c.num ? "num" : "", c.mono ? "mono" : ""].join(" ").trim() || null },
        c.render ? c.render(r) : (r[c.key] ?? ""))));
    if (onRow) tr.addEventListener("click", () => onRow(r, i));
    return tr;
  }));
  return h("table", {}, head, body);
}

export function toast(message, { action, onAction, timeout = 6000 } = {}) {
  const box = document.getElementById("toast");
  const item = h("div", {}, h("span", {}, message));
  if (action) {
    item.append(h("button", { type: "button", onclick: () => { onAction(); item.remove(); } }, action));
  }
  box.append(item);
  setTimeout(() => item.remove(), timeout);
}

// A modal dialog built from fields; resolves with the values, or null when cancelled.
export function ask(title, { text, fields = [], confirm = "OK", danger = false, warn } = {}) {
  const dlg = document.getElementById("dialog");
  clear(dlg);
  return new Promise(resolve => {
    const inputs = {};
    const form = h("form", { method: "dialog" },
      h("h2", { id: "dialog-title" }, title),
      warn ? h("div", { class: "warnbox", role: "alert" }, warn) : null,
      text ? (Array.isArray(text) ? text.map(t => h("p", {}, t)) : h("p", {}, text)) : null,
      fields.map(f => {
        const input = f.options
          ? h("select", { name: f.name }, f.options.map(o => h("option", { value: o.value }, o.label)))
          : h("input", { name: f.name, type: f.type || "text", required: f.required, autocomplete: "off",
                         minlength: f.minlength, value: f.value });
        inputs[f.name] = input;
        return h("label", {}, f.label, input);
      }),
      h("div", { class: "actions" },
        h("button", { type: "submit", class: danger ? "danger" : "primary", value: "ok" }, confirm),
        h("button", { type: "button", onclick: () => { dlg.close(); } }, "Cancel")));
    form.addEventListener("submit", ev => {
      ev.preventDefault();
      const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
      dlg.close();
      resolve(values);
    });
    dlg.addEventListener("close", () => resolve(null), { once: true });
    put(dlg, form);
    dlg.showModal();
    const first = form.querySelector("input, select, button.primary, button.danger");
    if (first) first.focus();
  });
}

export function showSignedOut() {
  const main = clear(document.getElementById("main"));
  put(main, h("h1", {}, "Signed out"),
    h("p", {}, "Run ", h("code", {}, "cairnward dashboard"), " in a terminal for a new sign-in link."));
}

export function size(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"]; let v = n / 1024; let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${units[i]}`;
}

export function when(ts) { return ts ? ts.replace("T", " ").slice(0, 19) : ""; }

export function settings() {
  try { return JSON.parse(localStorage.getItem("cw-settings") || "{}"); } catch { return {}; }
}
export function saveSettings(s) { localStorage.setItem("cw-settings", JSON.stringify(s)); }
