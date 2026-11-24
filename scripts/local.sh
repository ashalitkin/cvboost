#!/usr/bin/env bash
# Run CVBoost on this machine (host mode: Claude auth from the keychain or `claude login`).
# macOS and Linux. Personal values (TZ, CVBOOST_MAIL_FORWARDERS, ...) live in .env only.
# Usage: scripts/local.sh up | down | status | logs
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
PIDS=$ROOT/data/pids; mkdir -p "$PIDS" "$ROOT/data/logs"
OS=$(uname -s)
SERVICES="worker web"
[ "$OS" = Darwin ] && SERVICES="worker web awake"   # `awake` = caffeinate, macOS only

load_env() {
  set -a
  # shellcheck source=/dev/null
  [ -f .env ] && source ./.env
  set +a
  export CVBOOST_DB=${CVBOOST_DB:-$ROOT/data/cvboost.sqlite}
  export CVBOOST_RESUME_DIR=${CVBOOST_RESUME_DIR:-$ROOT/config/resume}
  # live plugin settings stay out of git; the repo config keeps everything off.
  # data/<name>.local.yaml -> CVBOOST_<NAME>_CONFIG (unless already set)
  local lf var base
  for lf in "$ROOT"/data/*.local.yaml; do
    [ -f "$lf" ] || continue
    base=$(basename "$lf" .local.yaml)
    case $base in
      ''|*[!a-z0-9_-]*) echo "warning: ignoring $lf (name must match [a-z0-9_-]+)" >&2; continue ;;
    esac
    var=CVBOOST_$(printf '%s' "$base" | tr 'a-z-' 'A-Z_')_CONFIG
    [ -n "${!var:-}" ] || export "$var=$lf"
  done
  export CVBOOST_PANEL_URL=${CVBOOST_PANEL_URL:-http://127.0.0.1:8080}
  # stable panel CSRF secret so a restart does not break open tabs (file is local, never printed)
  if [ -z "${CVBOOST_WEB_SECRET:-}" ]; then
    local sf=$ROOT/data/.web_secret
    [ -s "$sf" ] || (umask 077; openssl rand -hex 32 >"$sf")
    CVBOOST_WEB_SECRET=$(<"$sf")
    export CVBOOST_WEB_SECRET
  fi
  # Homebrew pango for weasyprint (macOS only)
  if [ "$OS" = Darwin ] && [ -d /opt/homebrew/lib ]; then
    export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
  fi
  return 0
}

lan_address() { # the address of this machine on the local network, empty if unknown
  if [ "$OS" = Darwin ]; then
    ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true
  else
    hostname -I 2>/dev/null | awk '{print $1}' || true
  fi
}

lan_hostname() { # extra Host header accepted by the panel (macOS: Bonjour name)
  if [ "$OS" = Darwin ]; then
    local n
    n=$(scutil --get LocalHostName 2>/dev/null || true)
    [ -n "$n" ] && echo "$n.local"
  fi
  return 0
}

running() { [ -f "$PIDS/$1.pid" ] && kill -0 "$(cat "$PIDS/$1.pid")" 2>/dev/null; }

start() { # name, command...
  local name=$1; shift
  if running "$name"; then echo "$name: already running (pid $(cat "$PIDS/$name.pid"))"; return; fi
  # not nohup: it is a SIP-protected binary and macOS strips DYLD_* from its env,
  # so weasyprint would not find pango and PDFs would not build
  ( trap '' HUP; exec "$@" ) </dev/null >>"$ROOT/data/logs/$name.log" 2>&1 &
  echo $! >"$PIDS/$name.pid"; echo "$name: started (pid $!)"
}

case "${1:-status}" in
  up)
    load_env
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$CVBOOST_DB" ".backup $ROOT/data/backup-$(date +%Y%m%d-%H%M).sqlite"
    else
      echo "sqlite3 not found: no backup made" >&2
    fi
    uv run cvboost sources >/dev/null 2>&1 || true   # apply DB migrations once, before worker and web race for them
    start worker uv run cvboost worker
    # Phone on the same Wi-Fi: CVBOOST_WEB_LAN=1 in .env, together with CVBOOST_WEB_USER and
    # CVBOOST_WEB_PASSWORD (cvboost web refuses a non-loopback address without basic auth).
    bind=127.0.0.1; lan=""
    if [ "${CVBOOST_WEB_LAN:-}" = 1 ]; then
      lan=$(lan_address)
      if [ -n "$lan" ]; then
        bind=0.0.0.0
        extra=$(lan_hostname)
        export CVBOOST_WEB_HOSTS=${CVBOOST_WEB_HOSTS:-localhost,127.0.0.1,$lan${extra:+,$extra}}
      fi
    fi
    start web uv run cvboost web --host "$bind" --port 8080
    if [ "$OS" = Darwin ]; then
      start awake caffeinate -is   # keep the Mac awake while on power (lid open)
    fi
    echo "Panel: http://127.0.0.1:8080"
    [ -n "$lan" ] && echo "Phone (same Wi-Fi): http://$lan:8080" || true ;;
  down)
    for n in $SERVICES; do
      if running "$n"; then
        p=$(cat "$PIDS/$n.pid")
        all="$p $(pgrep -P "$p" | tr '\n' ' ' || true)"   # uv parent + its python child
        # shellcheck disable=SC2086
        kill -TERM $all 2>/dev/null || true
        # the worker finishes its current cycle before exiting: wait up to 120 s
        for _ in $(seq 1 240); do
          alive=0
          # shellcheck disable=SC2086
          for q in $all; do kill -0 "$q" 2>/dev/null && alive=1; done
          [ "$alive" = 1 ] || break
          sleep 0.5
        done
        echo "$n: stopped"
      fi
      rm -f "$PIDS/$n.pid"
    done ;;
  status)
    for n in $SERVICES; do running "$n" && echo "$n: running (pid $(cat "$PIDS/$n.pid"))" || echo "$n: stopped"; done
    load_env; uv run cvboost worker --health && echo "worker heartbeat: ok" || echo "worker heartbeat: stale/none" ;;
  logs) tail -n 50 -f "$ROOT/data/logs/worker.log" ;;
  *) echo "usage: $0 up|down|status|logs"; exit 2 ;;
esac
