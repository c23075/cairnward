// Drives the dashboard in Chromium. Usage: node dashboard.mjs <login url>
// Prints one line: RESULT {json}. Steps that fail throw (non-zero exit).
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

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

for (const view of ["activity", "audit", "egress", "jobs", "artifacts", "mcp", "settings"]) {
  out[`h1_${view}`] = await go(view);
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
