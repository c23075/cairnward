import { api, ask, clear, h, put, saveSettings, settings, size, table, toast, when } from "/static/lib.js";

// Each view: async (main, params, ctx) => optional cleanup function.

// ------------------------------------------------------------------ approvals

function approvalCard(item, refresh) {
  const where = [item.environment && `environment ${item.environment}`, item.client && `client ${item.client}`,
    item.run_id && `run ${item.run_id.slice(0, 8)}`, `started by ${item.initiated_by}`].filter(Boolean).join(", ");
  const answer = async (granted, scope = "once", similar = false) => {
    let secret;
    if (granted && item.needs_secret) {
      const v = await ask("Choose a passphrase", {
        text: "Typed here only: it never goes to the model or the chat, and it can't be recovered if forgotten.",
        fields: [{ name: "p1", label: "Passphrase (at least 8 characters)", type: "password", required: true, minlength: 8 },
                 { name: "p2", label: "Again", type: "password", required: true }],
        confirm: "Approve and lock" });
      if (!v) return;
      if (v.p1 !== v.p2 || v.p1.length < 8) { toast("The passphrases differ or are too short."); return; }
      secret = v.p1;
    }
    try {
      const r = await api(`/api/approvals/${item.id}`, { method: "POST", body: { granted, scope, similar, secret } });
      toast(`${granted ? "Allowed" : "Denied"}${r.answered > 1 ? ` (${r.answered} requests)` : ""}.`);
    } catch (e) { toast(`Couldn't answer: ${e.message}`); }
    refresh();
  };
  const yes = item.run_id && item.kind === "net.connect" ? "Yes, for this run" : "Yes, once";
  return h("article", { class: "card", "aria-label": `${item.kind} ${item.target}` },
    h("h2", {}, `${item.kind}: `, h("span", { class: "mono" }, item.target)),
    item.gave_up ? h("p", { class: "muted" }, "The client stopped waiting; yes applies to its retry.") : null,
    item.provenance && item.provenance.length ? h("div", { class: "warnbox", role: "alert" },
      h("strong", {}, "Untrusted content entered the conversation since your last message. "),
      "Make sure this request is what you wanted:",
      h("ul", {}, item.provenance.map(p => h("li", {}, p)))) : null,
    h("dl", {}, h("dt", {}, "Why"), h("dd", {}, item.reason), h("dt", {}, "Risk"), h("dd", {}, item.risk),
      h("dt", {}, "Where"), h("dd", {}, where), h("dt", {}, "Waiting"), h("dd", {}, `${Math.round(item.age_s)} s`)),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "primary", onclick: () => answer(true) }, yes),
      item.rememberable && item.conversation_id ? h("button", { type: "button", onclick: () => answer(true, "conversation") }, "Yes, this conversation") : null,
      item.rememberable ? h("button", { type: "button", onclick: () => answer(true, "environment") }, "Yes, this environment") : null,
      item.similar ? h("button", { type: "button", onclick: () => answer(true, "once", true) }, `Yes to all ${item.similar + 1} like it`) : null,
      h("button", { type: "button", class: "danger", onclick: () => answer(false) }, "No")));
}

export async function approvals(main, _params, ctx) {
  let stop = false; let version = 0;
  const list = h("div", {});
  put(main, h("h1", {}, "Approvals"), h("p", { class: "muted" },
    "Requests waiting for your answer. Unanswered requests are refused when they time out."), list);
  const render = data => {
    clear(list);
    ctx.setPending(data.pending.length);
    if (!data.pending.length) put(list, h("p", {}, "Nothing is waiting."));
    data.pending.forEach(item => put(list, approvalCard(item, refresh)));
  };
  const refresh = async () => render(await api("/api/approvals"));
  await refresh();
  (async () => {
    while (!stop) {
      try {
        const data = await api(`/api/approvals/wait?version=${version}&timeout=25`);
        if (stop) break;
        if (data.version !== version) { version = data.version; render(data); }
      } catch { await new Promise(r => setTimeout(r, 3000)); }
    }
  })();
  return () => { stop = true; };
}

// ------------------------------------------------------------------ activity and audit

const auditColumns = [
  { label: "Seq", key: "seq", num: true },
  { label: "Time", render: r => when(r.ts) },
  { label: "Actor", key: "actor" },
  { label: "Action", key: "action" },
  { label: "Decision", render: r => [r.phase !== "event" ? r.phase : "", r.decision || "", r.outcome || ""].filter(Boolean).join(" ") },
  { label: "Target", key: "target", mono: true },
  { label: "Environment", key: "environment" },
];

export async function activity(main) {
  let last = 0; let rows = []; let stop = false;
  const holder = h("div", {});
  put(main, h("h1", {}, "Activity"), h("p", { class: "muted" }, "The newest audit records, updated every few seconds."), holder);
  const tick = async () => {
    try {
      const fresh = await api(`/api/activity?after=${last}&limit=200`);
      if (fresh.length) {
        last = fresh[fresh.length - 1].seq;
        rows = [...fresh.reverse(), ...rows].slice(0, 200);
        put(clear(holder), table(auditColumns, rows));
      } else if (!rows.length) put(clear(holder), h("p", {}, "No activity yet."));
    } catch { /* shown by api() */ }
    if (!stop) setTimeout(tick, 3000);
  };
  tick();
  return () => { stop = true; };
}

function filterForm(fields, params, onSubmit) {
  const inputs = {};
  const form = h("form", { class: "filters", role: "search" },
    fields.map(([name, label, placeholder]) => {
      inputs[name] = h("input", { name, value: params.get(name) || "", placeholder });
      return h("label", {}, label, inputs[name]);
    }),
    h("button", { type: "submit", class: "primary" }, "Show"));
  form.addEventListener("submit", ev => {
    ev.preventDefault();
    const q = new URLSearchParams();
    for (const [k, el] of Object.entries(inputs)) if (el.value.trim()) q.set(k, el.value.trim());
    onSubmit(q);
  });
  return form;
}

export async function audit(main, params) {
  const holder = h("div", {});
  const status = h("p", { role: "status" });
  const go = q => { location.hash = `#/audit?${q}`; };
  put(main, h("h1", {}, "Audit log"),
    filterForm([["action", "Action", "model.call"], ["client", "Client", ""], ["run", "Run", "id prefix"],
      ["env", "Environment", ""], ["conversation", "Conversation", "id prefix"], ["since", "Since", "24h, 7d"]], params, go),
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: async () => {
        status.textContent = "Checking…";
        const r = await api("/api/audit/verify");
        status.textContent = r.ok ? `Chain and mirror intact: ${r.records} records, head ${r.head.slice(0, 24)}…`
          : `FAILED: ${r.problems.join("; ")}`;
        status.className = r.ok ? "ok" : "bad";
      } }, "Verify the chain"),
      h("a", { href: "/api/audit/export", download: "" }, "Export (JSON lines)")),
    status, holder);
  const rows = await api(`/api/audit?${params}`);
  put(holder, rows.length ? table(auditColumns, rows.reverse()) : h("p", {}, "No records match."));
}

export async function egress(main, params) {
  const go = q => { location.hash = `#/egress?${q}`; };
  put(main, h("h1", {}, "Egress report"),
    h("p", { class: "muted" }, "What left this machine through Cairnward, per destination."),
    filterForm([["run", "Run", "id prefix"], ["client", "Client", ""], ["env", "Environment", ""],
      ["conversation", "Conversation", "id prefix"], ["since", "Since", "24h, 7d"]], params, go),
    h("p", {}, h("a", { href: `/api/egress?${params}&format=csv`, download: "" }, "Export CSV")));
  const rows = await api(`/api/egress?${params}`);
  if (!rows.length) { put(main, h("p", {}, "Nothing left this machine through Cairnward in this selection.")); return; }
  put(main, table([
    { label: "Destination", key: "destination", mono: true },
    { label: "Kind", key: "kind" },
    { label: "Allowed", key: "allowed", num: true },
    { label: "Denied", num: true, render: r => r.denied ? h("span", { class: "bad", title: Object.keys(r.deny_reasons).join("; ") }, r.denied) : "0" },
    { label: "Sent", num: true, render: r => r.kind === "network" ? size(r.bytes_up) : "–" },
    { label: "Received", num: true, render: r => r.kind === "network" ? size(r.bytes_down) : "–" },
    { label: "Tokens", num: true, render: r => r.kind === "model" ? r.prompt_tokens + r.completion_tokens : "–" },
    { label: "Flagged", render: r => Object.entries(r.flagged).map(([k, n]) => `${k} ×${n}`).join(", ") },
    { label: "Last", render: r => when(r.last) },
  ], rows));
}

// ------------------------------------------------------------------ vault

function messageView(m) {
  const body = m.body || {};
  let text = "";
  if (typeof body.content === "string") text = body.content;
  else if (Array.isArray(body.content)) text = body.content.map(p => p.text || `[${p.type}]`).join("\n");
  if (body.tool_calls) text += (text ? "\n" : "") + body.tool_calls.map(c => `→ ${c.function?.name}(${c.function?.arguments || ""})`).join("\n");
  return h("div", { class: `message ${m.role}` }, h("span", { class: "role" }, m.role + (m.model ? ` · ${m.model}` : "")), text);
}

export async function vault(main, params) {
  const prefs = settings();
  let rows = []; let index = 0; let current = null;
  const listBox = h("div", { "aria-label": "Conversations" });
  const detail = h("section", { "aria-live": "polite", "aria-label": "Conversation" });
  const search = h("input", { type: "search", name: "q", value: params.get("q") || "", "aria-label": "Search conversations", placeholder: "Search (/)" });
  const trash = params.get("trash") === "1";
  const help = () => ask("Keyboard shortcuts", { confirm: "Close", text: [
    "j / k: next / previous conversation · Enter: open · /: search",
    "d: delete (with undo) · f: move to folder · L: lock · p: pin",
    "x: egress report · l: audit records · ?: this help",
    prefs.noKeys ? "Single-key shortcuts are OFF (Settings)." : "Turn single-key shortcuts off in Settings." ] });

  const backupNote = h("p", { class: "muted", role: "status" });
  api("/api/backup").then(b => {
    if (b.pending_purges) {
      backupNote.textContent = `${b.pending_purges} purged conversation(s) pending removal from backups (done on the next backup run).`;
    }
  }).catch(() => {});
  const reload = async () => {
    const q = search.value.trim();
    rows = await api(`/api/vault?${new URLSearchParams({ ...(q ? { q } : {}), ...(trash ? { trash: "true" } : {}) })}`);
    index = Math.min(index, Math.max(rows.length - 1, 0));
    drawList();
  };
  const drawList = () => {
    clear(listBox);
    if (!rows.length) { put(listBox, h("p", {}, trash ? "The trash is empty." : "No conversations.")); return; }
    put(listBox, table([
      { label: "", srLabel: "Status", render: r => [r.pinned ? h("span", { title: "pinned", "aria-label": "pinned" }, "📌") : null,
                                 r.locked ? h("span", { title: "locked", "aria-label": "locked" }, "🔒") : null] },
      { label: "Title", render: r => r.locked && !r.title ? "(locked)" : (r.title || "(untitled)") },
      { label: "Environment", key: "environment" },
      { label: "Updated", render: r => when(r.updated_at) },
    ], rows, { selected: index, onRow: (_r, i) => { index = i; drawList(); open(); } }));
    if (rows[index]?.snippet) put(listBox, h("p", { class: "muted" }, `…${rows[index].snippet}…`));
  };
  const open = async () => {
    const r = rows[index]; if (!r) return;
    clear(detail);
    try {
      current = await api(`/api/vault/${r.id}`);
    } catch (e) {
      if (e.status === 423) {
        put(detail, h("p", {}, "This conversation is locked."),
          h("button", { type: "button", onclick: () => unlock(r) }, "Unlock…"));
        return;
      }
      throw e;
    }
    const c = current.conversation;
    put(detail, h("h2", {}, c.title || "(untitled)"),
      h("p", { class: "muted" }, `${c.environment || "-"} · ${c.client || "-"} · ${when(c.created_at)} · `, h("span", { class: "mono" }, c.id.slice(0, 10))),
      h("div", { class: "actions" },
        trash ? h("button", { type: "button", onclick: () => op(r, "restore") }, "Restore") : h("button", { type: "button", onclick: () => remove(r) }, "Delete (d)"),
        h("button", { type: "button", onclick: () => op(r, "pin", { pinned: !r.pinned }) }, r.pinned ? "Unpin" : "Pin (p)"),
        h("button", { type: "button", onclick: () => lock(r) }, "Lock… (L)"),
        h("button", { type: "button", onclick: () => move(r) }, "Move… (f)"),
        h("a", { href: `/api/vault/${r.id}/export?format=md`, download: "" }, "Export"),
        trash ? h("button", { type: "button", class: "danger", onclick: () => purge(r) }, "Destroy now") : null),
      h("p", { class: "muted" }, "Deleting here doesn't delete the copy in the client's own history."),
      current.messages.map(messageView));
  };
  const op = async (r, name, body) => {
    try { await api(`/api/vault/${r.id}/${name}`, { method: "POST", body }); } catch (e) { toast(e.message); }
    await reload(); open();
  };
  const remove = async r => {
    await api(`/api/vault/${r.id}/delete`, { method: "POST" });
    toast("Moved to the trash.", { action: "Undo", onAction: async () => { await api(`/api/vault/${r.id}/restore`, { method: "POST" }); reload(); } });
    clear(detail); await reload();
  };
  const purge = async r => {
    const ok = await ask("Destroy this conversation?", { text: "Its key is destroyed first; this can't be undone.", confirm: "Destroy", danger: true });
    if (ok) { await op(r, "purge"); clear(detail); }
  };
  const lock = async r => {
    const v = await ask("Lock this conversation", {
      text: ["A locked conversation needs the passphrase to read and is never sent to hosted models.",
             "There is no way to recover it if you forget the passphrase."],
      fields: [{ name: "p1", label: "Passphrase (at least 8 characters)", type: "password", required: true, minlength: 8 },
               { name: "p2", label: "Again", type: "password", required: true }], confirm: "Lock" });
    if (!v) return;
    if (v.p1 !== v.p2) { toast("The passphrases differ."); return; }
    await op(r, "lock", { passphrase: v.p1 });
    toast("Locked.");
  };
  const unlock = async r => {
    const v = await ask("Unlock", { fields: [{ name: "p", label: "Passphrase", type: "password", required: true },
      { name: "m", label: "For how long", options: [{ value: "15", label: "15 minutes" }, { value: "30", label: "30 minutes" }, { value: "120", label: "2 hours" }] }], confirm: "Unlock" });
    if (!v) return;
    try { await api(`/api/vault/${r.id}/unlock`, { method: "POST", body: { passphrase: v.p, minutes: Number(v.m) } }); open(); }
    catch (e) { toast(e.message); }
  };
  const move = async r => {
    const folders = await api("/api/vault/folders");
    const v = await ask("Move to folder", { fields: [{ name: "f", label: "Folder", options: [{ value: "", label: "(no folder)" }, ...folders.map(f => ({ value: f.id, label: f.name })), { value: "__new", label: "New folder…" }] }], confirm: "Move" });
    if (!v) return;
    let id = v.f || null;
    if (id === "__new") {
      const n = await ask("New folder", { fields: [{ name: "name", label: "Name", required: true }] });
      if (!n) return;
      id = (await api("/api/vault/folders", { method: "POST", body: { name: n.name } })).id;
    }
    await op(r, "move", { folder_id: id });
  };

  const keys = ev => {
    if (prefs.noKeys || ev.target.closest("input, select, textarea, dialog") || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const r = rows[index];
    const k = ev.key;
    const actions = {
      j: () => { index = Math.min(index + 1, rows.length - 1); drawList(); },
      k: () => { index = Math.max(index - 1, 0); drawList(); },
      Enter: open, "/": () => search.focus(), "?": help,
      d: () => r && remove(r), f: () => r && move(r), L: () => r && lock(r), p: () => r && op(r, "pin", { pinned: !r.pinned }),
      x: () => r && (location.hash = `#/egress?conversation=${r.id}`),
      l: () => r && (location.hash = `#/audit?conversation=${r.id}`),
    };
    if (actions[k]) { ev.preventDefault(); actions[k](); }
  };
  document.addEventListener("keydown", keys);
  search.addEventListener("keydown", ev => { if (ev.key === "Enter") reload(); if (ev.key === "Escape") search.blur(); });
  put(main, h("h1", {}, trash ? "Vault: trash" : "Vault"), backupNote,
    h("div", { class: "actions" }, search, h("button", { type: "button", onclick: reload }, "Search"),
      h("a", { href: trash ? "#/vault" : "#/vault?trash=1" }, trash ? "Back to conversations" : "Trash"),
      h("button", { type: "button", onclick: help }, "Shortcuts (?)")),
    h("div", { class: "split" }, listBox, detail));
  await reload();
  return () => document.removeEventListener("keydown", keys);
}

// ------------------------------------------------------------------ research jobs

export async function jobs(main, params) {
  const id = params.get("id");
  if (id) {
    const j = await api(`/api/jobs/${id}`);
    put(main, h("h1", {}, `Research job ${j.id} (${j.kind})`),
      h("p", {}, `${j.status} · ${j.done}/${j.total} steps · library ${j.library}`,
        j.eta_s ? ` · about ${Math.round(j.eta_s / 60)} min left` : ""),
      h("p", {}, h("a", { href: "#/jobs" }, "All jobs")),
      ["queued", "running"].includes(j.status) ? h("button", { type: "button", class: "danger", onclick: async () => {
        await api(`/api/jobs/${j.id}/cancel`, { method: "POST" }); location.reload(); } }, "Cancel") : null,
      table([
        { label: "Step", key: "name" }, { label: "", srLabel: "Details", render: r => r.label || "" },
        { label: "Status", render: r => h("span", { class: r.status === "failed" ? "bad" : r.status === "done" ? "ok" : null }, r.status) },
        { label: "Time", num: true, render: r => r.seconds ? `${Math.round(r.seconds)} s` : "" },
        { label: "Error", render: r => r.error || "" },
      ], j.steps),
      j.result ? [h("h2", {}, "Result (Markdown)"), h("pre", { class: "message" }, j.result)] : null);
    return;
  }
  put(main, h("h1", {}, "Research jobs"),
    h("p", { class: "muted" }, "Start one with ", h("code", {}, "cairnward research review <library> --question ..."), " or from a chat client (lit_review_start)."));
  const rows = await api("/api/jobs");
  put(main, rows.length ? table([
    { label: "Job", render: r => h("a", { href: `#/jobs?id=${r.id}` }, r.id) },
    { label: "Kind", key: "kind" }, { label: "Library", key: "library" },
    { label: "Status", key: "status" }, { label: "Progress", render: r => `${r.done}/${r.total}` },
    { label: "Created", render: r => when(r.created) },
  ], rows) : h("p", {}, "No research jobs yet."));
}

// ------------------------------------------------------------------ MCP

export async function mcp(main) {
  put(main, h("h1", {}, "MCP tools"));
  const servers = await api("/api/mcp/servers");
  if (!servers.length) { put(main, h("p", {}, "No MCP servers configured (mcp.toml).")); return; }
  for (const s of servers) {
    const box = h("div", {});
    put(main, h("article", { class: "card" },
      h("h2", {}, s.name, s.version ? h("span", { class: "muted" }, ` ${s.version}`) : null),
      s.description ? h("p", {}, s.description) : null,
      h("dl", {}, h("dt", {}, "Command"), h("dd", { class: "mono" }, s.command.join(" ")),
        h("dt", {}, "Enabled in"), h("dd", {}, s.environments.join(", ") || "nowhere"),
        h("dt", {}, "Network"), h("dd", {}, s.permissions.network.join(", ") || "none"),
        h("dt", {}, "Writes"), h("dd", {}, s.permissions.write.join(", ") || "its own folder only"),
        h("dt", {}, "Reads"), h("dd", {}, s.permissions.read ? s.permissions.read.join(", ") : "everything except secrets")),
      s.environments.length ? h("button", { type: "button", onclick: () => tools(s, box) }, "Review tools") : null, box));
  }
}

async function tools(s, box) {
  put(clear(box), h("p", { role: "status" }, "Starting the server…"));
  let data;
  try { data = await api(`/api/mcp/${s.name}/tools`); } catch (e) { put(clear(box), h("p", { class: "bad" }, e.message)); return; }
  const picks = {};
  put(clear(box), ...data.tools.map(t => {
    const box2 = h("input", { type: "checkbox", checked: t.status !== "approved" && !t.flags.length, disabled: t.status === "approved" });
    picks[t.name] = { el: box2, hash: t.hash, status: t.status };
    return h("div", { class: "card" },
      h("label", {}, h("span", {}, box2, ` ${t.name} `, h("strong", { class: t.status === "approved" ? "ok" : "bad" }, t.status))),
      h("p", { class: "message" }, t.description || "(no description)"),
      t.flags.length ? h("div", { class: "warnbox", role: "alert" }, "Warnings: ", t.flags.join(", "), ". Don't approve unless you understand why.") : null,
      t.offered ? null : h("p", { class: "muted" }, t.why));
  }), h("button", { type: "button", class: "primary", onclick: async () => {
    const chosen = Object.fromEntries(Object.entries(picks).filter(([, p]) => p.el.checked && p.status !== "approved").map(([n, p]) => [n, p.hash]));
    if (!Object.keys(chosen).length) { toast("Nothing selected."); return; }
    try { await api(`/api/mcp/${s.name}/approve`, { method: "POST", body: { environment: data.environment, tools: chosen } }); toast("Approved."); tools(s, box); }
    catch (e) { toast(e.message); }
  } }, "Approve selected definitions"));
}

// ------------------------------------------------------------------ settings

export async function settingsView(main) {
  const s = await api("/api/settings");
  const prefs = settings();
  const keysBox = h("input", { type: "checkbox", checked: !prefs.noKeys, onchange: ev => { prefs.noKeys = !ev.target.checked; saveSettings(prefs); toast("Saved."); } });
  const reload = () => settingsView(clear(main));

  // one setting: a select that writes the file (weakening asks first)
  const change = async (scope, setting, value, select, previous) => {
    const send = confirm => api("/api/settings/privacy", { method: "POST", body: { scope, setting, value: value || null, confirm } });
    try {
      let r;
      try { r = await send(false); } catch (e) {
        if (e.status !== 409) throw e;
        const ok = await ask("Weaken a protection?", { warn: e.message.replace(/^needs confirmation: /, ""),
          text: "This is recorded in the audit log and takes effect when Cairnward restarts.", confirm: "Save anyway", danger: true });
        if (!ok) { select.value = previous; return; }
        r = await send(true);
      }
      toast(r.changes.length ? "Saved. Restart Cairnward (cairnward serve) to apply it." : "Saved (no effective change).");
      reload();
    } catch (e) { select.value = previous; toast(`Not saved: ${e.message}`); }
  };
  const picker = (scope, setting, current, inherits, explicit = true) => {
    const opts = s.options[setting] || [];
    const sel = h("select", { "aria-label": `${setting} (${scope === "global" ? "all environments" : scope})` },
      inherits ? h("option", { value: "" }, scope === "global" ? "from the preset" : "as globally / preset") : null,
      opts.map(o => h("option", { value: o, selected: o === current }, o)));
    if (inherits && (!explicit || !opts.includes(current))) sel.value = "";
    const previous = sel.value;
    sel.addEventListener("change", () => change(scope, setting, sel.value, sel, previous));
    return sel;
  };
  const privacyTable = (scope, p) => table([
    { label: "Setting", render: r => r[0] },
    { label: "Value", render: r => h("span", { class: r[1].weaker_than_strict ? "bad" : null }, r[1].value) },
    { label: "From", render: r => r[1].source },
    { label: "Change", render: r => picker(scope, r[0], r[1].value, true,
      r[1].source === (scope === "global" ? "global setting" : `environment '${scope}'`)) },
  ], Object.entries(p.settings));
  const presetOf = p => (p.label || "").split(" ")[0].toLowerCase();
  const saved = s.saved_since_start.length ? h("div", { class: "warnbox", role: "status" },
    h("strong", {}, "Saved, not yet in effect: "), "restart Cairnward to apply ",
    s.saved_since_start.map(c => `${c.setting} ${c.before} → ${c.after} (${c.scope})`).join("; "), ".") : null;
  const rk = s.recovery_key;
  put(main, h("h1", {}, "Settings"),
    h("p", { class: "muted" }, "Privacy settings can be changed here: each change is written to the configuration file, recorded in the audit log, and takes effect when Cairnward restarts. Anything weaker than Strict asks first and stays highlighted. Other settings: edit the files in your configuration folder."),
    saved,
    h("h2", {}, "This browser"), h("label", {}, keysBox, " Single-key shortcuts in the vault"),
    h("h2", {}, `Privacy: ${s.global.label}`),
    h("label", {}, "Preset for all environments ", picker("global", "preset", presetOf(s.global), false)),
    s.global.warnings.map(w => h("div", { class: "warnbox" }, w)), privacyTable("global", s.global),
    h("h2", {}, "Environments"),
    Object.entries(s.environments).map(([name, e]) => h("details", {}, h("summary", {}, `${name}: ${e.privacy.label}`),
      e.description ? h("p", {}, e.description) : null,
      h("p", { class: "muted" }, `Workspace: ${e.workspace.join(", ") || "-"} · MCP servers: ${e.mcp_servers.join(", ") || "-"} · retention: ${e.retention_days ? e.retention_days + " days" : "forever"}`),
      e.privacy.warnings.map(w => h("div", { class: "warnbox" }, w)), privacyTable(name, e.privacy))),
    h("h2", {}, "Vault recovery key"),
    h("p", {}, !rk ? "Not available in this process." : rk.set_up
      ? `Set up ${rk.created_at.slice(0, 10)}: it can open ${rk.covered} of ${rk.locked} locked conversation(s)${rk.covered < rk.locked ? " (the others were locked before it existed; unlocking each once adds them)" : ""}.`
      : "None: a forgotten passphrase can't be recovered. Create one in a terminal with `cairnward vault recovery-key create` (it's shown once, there)."),
    h("h2", {}, "Backends"),
    table([{ label: "Name", render: r => r[0] }, { label: "Driver", render: r => r[1].driver }, { label: "Location", render: r => r[1].location }, { label: "Host", render: r => r[1].host, mono: true }], Object.entries(s.backends)),
    h("h2", {}, "Roles"), table([{ label: "Role", render: r => r[0] }, { label: "Model", render: r => r[1] }], Object.entries(s.roles)),
    h("h2", {}, "Clients"),
    Object.keys(s.clients).length ? table([{ label: "Name", render: r => r[0] }, { label: "Kind", render: r => r[1].kind }, { label: "Default environment", render: r => r[1].default_environment || "-" }], Object.entries(s.clients)) : h("p", {}, "None in clients.toml (keys made with `cairnward client add` work without an entry)."),
    h("h2", {}, "Audit anchoring"), h("p", {}, s.anchor_repo || "not set up"));
}

// ------------------------------------------------------------------ backups

export async function backups(main) {
  const b = await api("/api/backup");
  const stillShown = () => main.isConnected && location.hash.startsWith("#/backups");
  if (b.running) setTimeout(() => { if (stillShown()) backups(main); }, 3000);
  const backUpNow = async () => {
    try { await api("/api/backup/run", { method: "POST" }); toast("Backup started."); backups(main); }
    catch (e) { toast(e.message); }
  };
  const changeSettings = async () => {
    const modes = [{ value: "none", label: "Off" }, { value: "on_demand", label: "Only when asked" }, { value: "auto", label: "Automatic" }];
    const v = await ask("Backup settings", {
      text: "Backups hold your chats, artifacts, research library, audit log and settings, encrypted. Leave the folder empty for ~/cairnward-backups, or pick one on another disk if you can, because a backup on the same disk won't survive that disk failing. Changes take effect when Cairnward restarts.",
      fields: [{ name: "mode", label: "Backups", options: [...modes.filter(m => m.value === b.mode), ...modes.filter(m => m.value !== b.mode)] },
               ...(b.remote ? [] : [{ name: "repository", label: "Folder (full path)", value: b.where || "" },
                 { name: "access", label: "Who can read the folder", options: b.access === "group" ? [{ value: "group", label: "Me and my group" }, { value: "owner", label: "Only me" }] : [{ value: "owner", label: "Only me" }, { value: "group", label: "Me and my group" }] }])],
      confirm: "Save" });
    if (!v) return;
    try {
      const r = await api("/api/backup/settings", { method: "POST", body: { mode: v.mode, repository: v.repository ?? null, access: v.access ?? null } });
      toast(r.restart_needed ? "Saved. Restart Cairnward for it to take effect." : "Nothing changed.");
      backups(main);
    } catch (e) { toast(e.message); }
  };
  const dl = (...pairs) => h("dl", {}, pairs.filter(p => p[1] !== null && p[1] !== undefined)
    .flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  const last = b.last;
  put(main, h("h1", {}, "Backups"),
    h("p", { class: "muted" }, "restic backups of the vault, research library, artifacts and settings, always encrypted. Run or restore them in a terminal: `cairnward backup run`, `cairnward backup restore --at DATE`."),
    b.mode === "none"
      ? h("div", { class: "warnbox", role: "status" }, "Backups are off. Use Change settings, or run `cairnward backup use`.")
      : null,
    b.same_disk_warning ? h("div", { class: "warnbox" }, b.same_disk_warning) : null,
    b.cloud_warning ? h("div", { class: "warnbox" }, b.cloud_warning) : null,
    dl(["Mode", b.mode === "auto" ? `automatic, every ${b.interval_hours} h while Cairnward runs` : b.mode === "on_demand" ? "on demand" : "off"],
      ["Where", b.where || "not set"],
      ["Network", b.remote ? `remote (${b.remote}): through egress in the ${b.environment} environment` : (b.repository ? "a folder (no network)" : null)],
      ["Keeps", `${b.keep.daily} daily, ${b.keep.weekly} weekly, ${b.keep.monthly} monthly`],
      ["Includes", ["conversations", "audit log", ...b.includes].join(", ")],
      ["Last backup", last ? `${when(last.time)}${last.snapshot ? ` (snapshot ${last.snapshot})` : ""}` : "never"]),
    b.settings_saved ? h("div", { class: "warnbox", role: "status" }, "Settings saved. Restart Cairnward for them to take effect.") : null,
    b.run_error ? h("div", { class: "warnbox", role: "alert" }, `The last backup failed: ${b.run_error}`) : null,
    h("p", { role: "status" }, b.running ? "Backing up. This can take a few minutes." : null),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "primary", disabled: b.running || b.mode === "none", onclick: backUpNow }, "Back up now"),
      h("button", { type: "button", onclick: changeSettings }, "Change settings")),
    h("h2", {}, "Purged conversations still in older backups"),
    b.pending.length
      ? table([{ label: "Conversation", key: "conversation", mono: true }, { label: "Purged", render: r => when(r.since) },
               { label: "Tries", key: "tries", num: true }, { label: "Last error", render: r => r.error || "-" }], b.pending)
      : h("p", {}, "None: every purge has been removed from every backup."));
}

// ------------------------------------------------------------------ artifacts

export async function artifacts(main, params) {
  const scope = params.get("scope"), owner = params.get("owner");
  if (!scope) {
    put(main, h("h1", {}, "Artifacts"),
      h("p", { class: "muted" }, "Versioned files from conversations (MCP tool artifact_write), research jobs and runs. Each write is a version; restoring makes a new one."));
    const repos = await api("/api/artifacts");
    put(main, repos.length ? table([
      { label: "Kind", key: "scope" },
      { label: "Owner", render: r => h("a", { href: `#/artifacts?scope=${encodeURIComponent(r.scope)}&owner=${encodeURIComponent(r.owner)}` }, r.owner) },
      { label: "Files", render: r => r.locked ? "locked" : String(r.files), num: true },
    ], repos) : h("p", {}, "No artifacts yet."));
    return;
  }
  const data = await api(`/api/artifacts/${encodeURIComponent(scope)}/${encodeURIComponent(owner)}`);
  const view = h("section", { "aria-live": "polite", "aria-label": "File" });
  const base = `/api/artifacts/${encodeURIComponent(scope)}/${encodeURIComponent(owner)}`;
  const open = async (path, rev) => {
    clear(view);
    const q = new URLSearchParams({ path, ...(rev ? { rev } : {}) });
    const history = await api(`${base}/history?path=${encodeURIComponent(path)}`);
    put(view, h("h2", {}, path, rev ? h("span", { class: "muted" }, ` (version ${rev.slice(0, 10)})`) : null));
    if (/\.(html?|svg)$/i.test(path)) {
      // sandboxed: no scripts, no network, opaque origin (FR-ART-4)
      put(view, h("iframe", { src: `/api/preview/${encodeURIComponent(scope)}/${encodeURIComponent(owner)}?${q}`,
        sandbox: "", title: `Preview of ${path}`, class: "preview" }));
    } else {
      put(view, h("pre", { class: "message" }, await api(`${base}/file?${q}`)));
    }
    put(view, h("h2", {}, "Versions"), table([
      { label: "Version", render: r => h("a", { href: "#", onclick: ev => { ev.preventDefault(); open(path, r.rev); } }, r.rev.slice(0, 10)) },
      { label: "When", render: r => when(r.date) }, { label: "What", key: "message" },
      { label: "", srLabel: "Restore", render: r => r.rev === history[0]?.rev ? "current" : h("button", { type: "button", onclick: async () => {
        await api(`${base}/restore`, { method: "POST", body: { path, rev: r.rev } }); toast("Restored as a new version."); open(path);
      } }, "Restore") },
    ], history));
  };
  put(main, h("h1", {}, `Artifacts: ${scope} ${owner}`), h("p", {}, h("a", { href: "#/artifacts" }, "All artifacts")),
    h("div", { class: "split" },
      table([{ label: "File", render: r => h("a", { href: "#", onclick: ev => { ev.preventDefault(); open(r); } }, r) }], data.files.map(f => f)),
      view));
}
