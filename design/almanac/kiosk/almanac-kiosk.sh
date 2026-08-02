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

mkdir -p "$DATA_DIR" "$WEB"
cp -f "$APP/design/almanac/console_live.html" "$WEB/index.html"
ln -sf "$DATA" "$WEB/wx.json"

pids=()
cleanup(){ kill "${pids[@]}" 2>/dev/null; pkill -9 chromium 2>/dev/null; pkill -f "Xvfb $VDISP" 2>/dev/null; }
trap cleanup EXIT INT TERM

# 1) data engine on a virtual display (invisible)
Xvfb "$VDISP" -screen 0 1024x600x24 -nolisten tcp >/tmp/almanac_xvfb.log 2>&1 & pids+=($!)
sleep 2
( cd "$APP" && DISPLAY="$VDISP" "$PY" main.py ) >/tmp/almanac_data.log 2>&1 & pids+=($!)

# 2) local web server (page + live feed). Its access log is our render health signal.
( cd "$WEB" && "$PY" -m http.server "$PORT" --bind 127.0.0.1 ) >"$HTTP_LOG" 2>&1 & pids+=($!)

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
polls_growing(){
  local before after
  before=$(grep -c "wx.json" "$HTTP_LOG" 2>/dev/null); before=${before:-0}
  sleep 8
  after=$(grep -c "wx.json" "$HTTP_LOG" 2>/dev/null); after=${after:-0}
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

# keep the session alive; relaunch if chromium ever dies
while true; do
  if ! kill -0 "$CRPID" 2>/dev/null; then
    echo "chromium exited — relaunching" >> /tmp/almanac_chrome.log
    launch_cr; sleep 12
  fi
  sleep 15
done
