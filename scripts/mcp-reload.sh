#!/usr/bin/env bash
# mcp-reload.sh - restart an MCP server's stdio children and reconnect it in OpenCode.
#
# Why this exists: OpenCode does not retry an MCP server after its child process
# dies. A killed child (code reload, OOM, manual `kill`) leaves the connection in
# `failed` state until the server is restarted or /api/experimental/mcp/<name>/connect
# is called. This script does the kill + reconnect in one step.
#
# Usage:
#   scripts/mcp-reload.sh                    # ai-memory, kill + reconnect
#   scripts/mcp-reload.sh ai-memory --no-kill   # reconnect only
#   scripts/mcp-reload.sh --dry-run          # show what would happen
#   MCP_PORT=4096 scripts/mcp-reload.sh      # target a specific port
#
# Env:
#   MCP_PATTERN              process cmdline substring to kill (default: <server>-mcp with - as _)
#   MCP_PORT                 only talk to this port
#   MCP_RECONNECT_TIMEOUT    seconds to wait for `connected` (default 120)
#
# Note: only servers listening on an HTTP port can be reconnected. An
# `opencode serve --service` instance (TUI/desktop backend) has no HTTP port, so
# killing its MCP children requires restarting that service.

set -uo pipefail

SERVER="ai-memory"
KILL=1
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-kill) KILL=0 ;;
    --dry-run) DRY=1 ;;
    --port) MCP_PORT="$2"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) SERVER="$1" ;;
  esac
  shift
done

PATTERN="${MCP_PATTERN:-${SERVER//-/_}-mcp}"
[ "$PATTERN" = "${SERVER//-/_}-mcp" ] && PATTERN_ALT="${SERVER}-mcp" || PATTERN_ALT="$PATTERN"
TIMEOUT="${MCP_RECONNECT_TIMEOUT:-120}"

say() { printf '%s\n' "$*"; }
err() { printf 'error: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v pgrep >/dev/null 2>&1 || die "pgrep not found"

# ---- 1. discover opencode HTTP servers -------------------------------------
targets=()
while read -r pid; do
  [ -n "$pid" ] || continue
  [ -r "/proc/$pid/cmdline" ] || continue
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || continue
  port=$(sed -n 's/.*--port[ =]\+\([0-9]\+\).*/\1/p' <<<"$cmd")
  [ -n "$port" ] || continue
  if [ -n "${MCP_PORT:-}" ] && [ "$port" != "$MCP_PORT" ]; then continue; fi
  pw=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n 's/^OPENCODE_SERVER_PASSWORD=//p' | head -1)
  targets+=("$pid|$port|$pw")
done < <(pgrep -f 'opencode serve' 2>/dev/null)

[ ${#targets[@]} -gt 0 ] || die "no 'opencode serve --port <n>' process found (set MCP_PORT?)"

# ---- 2. kill stale children of the target servers only ---------------------
# Only descendants of the HTTP servers we can reconnect get killed. Killing the
# `--service` instance's children would leave it failed with no way back short of
# a service restart.
descendants() { # $1 pid -> recursive child pids
  local kids
  kids=$(pgrep -P "$1" 2>/dev/null) || return 0
  printf '%s\n' $kids
  local k
  for k in $kids; do descendants "$k"; done
}

kill_pids=()
if [ "$KILL" -eq 1 ]; then
  for t in "${targets[@]}"; do
    IFS='|' read -r opid _ _ <<<"$t"
    while read -r dpid; do
      [ -n "$dpid" ] || continue
      dcmd=$(tr '\0' ' ' <"/proc/$dpid/cmdline" 2>/dev/null) || continue
      case "$dcmd" in
        *"$PATTERN"*|*"$PATTERN_ALT"*) kill_pids+=("$dpid") ;;
      esac
    done < <(descendants "$opid")
  done
  mapfile -t kill_pids < <(printf '%s\n' "${kill_pids[@]}" | sort -u | grep -v '^$' || true)
  if [ ${#kill_pids[@]} -eq 0 ]; then
    say "no '$PATTERN' processes under the target servers"
  else
    say "killing $PATTERN: ${kill_pids[*]}"
    if [ "$DRY" -eq 1 ]; then
      say "dry run: not killing"
    else
      kill "${kill_pids[@]}" 2>/dev/null
      sleep 1
      still=$(pgrep -f -- "$PATTERN" 2>/dev/null | tr '\n' ' ')
      still=$(comm -12 <(printf '%s\n' $still | sort) <(printf '%s\n' "${kill_pids[@]}" | sort) | tr '\n' ' ')
      [ -n "$still" ] && { say "SIGTERM ignored, sending SIGKILL: $still"; kill -9 $still 2>/dev/null; sleep 1; }
    fi
  fi
fi

# ---- 3. reconnect + poll ---------------------------------------------------
status_of() { # $1 port, $2 password, $3 server
  curl -s -m 10 ${2:+-u "opencode:$2"} "localhost:$1/api/mcp" 2>/dev/null |
    python3 -c '
import json,sys
want=sys.argv[1]
try: d=json.load(sys.stdin)
except Exception: print("unknown"); raise SystemExit
for s in d.get("data",[]):
    if s.get("name")==want:
        st=s.get("status",{})
        print(st.get("status","?"))
        raise SystemExit
print("missing")' "$3"
}

rc=0
for t in "${targets[@]}"; do
  IFS='|' read -r pid port pw <<<"$t"
  say "reconnecting '$SERVER' on port $port (opencode pid $pid)"
  if [ "$DRY" -eq 1 ]; then
    say "dry run: would POST /api/experimental/mcp/$SERVER/connect"
    continue
  fi
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST \
    ${pw:+-u "opencode:$pw"} \
    "localhost:$port/api/experimental/mcp/$SERVER/connect?location%5Bdirectory%5D=%2Fhome%2Fllm")
  case "$code" in
    204) say "  connect accepted (204), waiting for handshake..." ;;
    404) err "  server '$SERVER' not configured on port $port (404)"; rc=1; continue ;;
    401) err "  auth failed on port $port (401) - no OPENCODE_SERVER_PASSWORD in process env"; rc=1; continue ;;
    *) err "  connect returned HTTP $code on port $port"; rc=1; continue ;;
  esac

  deadline=$(( $(date +%s) + TIMEOUT ))
  st=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    st=$(status_of "$port" "$pw" "$SERVER")
    [ "$st" = "connected" ] && break
    sleep 3
  done
  if [ "$st" = "connected" ]; then
    say "  connected (tools available on the next prompt)"
  else
    err "  still '$st' after ${TIMEOUT}s"
    rc=1
  fi
done

exit $rc
