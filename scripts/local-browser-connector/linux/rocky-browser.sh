#!/usr/bin/env bash
set -euo pipefail

# Generic Hermes/Rocky local browser connector for Linux desktops.
# Starts a visible isolated Chromium-family browser and optionally opens reverse
# SSH tunnels for CDP and the localhost launcher. Tenant values come from env.

APP_NAME="${ROCKY_BROWSER_APP_NAME:-rocky-browser}"
CDP_HOST="${ROCKY_BROWSER_CDP_HOST:-127.0.0.1}"
CDP_PORT="${ROCKY_BROWSER_CDP_PORT:-9222}"
PROFILE_DIR="${ROCKY_BROWSER_PROFILE_DIR:-$HOME/.hermes/rocky-browser}"
LOG_DIR="${ROCKY_BROWSER_LOG_DIR:-$HOME/.hermes/logs}"
PID_FILE="${ROCKY_BROWSER_PID_FILE:-$HOME/.hermes/rocky-browser.pid}"
TUNNEL_PID_FILE="${ROCKY_BROWSER_TUNNEL_PID_FILE:-$HOME/.hermes/rocky-browser-tunnel.pid}"
SSH_TARGET="${ROCKY_BROWSER_SSH_TARGET:-}"
SSH_PORT="${ROCKY_BROWSER_SSH_PORT:-22}"
SSH_IDENTITY_FILE="${ROCKY_BROWSER_SSH_IDENTITY_FILE:-}"
VPS_BIND="${ROCKY_BROWSER_VPS_BIND:-127.0.0.1}"
VPS_PORT="${ROCKY_BROWSER_VPS_PORT:-$CDP_PORT}"
START_URL="${ROCKY_BROWSER_START_URL:-about:blank}"
BROWSER_BIN="${ROCKY_BROWSER_BIN:-}"
LAUNCHER_HOST="${ROCKY_BROWSER_LAUNCHER_HOST:-127.0.0.1}"
LAUNCHER_PORT="${ROCKY_BROWSER_LAUNCHER_PORT:-18765}"
LAUNCHER_PID_FILE="${ROCKY_BROWSER_LAUNCHER_PID_FILE:-$HOME/.hermes/rocky-browser-launcher.pid}"
LAUNCHER_CONTROL_URL="http://127.0.0.1:${LAUNCHER_PORT}"

mkdir -p "$PROFILE_DIR" "$LOG_DIR"

say() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; }

require_loopback() {
  local name="$1" value="$2"
  if [[ "$value" != "127.0.0.1" && "$value" != "localhost" ]]; then
    err "$name must stay on loopback, got: $value"
    exit 2
  fi
}

read_pid() {
  local f="$1"
  [[ -f "$f" ]] && tr -dc '0-9' < "$f" || true
}

is_pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

find_browser() {
  if [[ -n "$BROWSER_BIN" && -x "$BROWSER_BIN" ]]; then
    printf '%s\n' "$BROWSER_BIN"
    return 0
  fi
  local candidates=(google-chrome-stable google-chrome chromium chromium-browser brave-browser brave-browser-stable microsoft-edge microsoft-edge-stable)
  local c
  for c in "${candidates[@]}"; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

port_ready() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time 1 "http://$CDP_HOST:$CDP_PORT/json/version" >/dev/null 2>&1
}

ssh_key() {
  local p="${SSH_IDENTITY_FILE/#\~/$HOME}"
  [[ -n "$p" ]] || { err "ROCKY_BROWSER_SSH_IDENTITY_FILE is required for tunnel mode"; return 1; }
  [[ -f "$p" ]] || { err "SSH identity file does not exist: $p"; return 1; }
  printf '%s\n' "$p"
}

ssh_target() {
  [[ -n "$SSH_TARGET" ]] || { err "ROCKY_BROWSER_SSH_TARGET is required for tunnel mode"; return 1; }
  printf '%s\n' "$SSH_TARGET"
}

start_browser() {
  require_loopback ROCKY_BROWSER_CDP_HOST "$CDP_HOST"
  local existing bin pid
  existing="$(read_pid "$PID_FILE")"
  if is_pid_alive "$existing" && port_ready; then
    say "Rocky browser already running (pid $existing)."
    return 0
  fi
  bin="$(find_browser)" || { err "No supported Chromium-family browser found. Install Chromium/Chrome/Brave/Edge or set ROCKY_BROWSER_BIN."; return 1; }
  say "Starting Rocky browser with isolated profile: $PROFILE_DIR"
  nohup "$bin" \
    --remote-debugging-address="$CDP_HOST" \
    --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --disable-features=Translate \
    --class="$APP_NAME" \
    "$START_URL" \
    > "$LOG_DIR/rocky-browser.log" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"
  for _ in {1..30}; do
    if port_ready; then
      say "Rocky browser ready at http://$CDP_HOST:$CDP_PORT (pid $pid)."
      return 0
    fi
    sleep 0.5
  done
  err "Browser started (pid $pid), but CDP did not become ready. Log: $LOG_DIR/rocky-browser.log"
  return 2
}

stop_browser() {
  local pid
  pid="$(read_pid "$PID_FILE")"
  if is_pid_alive "$pid"; then
    say "Stopping Rocky browser (pid $pid)."
    kill "$pid" 2>/dev/null || true
  else
    say "Rocky browser is not running."
  fi
  rm -f "$PID_FILE"
}

start_tunnel() {
  require_loopback ROCKY_BROWSER_CDP_HOST "$CDP_HOST"
  require_loopback ROCKY_BROWSER_VPS_BIND "$VPS_BIND"
  local existing key target pid
  existing="$(read_pid "$TUNNEL_PID_FILE")"
  if is_pid_alive "$existing"; then
    say "Reverse tunnel already running (pid $existing)."
    return 0
  fi
  port_ready || { err "Local browser CDP is not ready. Run: $0 start"; return 1; }
  key="$(ssh_key)" || return 1
  target="$(ssh_target)" || return 1
  chmod 600 "$key" 2>/dev/null || true
  say "Starting reverse SSH tunnel via $target:$SSH_PORT"
  nohup ssh -N \
    -p "$SSH_PORT" \
    -o IdentitiesOnly=yes \
    -i "$key" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R "$VPS_BIND:$VPS_PORT:$CDP_HOST:$CDP_PORT" \
    "$target" \
    > "$LOG_DIR/rocky-browser-tunnel.log" 2>&1 &
  pid=$!
  echo "$pid" > "$TUNNEL_PID_FILE"
  sleep 2
  if is_pid_alive "$pid"; then
    say "Reverse tunnel running (pid $pid). VPS endpoint: http://$VPS_BIND:$VPS_PORT"
    return 0
  fi
  err "Reverse tunnel failed. Last log lines:"
  tail -40 "$LOG_DIR/rocky-browser-tunnel.log" >&2 || true
  rm -f "$TUNNEL_PID_FILE"
  return 1
}

stop_tunnel() {
  local pid
  pid="$(read_pid "$TUNNEL_PID_FILE")"
  if is_pid_alive "$pid"; then
    say "Stopping reverse tunnel (pid $pid)."
    kill "$pid" 2>/dev/null || true
  else
    say "Reverse tunnel is not running."
  fi
  rm -f "$TUNNEL_PID_FILE"
}

status() {
  local bpid tpid lpid
  bpid="$(read_pid "$PID_FILE")"
  tpid="$(read_pid "$TUNNEL_PID_FILE")"
  lpid="$(read_pid "$LAUNCHER_PID_FILE")"
  say "Rocky browser connector status"
  say "  browser pid:    ${bpid:-none} $(is_pid_alive "$bpid" && echo '(running)' || echo '(not running)')"
  say "  local CDP:      http://$CDP_HOST:$CDP_PORT $(port_ready && echo '(ready)' || echo '(not ready)')"
  say "  profile:        $PROFILE_DIR"
  say "  tunnel pid:     ${tpid:-none} $(is_pid_alive "$tpid" && echo '(running)' || echo '(not running)')"
  say "  SSH target:     ${SSH_TARGET:-not configured}"
  say "  SSH port:       $SSH_PORT"
  say "  VPS CDP:        http://$VPS_BIND:$VPS_PORT"
  say "  launcher pid:   ${lpid:-none} $(is_pid_alive "$lpid" && echo '(running)' || echo '(not running)')"
  say "  launcher local: http://$LAUNCHER_HOST:$LAUNCHER_PORT $(curl -fsS --max-time 1 "$LAUNCHER_CONTROL_URL/health" >/dev/null 2>&1 && echo '(ready)' || echo '(not ready)')"
}

launcher_service() {
  require_loopback ROCKY_BROWSER_LAUNCHER_HOST "$LAUNCHER_HOST"
  require_loopback ROCKY_BROWSER_VPS_BIND "$VPS_BIND"
  local key target py_pid
  key="$(ssh_key)" || return 1
  target="$(ssh_target)" || return 1
  chmod 600 "$key" 2>/dev/null || true
  mkdir -p "$LOG_DIR"
  say "Starting Rocky launcher HTTP on $LAUNCHER_HOST:$LAUNCHER_PORT"
  ROCKY_BROWSER_SCRIPT="$0" ROCKY_BROWSER_LOG_DIR="$LOG_DIR" ROCKY_BROWSER_LAUNCHER_HOST="$LAUNCHER_HOST" ROCKY_BROWSER_LAUNCHER_PORT="$LAUNCHER_PORT" \
  python3 - <<'PY' > "$LOG_DIR/rocky-browser-launcher.log" 2>&1 &
import json, os, subprocess, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST = os.environ.get('ROCKY_BROWSER_LAUNCHER_HOST', '127.0.0.1')
PORT = int(os.environ.get('ROCKY_BROWSER_LAUNCHER_PORT', '18765'))
SCRIPT = os.environ['ROCKY_BROWSER_SCRIPT']

def run_cmd(args):
    p = subprocess.run(args, env=os.environ.copy(), text=True, capture_output=True, timeout=90)
    return {'exit_code': p.returncode, 'stdout': p.stdout[-8000:], 'stderr': p.stderr[-8000:]}

class Handler(BaseHTTPRequestHandler):
    server_version = 'HermesLocalBrowserLauncher/0.1'
    def log_message(self, fmt, *args):
        return
    def send_json(self, code, payload):
        body = json.dumps(payload, indent=2).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/health', '/'):
            self.send_json(200, {'ok': True, 'service': 'hermes-local-browser-launcher', 'time': time.time()})
        elif path == '/status':
            self.send_json(200, {'ok': True, 'command': 'status', 'result': run_cmd([SCRIPT, 'status'])})
        elif path in ('/open', '/up'):
            self.send_json(200, {'ok': True, 'command': 'up', 'result': run_cmd([SCRIPT, 'up'])})
        elif path in ('/down', '/close'):
            self.send_json(200, {'ok': True, 'command': 'down', 'result': run_cmd([SCRIPT, 'down'])})
        else:
            self.send_json(404, {'ok': False, 'error': 'unknown path', 'paths': ['/health','/status','/open','/down']})
    do_POST = do_GET

httpd = ThreadingHTTPServer((HOST, PORT), Handler)
print(f'Hermes local browser launcher listening at http://{HOST}:{PORT}', flush=True)
httpd.serve_forever()
PY
  py_pid=$!
  echo "$py_pid" > "$LAUNCHER_PID_FILE"
  cleanup_launcher() { kill "$py_pid" 2>/dev/null || true; rm -f "$LAUNCHER_PID_FILE"; }
  trap cleanup_launcher EXIT INT TERM
  sleep 1
  is_pid_alive "$py_pid" || { err "Launcher HTTP server failed. Log: $LOG_DIR/rocky-browser-launcher.log"; return 1; }
  say "Opening launcher reverse tunnel: VPS 127.0.0.1:$LAUNCHER_PORT -> local 127.0.0.1:$LAUNCHER_PORT"
  exec ssh -N \
    -p "$SSH_PORT" \
    -o IdentitiesOnly=yes \
    -i "$key" \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R "127.0.0.1:$LAUNCHER_PORT:$LAUNCHER_HOST:$LAUNCHER_PORT" \
    "$target"
}

install_self() {
  mkdir -p "$HOME/.local/bin"
  cp "$0" "$HOME/.local/bin/rocky-browser"
  chmod +x "$HOME/.local/bin/rocky-browser"
  say "Installed: $HOME/.local/bin/rocky-browser"
}

install_launcher_service() {
  local script_path service_dir service_file env_file
  script_path="$HOME/.local/bin/rocky-browser"
  [[ -x "$script_path" ]] || install_self
  service_dir="$HOME/.config/systemd/user"
  service_file="$service_dir/rocky-browser-launcher.service"
  env_file="$HOME/.config/rocky-browser.env"
  mkdir -p "$service_dir"
  if [[ ! -f "$env_file" ]]; then
    cat > "$env_file" <<EOF
# Fill these tenant-specific values before starting the service.
ROCKY_BROWSER_SSH_TARGET=
ROCKY_BROWSER_SSH_PORT=22
ROCKY_BROWSER_SSH_IDENTITY_FILE=$HOME/.ssh/<tenant-key>
ROCKY_BROWSER_CDP_PORT=9222
ROCKY_BROWSER_VPS_PORT=9222
ROCKY_BROWSER_LAUNCHER_PORT=18765
ROCKY_BROWSER_PROFILE_DIR=$HOME/.hermes/rocky-browser
ROCKY_BROWSER_START_URL=about:blank
# Optional: ROCKY_BROWSER_BIN=/usr/bin/chromium
EOF
    chmod 600 "$env_file"
  fi
  cat > "$service_file" <<EOF
[Unit]
Description=Hermes Rocky local browser launcher reverse tunnel
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
ExecStart=$script_path launcher-service
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable rocky-browser-launcher.service
  say "Installed user service: $service_file"
  say "Edit tenant values in: $env_file"
  say "Start it with: systemctl --user start rocky-browser-launcher.service"
}

case "${1:-status}" in
  install) install_self ;;
  start) start_browser ;;
  stop) stop_browser ;;
  restart) stop_browser; start_browser ;;
  tunnel) start_tunnel ;;
  tunnel-stop) stop_tunnel ;;
  up) start_browser; start_tunnel ;;
  down) stop_tunnel; stop_browser ;;
  status|launcher-status) status ;;
  launcher-service) launcher_service ;;
  launcher-install) install_launcher_service ;;
  *)
    cat <<EOF
Usage: $0 install|start|stop|restart|status|tunnel|tunnel-stop|up|down|launcher-service|launcher-install|launcher-status

Required for tunnel/launcher-service:
  ROCKY_BROWSER_SSH_TARGET=user@vps.example
  ROCKY_BROWSER_SSH_IDENTITY_FILE=/path/to/key

Optional:
  ROCKY_BROWSER_SSH_PORT=22
  ROCKY_BROWSER_CDP_PORT=9222
  ROCKY_BROWSER_VPS_PORT=9222
  ROCKY_BROWSER_LAUNCHER_PORT=18765
  ROCKY_BROWSER_PROFILE_DIR=$HOME/.hermes/rocky-browser
  ROCKY_BROWSER_BIN=/path/to/chrome
  ROCKY_BROWSER_START_URL=about:blank
EOF
    exit 2
    ;;
esac
