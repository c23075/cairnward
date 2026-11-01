// Runs a command in sandbox-runtime (srt) like `srt --settings <file> <command...>`, but
// with srt's ask callback connected to Cairnward: when the sandboxed program contacts a
// host that isn't on the allow list, Cairnward's policy decides, asking the user through
// `cairnward approvals` when network = ask (spec §5.3, M2).
//
// Usage: node srt_ask.mjs <srt package dir> <settings.json> <management socket> <run id>
//            -- <command> [args...]
//
// Mirrors srt's own CLI (dist/cli.js, 0.0.81): load the settings, initialize, wrap the
// quoted command, spawn it with a shell, clean up and exit with its status.

import { spawn } from 'node:child_process';
import * as http from 'node:http';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

const [pkgDir, settingsPath, askSocket, runId, sep, ...command] = process.argv.slice(2);
if (!pkgDir || !settingsPath || !askSocket || !runId || sep !== '--' || command.length === 0) {
  console.error('usage: srt_ask.mjs <srt dir> <settings> <socket> <run id> -- <command...>');
  process.exit(2);
}

const lib = (rel) => import(pathToFileURL(path.join(pkgDir, 'dist', rel)).href);
const { SandboxManager } = await lib('index.js');
const { loadConfig } = await lib('utils/config-loader.js');
const { quote } = await lib('utils/shell-quote.js');

function askCairnward({ host, port }) {
  // Any failure is a denial with a reason the program (and the user) can read.
  return new Promise((resolve) => {
    const body = JSON.stringify({ run_id: runId, host, port });
    const req = http.request(
      {
        socketPath: askSocket,
        path: '/v1/approvals/sandbox',
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      },
      (res) => {
        let data = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { data += chunk; });
        res.on('end', () => {
          try {
            const answer = JSON.parse(data);
            if (res.statusCode === 200 && answer.allow === true) {
              resolve(true);
            } else {
              resolve({ allow: false, reason: String(answer.reason ?? 'denied by cairnward') });
            }
          } catch {
            resolve({ allow: false, reason: 'cairnward: unreadable answer' });
          }
        });
      },
    );
    req.on('error', (err) => resolve({ allow: false, reason: `cairnward unreachable: ${err.message}` }));
    req.end(body);
  });
}

const loaded = loadConfig(settingsPath);
if (loaded.kind !== 'ok') {
  console.error(`cairnward: sandbox settings unusable (${loaded.kind}): ${loaded.reason ?? settingsPath}`);
  process.exit(1);
}

let child;
const interrupted = new AbortController();
const interrupt = (signal) => {
  if (child) child.kill(signal);
  else interrupted.abort(new Error(`interrupted by ${signal}`));
};
process.on('SIGINT', () => interrupt('SIGINT'));
process.on('SIGTERM', () => interrupt('SIGTERM'));

try {
  await SandboxManager.initialize(loaded.config, askCairnward);
  const wrapped = await SandboxManager.wrapWithSandbox(quote(command), undefined, undefined, interrupted.signal);
  interrupted.signal.throwIfAborted();
  child = spawn(wrapped, { shell: true, stdio: 'inherit' });
} catch (err) {
  console.error(`cairnward: sandbox failed to start: ${err instanceof Error ? err.message : err}`);
  process.exit(1);
}

child.on('error', (err) => {
  console.error(`cairnward: failed to execute command: ${err.message}`);
  process.exit(1);
});
child.on('exit', (code, signal) => {
  SandboxManager.cleanupAfterCommand();
  if (signal) process.exit(signal === 'SIGINT' || signal === 'SIGTERM' ? 0 : 1);
  process.exit(code ?? 0);
});
