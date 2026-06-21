#!/usr/bin/env bash
# aether — turnkey setup + run (PRD §6 no-hardware gate; single-origin serve).
#
#   git clone … && cd aether
#   ./run.sh                 # first run installs everything, then serves on :8000
#   ./run.sh                 # later runs just start (fast)
#
# What it does, idempotently:
#   1. preflight: python ≥3.11, node, npm, a broker (native mosquitto or Docker)
#   2. config:    copy .env.example → .env on first run, then source it
#   3. backend:   create .venv + `pip install -e ".[dev]"` (stamped; skipped if current)
#   4. frontend:  `npm ci` + `npm run build` (skipped if dist/ is up to date)
#   5. broker:    start a loopback-bound Mosquitto (reused if one already runs)
#   6. serve:     uvicorn serves the API, /ws/v2, AND the built SPA — one URL, one
#                 process — at http://127.0.0.1:8000, and opens your browser
#
# Ctrl-C stops the app and any broker this script started. Everything binds
# loopback (PRD §5/§6); expose to your tailnet with `tailscale serve`, never Funnel.
#
# Flags:
#   --port N           serve on a different port (default 8000)
#   --host H           bind a different host (default 127.0.0.1 — keep it loopback)
#   --no-open          don't launch a browser
#   --rebuild          force a frontend rebuild
#   --reinstall        force venv + npm reinstall
#   --with-lightning   also install the GLM lightning extra (netCDF4)
#   --install-service  write a systemd --user unit so aether starts on boot, then exit
#   --service          internal: run headless for systemd (no browser)
#   -h, --help         show this help

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="127.0.0.1"
PORT="8000"
OPEN_BROWSER=1
REBUILD=""
REINSTALL=""
WITH_LIGHTNING=""
INSTALL_SERVICE=""
SERVICE_MODE=""

RUNTIME_DIR="$ROOT/.aether"
BROKER_LOG="$RUNTIME_DIR/mosquitto.log"
BROKER_CONF="$ROOT/deploy/mosquitto/mosquitto.local.conf"
DEPS_STAMP="$ROOT/.venv/.aether-deps"

# Things this run started, so cleanup only stops what it owns.
BROKER_PID=""
BROKER_DOCKER=""
OPENER_PID=""

# ── tiny helpers ──────────────────────────────────────────────────────────────
c_info() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
c_ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
c_err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }
die()    { c_err "$*"; exit 1; }

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

# loopback port reachable? (bash /dev/tcp, no extra tooling)
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&-; }

cleanup() {
  [ -n "$OPENER_PID" ] && kill "$OPENER_PID" 2>/dev/null || true
  if [ -n "$BROKER_PID" ]; then
    c_info "stopping broker (pid $BROKER_PID)"
    kill "$BROKER_PID" 2>/dev/null || true
  fi
  if [ -n "$BROKER_DOCKER" ]; then
    c_info "stopping Docker broker"
    docker compose down 2>/dev/null || true
  fi
}

# ── args ──────────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --no-open) OPEN_BROWSER=0; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --reinstall) REINSTALL=1; shift ;;
    --with-lightning) WITH_LIGHTNING=1; shift ;;
    --install-service) INSTALL_SERVICE=1; shift ;;
    --service) SERVICE_MODE=1; OPEN_BROWSER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

mkdir -p "$RUNTIME_DIR"

# ── 1. preflight ──────────────────────────────────────────────────────────────
preflight() {
  c_info "preflight"
  command -v python3 >/dev/null || die "python3 not found — install Python ≥3.11"
  python3 - <<'PY' || die "Python ≥3.11 required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  command -v node >/dev/null || die "node not found — install Node 18+ (e.g. via nvm or apt)"
  command -v npm  >/dev/null || die "npm not found — install Node 18+ which bundles npm"
  if ! command -v mosquitto >/dev/null && ! command -v docker >/dev/null; then
    die "no MQTT broker available — install one: 'sudo apt install mosquitto' (recommended) or Docker"
  fi
  c_ok "python $(python3 -V 2>&1 | awk '{print $2}'), node $(node -v), npm $(npm -v)"
}

# ── 2. config (.env) ──────────────────────────────────────────────────────────
load_env() {
  if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    c_warn "created .env from template — edit it to add your station coords / API keys"
  fi
  set -a; . "$ROOT/.env"; set +a
  # Turnkey default: show the demo map unless .env says otherwise.
  : "${AETHER_DEMO_SOURCE:=1}"; export AETHER_DEMO_SOURCE
  # Single-origin serve: tell the backend where the built SPA lives (abs path,
  # so it resolves regardless of CWD / systemd WorkingDirectory).
  export AETHER_FRONTEND_DIST="$ROOT/frontend/dist"
}

# ── 3. backend (venv + editable install) ──────────────────────────────────────
needs_deps() {
  [ -n "$REINSTALL" ] && return 0
  [ ! -f "$DEPS_STAMP" ] && return 0
  [ "$ROOT/pyproject.toml" -nt "$DEPS_STAMP" ] && return 0
  return 1
}

setup_backend() {
  if [ ! -d "$ROOT/.venv" ] || [ -n "$REINSTALL" ]; then
    c_info "creating virtualenv (.venv)"
    python3 -m venv "$ROOT/.venv"
  fi
  # shellcheck disable=SC1091
  . "$ROOT/.venv/bin/activate"
  if needs_deps; then
    local extras="dev"
    [ -n "$WITH_LIGHTNING" ] && extras="dev,lightning"
    c_info "installing Python deps (.[${extras}]) — first run takes a minute"
    python -m pip install -q --upgrade pip
    python -m pip install -q -e ".[${extras}]"
    touch "$DEPS_STAMP"
    c_ok "backend deps installed"
  else
    c_ok "backend deps up to date"
  fi
}

# ── 4. frontend (npm + vite build) ────────────────────────────────────────────
needs_build() {
  [ -n "$REBUILD" ] && return 0
  [ ! -f "$ROOT/frontend/dist/index.html" ] && return 0
  # any source/config newer than the built entry → rebuild
  local newer
  newer="$(find "$ROOT/frontend/src" "$ROOT/frontend/index.html" \
      "$ROOT/frontend/package.json" "$ROOT/frontend/vite.config.ts" \
      -newer "$ROOT/frontend/dist/index.html" -print -quit 2>/dev/null || true)"
  [ -n "$newer" ]
}

setup_frontend() {
  if [ ! -d "$ROOT/frontend/node_modules" ] || [ -n "$REINSTALL" ]; then
    c_info "installing frontend deps (npm ci)"
    ( cd "$ROOT/frontend" && npm ci )
  fi
  if needs_build; then
    c_info "building frontend (vite)"
    ( cd "$ROOT/frontend" && npm run build )
    c_ok "frontend built → frontend/dist"
  else
    c_ok "frontend build up to date"
  fi
}

# ── 5. broker ─────────────────────────────────────────────────────────────────
start_broker() {
  if port_open 1883; then
    c_ok "MQTT broker already running on 127.0.0.1:1883 — reusing it"
    return
  fi
  if command -v mosquitto >/dev/null; then
    c_info "starting Mosquitto (loopback) → $BROKER_LOG"
    mosquitto -c "$BROKER_CONF" >"$BROKER_LOG" 2>&1 &
    BROKER_PID=$!
  elif command -v docker >/dev/null; then
    c_info "starting Mosquitto via Docker"
    docker compose up -d
    BROKER_DOCKER=1
  else
    die "no broker available (mosquitto/docker)"
  fi
  # wait up to ~10s for the listener
  for _ in $(seq 1 50); do
    port_open 1883 && { c_ok "broker up on 127.0.0.1:1883"; return; }
    sleep 0.2
  done
  die "broker did not come up — see $BROKER_LOG"
}

# ── browser opener (background; waits for the server then opens) ───────────────
open_browser_when_ready() {
  [ "$OPEN_BROWSER" -eq 1 ] || return 0
  command -v xdg-open >/dev/null || return 0
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 0
  (
    for _ in $(seq 1 50); do
      port_open "$PORT" && { xdg-open "http://${HOST}:${PORT}" >/dev/null 2>&1; break; }
      sleep 0.2
    done
  ) &
  OPENER_PID=$!
}

# ── --install-service (systemd --user unit) ───────────────────────────────────
install_service() {
  local unit_dir="$HOME/.config/systemd/user"
  local unit="$unit_dir/aether.service"
  mkdir -p "$unit_dir"
  cat >"$unit" <<EOF
[Unit]
Description=aether — common operating picture
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/run.sh --service
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  c_ok "wrote $unit"
  echo
  echo "Enable it to start on boot:"
  echo "    systemctl --user enable --now aether"
  echo "    loginctl enable-linger $USER   # run without an active login (headless Pi)"
  echo
  echo "Logs:  journalctl --user -u aether -f"
}

# ── main ──────────────────────────────────────────────────────────────────────
preflight
load_env
setup_backend
setup_frontend

if [ -n "$INSTALL_SERVICE" ]; then
  install_service
  exit 0
fi

trap cleanup EXIT INT TERM
start_broker
open_browser_when_ready

c_ok "serving aether at http://${HOST}:${PORT}  (Ctrl-C to stop)"
[ "${AETHER_DEMO_SOURCE}" = "1" ] && c_info "demo source ON — set AETHER_DEMO_SOURCE=0 in .env once real sources are configured"
# Foreground (not exec) so the cleanup trap fires on Ctrl-C / SIGTERM.
uvicorn aether.backend.main:app --host "$HOST" --port "$PORT"
