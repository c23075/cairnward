#!/usr/bin/env bash
# T0.6 sandbox spike: run probe.py under Anthropic's sandbox-runtime (srt) and report.
# Usage: npm ci && ./run_spike.sh   (run as a normal user, not root)
# Optional: SPIKE_ALLOWED_DOMAIN=pypi.org to also test an allowed internet domain.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRT="${SRT:-node $HERE/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js}"
PORT="${SPIKE_GATEWAY_PORT:-18484}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/cw-spike.XXXXXX")"
trap 'kill ${GW_PID:-0} ${SOCK_PID:-0} 2>/dev/null || true; rm -rf "$WORK"' EXIT

# Fake "gateway" on the host's loopback, standing in for Cairnward's model endpoint.
mkdir -p "$WORK/gw" && echo ok > "$WORK/gw/index.html"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/gw" >/dev/null 2>&1 &
GW_PID=$!
# A host Unix socket (stands in for docker.sock / ssh-agent / D-Bus).
python3 -c "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(); time.sleep(600)" "$WORK/host.sock" &
SOCK_PID=$!
# Workspace with a git repo, a fake secret outside it.
mkdir -p "$WORK/workspace" "$WORK/home-secrets/.ssh"
git -C "$WORK/workspace" init -q
echo "PRIVATE KEY" > "$WORK/home-secrets/.ssh/id_test"
sleep 1

ALLOWED='"127.0.0.1:'"$PORT"'"'
if [[ -n "${SPIKE_ALLOWED_DOMAIN:-}" ]]; then ALLOWED="$ALLOWED, \"$SPIKE_ALLOWED_DOMAIN\""; fi
cat > "$WORK/srt-settings.json" <<JSON
{
  "network": { "allowedDomains": [ $ALLOWED ], "deniedDomains": [] },
  "filesystem": {
    "denyRead": ["$WORK/home-secrets"],
    "allowRead": [],
    "allowWrite": ["$WORK/workspace"],
    "denyWrite": []
  }
}
JSON

export SPIKE_GATEWAY_URL="http://127.0.0.1:$PORT/"
export SPIKE_WORKSPACE="$WORK/workspace"
export SPIKE_SECRET_FILE="$WORK/home-secrets/.ssh/id_test"
export SPIKE_OUTSIDE_FILE="$WORK/outside/escaped.txt"
export SPIKE_HOST_SOCKET="$WORK/host.sock"
export SPIKE_ALLOWED_URL="${SPIKE_ALLOWED_DOMAIN:+https://$SPIKE_ALLOWED_DOMAIN/}"

cd "$WORK/workspace"
# srt sets no_proxy=localhost,127.0.0.1 inside the sandbox (its loopback is private on
# Linux). Cairnward's gateway is on the HOST loopback, reachable only via the proxy,
# so the command must clear no_proxy before it starts.
$SRT --settings "$WORK/srt-settings.json" env -u no_proxy -u NO_PROXY python3 "$HERE/probe.py" > "$WORK/result.json"
python3 - "$WORK/result.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
bad = [k for k, v in r.items() if not v["ok"]]
for k, v in r.items():
    mark = "PASS" if v["ok"] else "FAIL"
    print(f"{mark}  {k:32} {'worked' if v['worked'] else 'blocked':8} {v['detail']}")
print(f"\n{len(r) - len(bad)}/{len(r)} checks behaved as required")
sys.exit(1 if bad else 0)
PY
