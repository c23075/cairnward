// citeproc-js runner for Cairnward (reference formatting with CSL styles).
// Reads one JSON object on stdin: {"citeproc": "<path to citeproc_commonjs.js>",
//   "items": [CSL-JSON...], "style": "<CSL XML>", "locale": "<locale XML>"}
// and writes {"entries": ["<html>", ...], "errors": [...]} to stdout.
// Run by Cairnward with Node's permission model: reading only citeproc's folder,
// no child processes, no workers. It makes no network requests.
import { createRequire } from "node:module";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const spec = JSON.parse(input);
const CSL = createRequire(import.meta.url)(spec.citeproc);
const byId = Object.fromEntries(spec.items.map((it) => [String(it.id), it]));
const sys = {
  retrieveLocale: () => spec.locale,
  retrieveItem: (id) => byId[id],
};
const engine = new CSL.Engine(sys, spec.style, "en-US", true);
engine.setOutputFormat("html");
engine.updateItems(Object.keys(byId));
const result = engine.makeBibliography();
if (!result) {
  process.stdout.write(JSON.stringify({ entries: [], errors: ["this style has no bibliography"] }));
} else {
  const [meta, entries] = result;
  process.stdout.write(JSON.stringify({ entries, errors: meta.bibliography_errors || [] }));
}
