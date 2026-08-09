# Almanac kiosk — fresh Pi 4 / Pi 5 setup

This is the from-scratch path for a new Raspberry Pi. The kiosk launcher
(`almanac-kiosk.sh`) runs on **both** display servers and picks the right one at
startup, so you do not have to force the desktop to X11 the way older builds
required.

## Recommended OS

**Raspberry Pi OS (64-bit), Bookworm, Desktop.**

- 64-bit matches the build target and CI (Python 3.11, `aarch64`).
- Desktop (not Lite) ships chromium and a login session, which the kiosk needs.
- Bookworm's Python 3.11 is exactly what `wfpiconsole.sh` builds its venv against.

A fresh Bookworm image on a Pi 4/5 boots a **Wayland** session (labwc or
wayfire). Older installs, and anything set to X11 in `raspi-config`, run
**X11** (LXDE-pi/openbox). The launcher detects which one is live and adapts:

| | X11 session | Wayland session |
|---|---|---|
| Real display | `:0`, gated on openbox + `xset q` | Wayland socket, gated on labwc/wayfire + socket |
| chromium flag | `--ozone-platform=x11` | `--ozone-platform=wayland` |
| Screen blanking | `xset s off -dpms` | compositor config (see below) |
| Headless data engine | `Xvfb :1` (identical on both) | `Xvfb :1` (identical on both) |

Force a backend with `WFP_BACKEND=x11` or `WFP_BACKEND=wayland` if detection ever
guesses wrong. Point at a specific browser with `WFP_CHROMIUM=/usr/bin/chromium`.

## Setup

1. **Flash the image** with Raspberry Pi Imager. In its settings, preset the
   hostname, your user, WiFi, locale, and enable SSH, for a headless first boot.
2. **Update now**, while the card is fresh:
   ```
   sudo apt update && sudo apt full-upgrade -y
   sudo apt install -y xvfb      # the headless data engine needs it; chromium is already present
   ```
3. **Autologin to the desktop** so a session exists at boot:
   `sudo raspi-config` → System Options → Boot / Auto Login → **Desktop Autologin**.
   You do **not** need to switch Wayland→X11; the launcher handles either.
4. **Clone the fork and install:**
   ```
   git clone -b main https://github.com/gneitzke/WeatherFlow_PiConsole.git ~/wfpiconsole
   cd ~/wfpiconsole && ./wfpiconsole.sh install
   ```
5. **Configure** (`wfpiconsole start`, then the wizard). No hardware at this Pi?
   The station picker will offer to search for a nearby public station; you only
   need a free WeatherFlow account and a Personal Access Token.
6. **Enable the kiosk service.** The systemd user service, autostart, and revert
   steps are in [`README.md`](README.md). Set `WFP_BIND=0.0.0.0` in the service to
   view the page from other devices at `http://<hostname>.local:8137`.

## Screen blanking on Wayland

X11 blanking is handled by the launcher (`xset`). Wayland has no `xset`:

- **labwc** ships no idle daemon by default, so the screen does not blank on its
  own; there is nothing to do. chromium `--kiosk` also holds an idle-inhibit.
- **wayfire** enables an idle/DPMS plugin. To stop the wall display from blanking,
  set a zero timeout in `~/.config/wayfire.ini`:
  ```ini
  [idle]
  dpms_timeout = 0
  screensaver_timeout = 0
  ```
  then log out/in (or reboot).

## Why a Pi 4/5 over the old Pi 3

- Wired Ethernet. Avoids the dead-onboard-radio / flaky-dongle trouble the Pi 3
  had, which matters for a headless wall appliance you manage over SSH.
- The cores and RAM the headless engine + chromium actually want. 2 GB is enough,
  4 GB comfortable.
- The cold-boot white-screen guard and self-healing watchdog in the launcher are
  GPU-agnostic, so they carry over unchanged.

## Verifying the backend it chose

After the service is up:
```
grep -E "healthy|attempt|backend" /tmp/almanac_chrome.log
echo "$XDG_SESSION_TYPE"          # x11 or wayland — what the desktop is running
pgrep -a -x 'labwc|wayfire|openbox'
```
`/health` should report growing `polls` (the render heartbeat):
```
curl -s http://127.0.0.1:8137/health
```
