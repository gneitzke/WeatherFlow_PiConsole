# Almanac kiosk — deploy & revert

Shows the almanac HTML overlay (`design/almanac/console_live.html`) fullscreen on
the Pi, fed live by the console. See `almanac-kiosk.sh` for the architecture.

## What runs
- **Data engine** — the console, headless on Xvfb `:1`, with `[Display] LayoutStyle = almanac`
  so `lib/almanac_emit.py` writes `/tmp/wfp_data/wx.json` (~2s). Never on the real screen.
- **Server** — `python3 -m http.server` on `127.0.0.1:8137` serving the page + feed.
- **Display** — `chromium-browser --kiosk` on `:0`, touch on, cursor hidden.

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

# autostart the kiosk in the desktop session
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/almanac-kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Almanac Kiosk
Exec=$HOME/wfpiconsole/design/almanac/kiosk/almanac-kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
```
Then reboot (or log out/in). The overlay comes up fullscreen on boot.

## Verify before trusting it (the one unproven step)
The headless data engine (Kivy on Xvfb) is the part to confirm on-device:
```bash
# after setup, check the feed is being written with real values:
cat /tmp/wfp_data/wx.json          # should show live temp/wind/etc, ts ~now
tail /tmp/almanac_data.log          # console errors?
```
If `wx.json` isn't appearing, the console isn't running headless correctly — check
`/tmp/almanac_xvfb.log` and `/tmp/almanac_data.log`. (Kivy needs Xvfb's virtual GL;
if VC4 offscreen GL is unhappy, add `KIVY_GL_BACKEND=gl` or install `libgl1-mesa-dri`.)

## Revert to the classic console
```bash
rm -f ~/.config/autostart/almanac-kiosk.desktop
pkill -f chromium; pkill -f "Xvfb :1"
sed -i 's/^LayoutStyle = .*/LayoutStyle = classic/' ~/wfpiconsole/wfpiconsole.ini
sudo systemctl enable --now wfpiconsole.service
```
Nothing in the upstream app is modified beyond the additive flag + emitter hook, so
this is a clean switch back.
