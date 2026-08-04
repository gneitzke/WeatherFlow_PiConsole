#!/usr/bin/env bash
# Almanac kiosk launcher — shows the HTML weather overlay fullscreen on the Pi,
# fed live by the console. Runs as the console user inside their X session.
#
# Architecture:
#   1. DATA ENGINE — the console runs HEADLESS on a virtual X display (Xvfb :1)
#      with [Display] LayoutStyle=almanac, so lib/almanac_emit.py writes
#      /tmp/wfp_data/wx.json (~every 2s). It never touches the real screen.
#   2. SERVER — a tiny HTTP server serves the overlay page + wx.json (same origin).
#   3. DISPLAY — chromium --kiosk shows the page fullscreen on the real display :0,
#      with touch enabled and the cursor hidden.
#
# Prereq (one time):  sudo apt-get install -y xvfb   (chromium-browser is already present)
# This launcher REPLACES the on-screen Kivy console: stop/disable wfpiconsole.service
# and autostart this instead (see README.md). Revert = re-enable wfpiconsole.service.
set -u

APP="${WFP_APP:-$HOME/wfpiconsole}"                 # console install dir
PY="$APP/venv/bin/python3"
WEB="${WFP_WEB:-$HOME/almanac_web}"                 # served dir (index.html + wx.json)
DATA_DIR="/tmp/wfp_data"; DATA="$DATA_DIR/wx.json"
PORT="${WFP_PORT:-8137}"; VDISP="${WFP_VDISP:-:1}"
THEME="${WFP_THEME:-night}"                          # night (dark) | paper (light)
UDD="/tmp/almanac_chrome"                            # chromium profile (wiped each launch)
HTTP_LOG="/tmp/almanac_http.log"

# real session env — chromium needs the exact session bus to map a window + touch.
# Force the standard paths (autostart may carry a different/empty value).
export DISPLAY=":0"
export XAUTHORITY="$HOME/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

# ── COLD-BOOT RACE (the root cause of "fragile on reboot") ────────────────────
# If chromium launches before the VC4 GPU/compositor is ready, its GPU process
# initialises into a broken state and composites a BLANK WHITE window that never
# recovers. Gate the launch on real readiness signals, not a fixed sleep:
#   1) the window manager (openbox) is up,
#   2) the X server is actually answering ( xset q ),
#   3) a short settle for the GPU stack.
# The watchdog below is the belt-and-braces guarantee if the race still slips through.
for _ in $(seq 1 60); do pgrep -x openbox >/dev/null 2>&1 && break; sleep 1; done
for _ in $(seq 1 30); do xset q          >/dev/null 2>&1 && break; sleep 1; done
sleep 5

# RESTART-SAFE: if a previous instance died uncleanly (SIGKILL, OOM), its Xvfb /
# engine / server / chromium children are orphaned onto init and would collide
# with a fresh launch (two Xvfb on the same display, duelling chromiums). Clear
# any leftovers so a relaunch — by systemd, cron, or by hand — starts clean. At a
# normal boot this matches nothing.
pkill -9 chromium 2>/dev/null || true
pkill -f "Xvfb $VDISP" 2>/dev/null || true
pkill -f "venv/bin/python3 main.py" 2>/dev/null || true
pkill -f "kiosk/serve.py" 2>/dev/null || true
sleep 1

mkdir -p "$DATA_DIR" "$WEB"
cp -f "$APP/design/almanac/console_live.html" "$WEB/index.html"
ln -sf "$DATA" "$WEB/wx.json"

pids=()
cleanup(){ kill "${pids[@]}" 2>/dev/null; pkill -9 chromium 2>/dev/null; pkill -f "Xvfb $VDISP" 2>/dev/null; }
trap cleanup EXIT INT TERM

# 1) data engine on a virtual display (invisible).
#    WFP_HEADLESS=1 runs the console's data pipeline with NO GUI panels, so the
#    software GL rasterizer (llvmpipe) has nothing to draw — cuts the engine from
#    ~70% of a core to near-idle. Empty window still needs a display (Xvfb); cap
#    its frame rate low since nothing is shown.
# Each critical process is launched via a function so the watchdog can relaunch it.
launch_xvfb(){
  Xvfb "$VDISP" -screen 0 1024x600x24 -nolisten tcp >/tmp/almanac_xvfb.log 2>&1 &
  XVFB_PID=$!; pids+=("$XVFB_PID")
}
launch_engine(){
  ( cd "$APP" && DISPLAY="$VDISP" WFP_HEADLESS=1 KCFG_GRAPHICS_MAXFPS=10 "$PY" main.py ) >/tmp/almanac_data.log 2>&1 &
  ENGINE_PID=$!; pids+=("$ENGINE_PID")
  ENGINE_GRACE=6                                   # ~90s warmup before the freshness check judges it
}
# 2) local web server (page + live feed + /health endpoint). /health exposes a
#    "polls" counter (wx.json fetches) — our render heartbeat, replacing the
#    access-log grep. Bind 127.0.0.1 by default; WFP_BIND=0.0.0.0 exposes it.
launch_server(){
  ( cd "$WEB" && WFP_PORT="$PORT" WFP_WEB="$WEB" WFP_DATA="$DATA" WFP_BIND="${WFP_BIND:-127.0.0.1}" \
      "$PY" "$APP/design/almanac/kiosk/serve.py" ) >"$HTTP_LOG" 2>&1 &
  SERVE_PID=$!; pids+=("$SERVE_PID")
}

launch_xvfb
sleep 2
launch_engine
launch_server

# wait for first data frame (up to 45s) so the page opens populated
for _ in $(seq 1 45); do [ -s "$DATA" ] && break; sleep 1; done

# stop the screen from blanking (kiosk has no working input)
xset s off -dpms s noblank 2>/dev/null || true

# ── chromium kiosk, with a self-healing watchdog ──────────────────────────────
# Flags stay MINIMAL and use the REAL GPU (default). Do NOT add --disable-gpu
# (software rendering can't composite on VC4 -> no window maps), nor
# --disable-dev-shm-usage / --single-process (they starve renderer IPC).
CR_FLAGS=(--kiosk --ozone-platform=x11 --touch-events=enabled
  --no-first-run --no-default-browser-check --disable-infobars
  --disable-session-crashed-bubble --noerrdialogs --password-store=basic)
URL="http://127.0.0.1:$PORT/index.html?theme=$THEME"

CRPID=""
launch_cr(){
  pkill -9 chromium 2>/dev/null; sleep 2
  rm -rf "$UDD"                                   # fresh profile: no stale SingletonLock
  chromium-browser "${CR_FLAGS[@]}" --user-data-dir="$UDD" "$URL" \
    >/tmp/almanac_chrome.log 2>&1 &
  CRPID=$!
}

# A healthy render means the page's JS is polling wx.json (~every 2s). A blank/
# broken GPU init leaves the renderer unable to run JS -> ZERO new polls. That is
# our screenshot-free, root-free health check.
read_polls(){ curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | sed -n 's/.*"polls": *\([0-9]*\).*/\1/p'; }
polls_growing(){
  local before after
  before=$(read_polls); before=${before:-0}
  sleep 8
  after=$(read_polls); after=${after:-0}
  [ "$after" -gt "$before" ]
}

# launch, and if it came up blank (not polling), wipe and retry
for attempt in 1 2 3 4; do
  launch_cr
  sleep 12
  if polls_growing; then
    echo "kiosk healthy on attempt $attempt" >> /tmp/almanac_chrome.log
    break
  fi
  echo "blank render on attempt $attempt — restarting chromium" >> /tmp/almanac_chrome.log
done

# keep the session alive; relaunch ANY critical process that dies (not just
# chromium — a dead data engine or server used to leave the screen stale forever),
# and every ~5 min re-check for a wedged alive-but-blank render.
CLOG=/tmp/almanac_chrome.log
loops=0; stale_hits=0
while true; do
  pgrep -f "Xvfb $VDISP" >/dev/null 2>&1 || { echo "Xvfb died — relaunching" >> "$CLOG"; launch_xvfb; sleep 2; }
  kill -0 "$ENGINE_PID" 2>/dev/null || { echo "data engine died — relaunching" >> "$CLOG"; launch_engine; }
  kill -0 "$SERVE_PID"  2>/dev/null || { echo "web server died — relaunching"  >> "$CLOG"; launch_server; }
  if ! kill -0 "$CRPID" 2>/dev/null; then
    echo "chromium exited — relaunching" >> "$CLOG"
    launch_cr; sleep 12
  fi

  # DATA FRESHNESS — the engine can be alive-but-wedged (a hung websocket or a
  # stalled emit loop): the PID guard above won't catch that, but the screen goes
  # silently stale. /health reports "stale" once wx.json stops updating (age > 20s).
  # After a fresh engine's warmup grace, restart it if data stays stale two checks
  # running (~30s) — recovering a hang the classic UI would just sit in.
  if [ "${ENGINE_GRACE:-0}" -gt 0 ]; then
    ENGINE_GRACE=$((ENGINE_GRACE - 1)); stale_hits=0
  else
    st=$(curl -s --max-time 4 "http://127.0.0.1:$PORT/health" | sed -n 's/.*"status": *"\([a-z]*\)".*/\1/p')
    if [ "$st" = "stale" ] || [ "$st" = "error" ]; then
      stale_hits=$((stale_hits + 1))
      if [ "$stale_hits" -ge 2 ]; then
        echo "data $st — restarting data engine" >> "$CLOG"
        kill "$ENGINE_PID" 2>/dev/null; sleep 2; launch_engine
        stale_hits=0
      fi
    else
      stale_hits=0
    fi
  fi

  loops=$((loops + 1))
  if [ $((loops % 8)) -eq 0 ]; then                  # ~every 2 min (8 × 15s): alive-but-blank render
    polls_growing || { echo "render wedged — relaunching chromium" >> "$CLOG"; launch_cr; sleep 12; }
  fi
  sleep 15
done
