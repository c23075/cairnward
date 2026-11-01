import { api, clear, h, put, setCsrf, toast } from "/static/lib.js";
import * as views from "/static/views.js";

const routes = {
  approvals: views.approvals, activity: views.activity, audit: views.audit, egress: views.egress,
  vault: views.vault, jobs: views.jobs, mcp: views.mcp, settings: views.settingsView,
};
let cleanup = null;
let lastPending = 0;
let first = true;

const ctx = {
  setPending(n) {
    document.getElementById("pending-count").textContent = n ? String(n) : "";
    document.title = n ? `(${n}) Cairnward` : "Cairnward";
    if (n > lastPending && document.hidden) toast(`${n} approval(s) waiting`);
    lastPending = n;
  },
};

async function route() {
  const [name, query] = (location.hash.replace(/^#\//, "") || "approvals").split("?");
  const view = routes[name] || routes.approvals;
  if (cleanup) { cleanup(); cleanup = null; }
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.dataset.view === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const main = clear(document.getElementById("main"));
  main.dataset.loading = "1";
  try {
    cleanup = (await view(main, new URLSearchParams(query || ""), ctx)) || null;
  } catch (e) {
    if (e.status !== 401) put(main, h("p", { class: "bad", role: "alert" }, `Error: ${e.message}`));
  }
  delete main.dataset.loading;
  if (!first) main.focus({ preventScroll: true });  // tell screen readers the page changed
  first = false;
}

async function start() {
  let session;
  try { session = await api("/api/session"); } catch { return; }
  setCsrf(session.csrf);
  const badge = document.getElementById("privacy-badge");
  badge.textContent = `Privacy: ${session.privacy}${session.isolated ? " · core isolated" : ""}`;
  if (session.weakened.length) {
    badge.classList.add("weak");
    badge.title = `Weaker than Strict: ${session.weakened.join(", ")}`;
  }
  ctx.setPending(session.pending);
  document.getElementById("logout").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    location.reload();
  });
  // keep the pending count fresh on every page
  (async () => {
    let version = 0;
    for (;;) {
      try {
        const d = await api(`/api/approvals/wait?version=${version}&timeout=25`);
        version = d.version; ctx.setPending(d.pending.length);
      } catch (e) { if (e.status === 401) return; await new Promise(r => setTimeout(r, 5000)); }
    }
  })();
  window.addEventListener("hashchange", route);
  route();
}

start();
