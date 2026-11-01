#!/usr/bin/env bash
# T0.6 spike, part 2: srt forwards to an upstream proxy (stand-in for cairnward-egress),
# which logs and makes the final decision.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRT="${SRT:-node $HERE/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js}"
GW_PORT=18485; EG_PORT=18486
WORK="$(mktemp -d "${TMPDIR:-/tmp}/cw-chain.XXXXXX")"
trap 'kill ${GW_PID:-0} ${EG_PID:-0} 2>/dev/null || true; rm -rf "$WORK"' EXIT
mkdir -p "$WORK/gw" && echo ok > "$WORK/gw/index.html"
python3 -m http.server "$GW_PORT" --bind 127.0.0.1 --directory "$WORK/gw" >/dev/null 2>&1 & GW_PID=$!
python3 "$HERE/fake_egress.py" "$EG_PORT" "$WORK/egress.log" "127.0.0.1:$GW_PORT" & EG_PID=$!
sleep 1
cat > "$WORK/s.json" <<JSON
{"network":{"allowedDomains":["127.0.0.1:$GW_PORT", ${SRT_ALLOW_EXTRA:-"\"allowed-by-srt.example\""}],"deniedDomains":[]},
 "filesystem":{"denyRead":[],"allowRead":[],"allowWrite":["$WORK"],"denyWrite":[]}}
JSON
cat > "$WORK/probe.py" <<PY
import urllib.request as u
for url in ["http://127.0.0.1:$GW_PORT/", "https://allowed-by-srt.example/", "https://not-allowed.example/"]:
    try:
        print("worked ", url, u.urlopen(url, timeout=10).status)
    except Exception as e:
        print("blocked", url, str(e)[:90])
PY
cd "$WORK"
# srt treats HTTP(S)_PROXY in ITS OWN environment as its parent proxy.
HTTP_PROXY="http://127.0.0.1:$EG_PORT" HTTPS_PROXY="http://127.0.0.1:$EG_PORT" \
  $SRT --settings "$WORK/s.json" env -u no_proxy -u NO_PROXY python3 "$WORK/probe.py" | tee "$WORK/out.txt"
echo "--- cairnward-egress log ---"; cat "$WORK/egress.log"
fail=0
grep -q "^worked  http://127.0.0.1:$GW_PORT/" "$WORK/out.txt" || { echo "FAIL gateway not reachable"; fail=1; }
grep -q "^DENY CONNECT allowed-by-srt.example:443" "$WORK/egress.log" || { echo "FAIL srt did not forward to egress"; fail=1; }
grep -q "^blocked https://allowed-by-srt.example/" "$WORK/out.txt" || { echo "FAIL egress decision not enforced"; fail=1; }
grep -q "^blocked https://not-allowed.example/" "$WORK/out.txt" || { echo "FAIL srt allowlist not enforced"; fail=1; }
if grep -q "not-allowed.example" "$WORK/egress.log"; then echo "FAIL srt forwarded a host outside its allowlist"; fail=1; fi
[[ $fail == 0 ]] && echo "chain: all checks behaved as required"
exit $fail
