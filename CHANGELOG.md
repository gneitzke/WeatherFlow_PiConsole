# Changelog

Changes in this fork's Almanac work, newest first. The upstream WeatherFlow
PiConsole keeps its own release notes; entries under **Core** below are fixes
to shared upstream code that the classic console benefits from too.

## 2026-09-06

Two independent audits of the fork (an initial one, then an adversarial review
of the fixes) drove this release. The full findings and review are in
`design/almanac/AUDIT-2026-09-06.md`.

### Console
- Freshness is measured at the source. The masthead mark now reads **STALE** when
  nothing new is reaching the screen and **SILENT** when frames arrive but the
  station has stopped reporting (`obsAgeSec`, from the observation's own epoch).
  Polling is single-flight with a 4 s deadline; a hung request flags STALE
  within about 12 s instead of never.
- The hero forecast curve's forward segment follows real hourly forecast
  temperatures, with the high and low labelled at the hours they occur. It no
  longer draws a rise back to a high that already happened.
- Lightning takes the Sun & Sky slot while strikes are active; the rainfall
  panel stays visible in a storm.
- Rain volume slews toward the measured rate (columns count up fast, down slow)
  and rain fades in and out instead of switching.
- Light rain never reads as dry: a 10-minute rolling window bridges the haptic
  sensor's zero minutes between trace readings.
- AQI marker and colour bands share one scale; inHg keeps two decimals; the
  barograph plots samples by their epoch on the producer's 24 h window; long AQI
  text ellipsizes; the lightning tile follows the distance unit.
- Barometer outlook uses a compact vocabulary that fits its row; the band's
  TODAY hi/lo come from the same provider as the hero's LOW/HIGH.

### Data engine
- Non-finite numbers (NaN/inf, including formatted strings) can no longer stop
  the JSON feed.
- Every scheduled timer is registered and cancelled by `stop()`; each provider
  gets one in-flight fetch and one retry chain (a day of outage used to
  accumulate 25 retry chains). Provider results publish as one immutable
  snapshot.
- Cached NWS alerts are re-expired every emit; tomorrow's hint and the AQI
  daily trend are chosen by calendar date, never by array position.
- Payload carries `obsTs`/`obsAgeSec`, `lightningTs`, per-provider fetch ages,
  `fcHourly`, `dayStartTs`, and per-day forecast precipitation (`qpf`).
- `/health` reports `degraded` ("sensor silent") distinct from `stale` ("engine
  stalled"), and counts frames the kiosk confirmed painting.

### Core (shared with the classic console)
- WeatherFlow's websocket delivers every `obs_st` twice; the duplicate guard
  compared a list to a number and never fired, so per-minute integrators (peak
  sun, strike counts) double-counted. Fixed.
- Tempest daily-bucket rain columns were swapped: month and year were seeded
  from the rain-check corrected figure while today/yesterday used the raw
  sensor. With `nc_rain` off the year total now matches the device (34.5 in,
  not 42.5 in).
- REST seeds (daily wind average, gust max, yesterday's rain) retry every five
  minutes while missing instead of only once at boot; an echoed message no
  longer wipes the REST cache.
- Yearly rain rollover no longer doubles its baseline for one observation;
  lightning frequency averages over real coverage; sunrise/sunset use station
  midnight in explicit UTC, with polar days handled; statistics rows are
  selected by date and must carry a finite number; short websocket rows are
  shape-checked.

### Kiosk
- Launcher waits for the declared display backend (a Wayland session never
  falls back to X11), bounds every health probe, reads the 503 stale body so a
  wedged engine restarts the engine rather than the server, exits cleanly on
  TERM with children reaped, and restarts the engine once per silent-sensor
  episode.
- Pi 4: `wifi-keepalive` timer re-associates when the gateway stops answering;
  persistent journal so the next drop leaves a log. See `kiosk/PI4-SETUP.md`.

## 2026-09-01

- Wind-driven rain and blowing snow glyphs; tomorrow hint under the headline;
  MAX gust row on the wind panel; forecast-day rain amounts (from drench44).
- Rolling rain window; rain fade; rain volume slew.
- Barograph in the station's pressure unit; outlook vocabulary compacted.

## 2026-08-29 to 2026-08-31

- 7-day outlook band on one shared temperature scale, condition glyphs, snow-aware
  status, alerts and outlook coexisting, etched two-depth rain with a waving
  water surface, intensity-scaled rain gauge.
- Forecast fetch retries until first success; fit-and-finish audit fixes.

## 2026-08-03 to 2026-08-10

- NWS weather alerts and AQI (WAQI) with severity forecast; alert rotation.
- Systemd-supervised kiosk with stale-data recovery; headless mode; LAN view;
  X11/Wayland auto-detect; Pi 4 setup guide; public-station picker for
  hardware-less setup; offline test suite and CI.
