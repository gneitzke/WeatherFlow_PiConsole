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

APP="${WFP_APP:-/home/garyneitzke/wfpiconsole}"     # console install dir
PY="$APP/venv/bin/python3"
WEB="${WFP_WEB:-$HOME/almanac_web}"                 # served dir (index.html + wx.json)
DATA_DIR="/tmp/wfp_data"; DATA="$DATA_DIR/wx.json"
PORT="${WFP_PORT:-8137}"; VDISP="${WFP_VDISP:-:1}"

# real session env — chromium needs the exact session bus to map a window + touch.
# Force the standard paths (autostart may carry a different/empty value).
export DISPLAY=":0"
export XAUTHORITY="$HOME/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

# COLD-BOOT RACE: the autostart fires before the window manager is ready, so
# chromium launches and its window never maps. Wait for openbox, then a buffer.
for _ in $(seq 1 40); do pgrep -x openbox >/dev/null 2>&1 && break; sleep 1; done
sleep 6

mkdir -p "$DATA_DIR" "$WEB"
cp -f "$APP/design/almanac/console_live.html" "$WEB/index.html"
ln -sf "$DATA" "$WEB/wx.json"

pids=()
cleanup(){ kill "${pids[@]}" 2>/dev/null; pkill -f "Xvfb $VDISP" 2>/dev/null; }
trap cleanup EXIT INT TERM

# 1) data engine on a virtual display (invisible)
Xvfb "$VDISP" -screen 0 1024x600x24 -nolisten tcp >/tmp/almanac_xvfb.log 2>&1 & pids+=($!)
sleep 2
( cd "$APP" && DISPLAY="$VDISP" "$PY" main.py ) >/tmp/almanac_data.log 2>&1 & pids+=($!)

# 2) local web server (page + live feed)
( cd "$WEB" && "$PY" -m http.server "$PORT" --bind 127.0.0.1 ) >/tmp/almanac_http.log 2>&1 & pids+=($!)

# wait for first data frame (up to 45s) so the page opens populated
for _ in $(seq 1 45); do [ -s "$DATA" ] && break; sleep 1; done

# stop the screen from blanking (kiosk has no input; touch is dead)
xset s off -dpms s noblank 2>/dev/null || true

# 3) fullscreen touch kiosk on the real screen.
#    IMPORTANT: do NOT pass --disable-gpu — software rendering cannot composite a
#    window on the Pi's VC4 (Chromium runs but no window ever maps); the real GPU
#    works. Memory headroom for Chromium's multi-process model comes from the 1 GB
#    swap added at /var/swap2. --disable-dev-shm-usage avoids the tiny /dev/shm.
exec chromium-browser --kiosk --ozone-platform=x11 --touch-events=enabled \
  --disable-dev-shm-usage --disk-cache-size=1 \
  --no-first-run --no-default-browser-check --disable-infobars \
  --disable-session-crashed-bubble --noerrdialogs --disable-features=Translate \
  --password-store=basic --user-data-dir=/tmp/almanac_chrome \
  --check-for-update-interval=31536000 \
  "http://127.0.0.1:$PORT/index.html?theme=night"
