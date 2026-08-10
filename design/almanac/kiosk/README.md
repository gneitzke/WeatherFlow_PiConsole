# Almanac kiosk: deploy, manage, revert

Shows the almanac HTML overlay (`design/almanac/console_live.html`) fullscreen on
the Pi, fed live by the console. See `almanac-kiosk.sh` for the architecture.

> **Starting from a blank card on a Pi 4 or Pi 5?** Follow [`PI4-SETUP.md`](PI4-SETUP.md)
> first (OS choice, install, first-boot). This page is the deploy/manage/revert
> reference for a Pi that already has the console installed.

The launcher auto-detects the display server, so it runs on both an X11 desktop and
a Wayland one (a fresh Bookworm Pi 4/5 boots Wayland). Set `WFP_BACKEND=x11|wayland`
to override.

## What runs
- **Data engine**: the console, headless on Xvfb `:1`, with `[Display] LayoutStyle = almanac`
  so `lib/almanac_emit.py` writes `/tmp/wfp_data/wx.json` (~2s). Never on the real screen.
- **Server**: `serve.py` on `127.0.0.1:8137` serving the page + feed + a `/health` endpoint.
- **Display**: `chromium --kiosk` on the real screen (X11 `:0` or the Wayland socket), touch on, cursor hidden.
- **Watchdog**: `almanac-kiosk.sh` supervises all of the above: it relaunches any that
  die, restarts the engine if the feed goes stale (a hung websocket the classic UI would
  sit in), and re-checks for a blank/wedged render every ~2 min.

The kiosk **replaces** the on-screen Kivy console, so we stop its service and run this.

## One-time setup (run at the Pi as your desktop login user)
```bash
sudo apt-get install -y xvfb                       # only new dependency
# emitter must run -> almanac layout in the ini:
sed -i 's/^LayoutStyle = .*/LayoutStyle = almanac/' ~/wfpiconsole/wfpiconsole.ini \
  || sed -i '/^\[Display\]/a LayoutStyle = almanac' ~/wfpiconsole/wfpiconsole.ini
chmod +x ~/wfpiconsole/design/almanac/kiosk/almanac-kiosk.sh

# stop the on-screen Kivy console (frees :0 for chromium)
sudo systemctl disable --now wfpiconsole.service

# supervise the kiosk with systemd so the whole chain always comes back
mkdir -p ~/.config/systemd/user
cp ~/wfpiconsole/design/almanac/kiosk/almanac-kiosk.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now almanac-kiosk.service
sudo loginctl enable-linger "$USER"                # start at boot without an interactive login

# belt-and-suspenders: a 1-min cron check that restarts the service if ever inactive
( crontab -l 2>/dev/null | grep -v almanac-kiosk.service; \
  echo '* * * * * /bin/sh -c "export XDG_RUNTIME_DIR=/run/user/$(id -u); systemctl --user is-active --quiet almanac-kiosk.service || systemctl --user start almanac-kiosk.service"' ) | crontab -
```
The overlay comes up fullscreen on boot. `almanac-kiosk.service` is `Restart=always`, so a
crashed watchdog is brought back immediately; the cron check is a second layer behind it.

> Migrating from the older autostart method? Remove the stale entry so it can't double-launch:
> `rm -f ~/.config/autostart/almanac-kiosk.desktop*`

## Manage
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status  almanac-kiosk.service     # state + last logs
systemctl --user restart almanac-kiosk.service     # after an emitter/Python change
journalctl --user -u almanac-kiosk -f              # follow logs (also /tmp/almanac_*.log)
curl -s 127.0.0.1:8137/health                      # {status, dataAgeSec, polls, station, temp}
```
An **HTML-only** change just needs the page reloaded (`pkill -9 chromium`; the watchdog
relaunches it in ~15s). An **emitter/Python** change needs the engine restarted; the
simplest way is `systemctl --user restart almanac-kiosk.service` (or a reboot).

## Verify before trusting it
```bash
cat /tmp/wfp_data/wx.json           # live temp/wind/etc, ts ~now
curl -s 127.0.0.1:8137/health       # status:"ok", dataAgeSec small, polls climbing
```
If `wx.json` isn't appearing, the console isn't running headless correctly. Check
`/tmp/almanac_xvfb.log` and `/tmp/almanac_data.log`. (Kivy needs Xvfb's virtual GL;
if VC4 offscreen GL is unhappy, add `KIVY_GL_BACKEND=gl` or install `libgl1-mesa-dri`.)

## View the live page from another device

The overlay is a served web page, so anything on your network can view the exact
same live console in a browser, with nothing installed on that device. The
service already sets `WFP_BIND=0.0.0.0`; from a laptop, phone, or tablet open:
```
http://<hostname>.local:8137
```
Every gauge and the 2s updates render client-side, so the Pi only ships the JSON
feed. Binding `0.0.0.0` also makes `wx.json` LAN-readable; leave it at the default
`127.0.0.1` if you only ever view on the Pi itself.

## Run headless: no local screen, view from a laptop

For a box with no monitor (a headless Pi, a spare server), run the launcher in
**headless mode**: `WFP_MODE=headless` starts the data engine + web server with
no chromium and no display gate, so it works with nothing plugged into the video
out. The same watchdog still supervises the engine and server (including a
restart if the feed goes stale), because that half never depended on a screen.

One-time setup, then supervise it with the ready-made service:
```bash
cd ~/wfpiconsole
sudo apt-get install -y xvfb                        # headless Kivy still needs a virtual display
# almanac layout so the emitter writes wx.json:
sed -i 's/^LayoutStyle = .*/LayoutStyle = almanac/' wfpiconsole.ini \
  || sed -i '/^\[Display\]/a LayoutStyle = almanac' wfpiconsole.ini

mkdir -p ~/.config/systemd/user
cp design/almanac/kiosk/almanac-headless.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now almanac-headless.service
sudo loginctl enable-linger "$USER"                 # start at boot without an interactive login
```
Then browse `http://<hostname>.local:8137` from your laptop, and check
`curl -s <hostname>.local:8137/health` (status `ok`, `dataAgeSec` small). The
service sets `WFP_BIND=0.0.0.0`. Do not run it alongside `almanac-kiosk.service`;
both drive the same engine, `Xvfb :1`, and port.

To try it in the foreground first (no service), just run the launcher with the
flag: `WFP_MODE=headless WFP_BIND=0.0.0.0 ./design/almanac/kiosk/almanac-kiosk.sh`.

## Run it on a laptop or PC instead of a Pi

This isn't Pi-only. The installer supports any Ubuntu 22.04+ or Raspberry Pi OS
Desktop machine, `amd64` or `arm64`; see the [PC / Laptop](../../../README.md#pc--laptop)
note in the main README. (macOS and Windows aren't covered, since the installer is
`apt-get` based.)

Install the same way (`./wfpiconsole.sh install`), then either:
- **Classic GUI:** `wfpiconsole start` opens the console in a desktop window.
- **Almanac:** set `LayoutStyle = almanac`, run the headless engine + server as
  above, and open `http://127.0.0.1:8137` in the laptop's own browser. This is also
  the quickest way to preview HTML changes without touching the Pi.

## Revert to the classic console
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user disable --now almanac-kiosk.service
crontab -l 2>/dev/null | grep -v almanac-kiosk.service | crontab -
pkill -f chromium; pkill -f "Xvfb :1"
sed -i 's/^LayoutStyle = .*/LayoutStyle = classic/' ~/wfpiconsole/wfpiconsole.ini
sudo systemctl enable --now wfpiconsole.service
```
Nothing in the upstream app is modified beyond the additive flag + emitter hook, so
this is a clean switch back.
