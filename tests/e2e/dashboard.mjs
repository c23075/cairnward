// Drives the dashboard in Chromium. Usage: node dashboard.mjs <login url>
// Prints one line: RESULT {json}. Steps that fail throw (non-zero exit).
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const { readFileSync } = await import("node:fs");
const axeSource = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const url = process.argv[2];
const out = { errors: [] };
const browser = await chromium.launch();
const page = await browser.newPage();
page.on("pageerror", e => out.errors.push(`pageerror: ${e.message}`));
page.on("console", m => { if (m.type() === "error") out.errors.push(`console: ${m.text()}`); });

try {
await page.goto(url);
out.landed = new URL(page.url()).pathname + new URL(page.url()).search;
await page.waitForSelector("#privacy-badge:not(:empty)");
out.badge = await page.textContent("#privacy-badge");

// the skip link is the first thing Tab reaches
await page.keyboard.press("Tab");
out.firstTab = await page.evaluate(() => document.activeElement.textContent);

// approvals: one is waiting (the test asked through the management socket)
await page.waitForSelector("article.card");
out.approvalTitle = await page.textContent("article.card h2");
out.provenanceShown = await page.isVisible("article.card .warnbox");
await page.click("article.card button.primary");
await page.waitForSelector("text=Nothing is waiting.");

// vault
await page.click("a[data-view=vault]");
await page.waitForSelector("table tbody tr");
out.rows = await page.locator("table tbody tr").count();
await page.keyboard.press("j");
await page.keyboard.press("k");
await page.keyboard.press("Enter");
await page.waitForSelector(".message");
out.messageText = await page.locator(".message").first().textContent();
out.pwned = await page.evaluate(() => window.pwned === 1);
// lock with L
await page.keyboard.press("L");
await page.waitForSelector("dialog[open]");
await page.fill("dialog input[name=p1]", "browser passphrase 9");
await page.fill("dialog input[name=p2]", "browser passphrase 9");
await page.click("dialog button.primary");
await page.waitForSelector("[aria-label=locked]");
out.locked = true;
// help overlay
await page.keyboard.press("?");
await page.waitForSelector("dialog[open] >> text=Keyboard shortcuts");
await page.keyboard.press("Escape");

async function go(view) {
  await page.click(`a[data-view=${view}]`);
  await page.waitForFunction(v => document.querySelector(`a[data-view=${v}]`)?.getAttribute("aria-current") === "page"
    && document.querySelector("main h1") && !document.querySelector("main").dataset.loading, view);
  return page.textContent("main h1");
}
// an artifact with a script and an external image: the sandboxed preview runs neither
await page.click("a[data-view=artifacts]");
await page.waitForSelector("main h1");
await page.click("main table a");
await page.waitForSelector("main table a >> text=page.html");
await page.click("main table a >> text=page.html");
await page.waitForSelector("iframe.preview");
await page.waitForTimeout(1500);
out.previewSandbox = await page.getAttribute("iframe.preview", "sandbox");
out.pwned2 = await page.evaluate(() => window.pwned2 === 1);
out.previewText = await page.frameLocator("iframe.preview").locator("h1").textContent();

for (const view of ["activity", "audit", "egress", "jobs", "artifacts", "backups", "mcp", "settings"]) {
  out[`h1_${view}`] = await go(view);
}

// change settings in the page: a stricter value saves at once; a weaker one asks first
await go("settings");
await page.selectOption("select[aria-label='network (all environments)']", "off");
await page.waitForSelector("text=Saved, not yet in effect");
await page.selectOption("select[aria-label='approvals (all environments)']", "off");
await page.waitForSelector("dialog[open] >> text=Weaken a protection?");
out.weakenWarning = await page.textContent("dialog[open] .warnbox");
await page.click("dialog[open] button.danger");
await page.waitForFunction(() => document.querySelector("main .warnbox")?.textContent.includes("approvals"));
out.savedBanner = await page.textContent("main .warnbox");

// axe-core on every view, light and dark (evaluate runs outside the page's CSP)
out.axe = {};
for (const scheme of ["light", "dark"]) {
  await page.emulateMedia({ colorScheme: scheme });
  for (const view of ["approvals", "activity", "audit", "egress", "vault", "jobs", "artifacts", "backups", "mcp", "settings"]) {
    await go(view);
    await page.evaluate(() => { for (const d of document.querySelectorAll("main details")) d.open = true; });
    if (!(await page.evaluate(() => typeof window.axe === "object"))) await page.evaluate(axeSource);
    const found = await page.evaluate(async () => {
      const r = await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] } });
      return r.violations.map(v => `${v.id} (${v.impact}): ${v.nodes.slice(0, 3).map(n => n.target.join(" ")).join(" | ")}`);
    });
    if (found.length) out.axe[`${scheme}:${view}`] = found;
  }
}
// accessibility basics on the settings page and the vault page
for (const view of ["vault", "audit", "settings"]) {
  await go(view);
  out[`a11y_${view}`] = await page.evaluate(() => {
    const problems = [];
    for (const el of document.querySelectorAll("input, select")) {
      const named = el.closest("label") || el.getAttribute("aria-label") || el.id && document.querySelector(`label[for="${el.id}"]`);
      if (!named) problems.push(`unlabelled ${el.tagName} ${el.name}`);
    }
    for (const b of document.querySelectorAll("button, a")) {
      if (!(b.textContent.trim() || b.getAttribute("aria-label"))) problems.push(`unnamed ${b.tagName}`);
    }
    if (!document.querySelector("nav a[aria-current=page]")) problems.push("no current page in nav");
    return problems;
  });
}
} catch (e) {
  const main = await page.evaluate(() => document.querySelector("main")?.innerText).catch(() => "?");
  const active = await page.evaluate(() => document.activeElement?.outerHTML?.slice(0, 200)).catch(() => "?");
  console.log("FAILED " + JSON.stringify({ error: String(e).slice(0, 300), main, active, out }));
  await browser.close();
  process.exit(1);
}
await browser.close();
console.log("RESULT " + JSON.stringify(out));
