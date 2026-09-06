#!/bin/bash
# wifi-keepalive: re-associate the wifi link when the LAN stops answering.
#
# The Pi 4's onboard BCM43455 has been seen holding its association (the
# router still lists the box as online) while ARP and ping are dead, so the
# console vanishes from the LAN until someone power-cycles it. Power save is
# already off (NetworkManager wifi.powersave=2); this is the recovery for the
# case that still slips through. Run from a systemd timer every minute:
#   - ping the default gateway (3 tries)
#   - after FAIL_LIMIT consecutive failures, bounce the wifi device through
#     NetworkManager and log it, so the journal shows every recovery
#
# State lives in /run so a reboot starts clean.
set -u
IFACE="${WIFI_IFACE:-wlan0}"
FAIL_LIMIT="${WIFI_FAIL_LIMIT:-3}"
STATE=/run/wifi-keepalive.fails

gw=$(ip route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [ -z "$gw" ]; then
  # no route at all: NetworkManager is already the one to fix that; count it
  fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
else
  if ping -c 3 -W 2 -I "$IFACE" "$gw" >/dev/null 2>&1; then
    echo 0 > "$STATE"
    exit 0
  fi
  fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
fi
echo "$fails" > "$STATE"

if [ "$fails" -ge "$FAIL_LIMIT" ]; then
  logger -t wifi-keepalive "gateway ${gw:-unknown} unreachable ${fails}x on ${IFACE}; re-associating"
  nmcli device disconnect "$IFACE" >/dev/null 2>&1
  sleep 3
  if ! nmcli device connect "$IFACE" >/dev/null 2>&1; then
    # the device would not re-associate: cycle the radio, which resets the driver
    nmcli radio wifi off; sleep 2; nmcli radio wifi on
  fi
  echo 0 > "$STATE"
fi
