# Almanac UX — Kivy integration plan

An alternate "almanac" display style for WeatherFlow PiConsole, built **natively in
Kivy** (the app's existing framework) and gated behind a feature flag so existing
installs are untouched until the user opts in.

`console.html` in this folder is the **design spec only** — a self-contained
HTML/CSS/JS reference (sample data for **Seattle, WA**, no personal station data)
that the Kivy implementation targets visually. Open it in a browser; footer tabs
switch screens; `?theme=night` / `?sim=1` demo the themes and the live-lightning
behavior. It does **not** ship as a runtime — it's the picture we build to.

## Approach

Implement the almanac UX as a **native Kivy layout** using the existing framework:
`kvlang/*.kv` + `panels/*.py`, driven by the app's existing reactive data
(`lib/observation_parser.py`, `derived_variables.py`, `astronomical.py`,
`sager.py`). No new runtime, no Chromium, no data bridge — the widgets bind to the
same app properties the classic panels already use.

The almanac is a cohesive full-screen composition (masthead + temperature hero +
instrument grid + footer tabs), so it's a **whole-screen alternate layout**
selected by the flag, not individual panels swapped into the classic 6-panel grid.

## Feature flag

Add `[Display] LayoutStyle` (`classic` | `almanac`), default **`classic`**, via the
existing config-migration path in `lib/config.py`
(`default_config_file()` + `update_required()`), so **existing `.ini` files keep
`classic` on upgrade** — no forced change. The screen builder in `main.py` branches
on this flag to load the classic `CurrentConditions` layout or the new almanac one.

## Upgrade prompt

On version upgrade, mirror the existing update popup
(`lib/system.py::check_version` → `panels/update.py`) to offer "Try the new Almanac
layout?" — accepting flips `LayoutStyle` to `almanac`.

## Build map (Kivy)

- **Layout:** `kvlang/almanac.kv` — masthead, temperature hero, 2×2 instrument grid,
  footer tab bar; a matching `panels/almanac.py` (or reuse `CurrentConditions` with a
  variant) wired into `main.py`'s layout selection.
- **Instruments (Kivy `canvas` drawing):** compass rose, aneroid barometer dial,
  sun arc, temperature day-curve sparkline, rain-rate tube, UV bar, mini range-rings.
  These are Line/Ellipse/Mesh canvas instructions, redrawn on data update.
- **Theming:** paper/night token sets as app-level color properties; a serif display
  font asset in `fonts/` for the almanac figures (classic layout unaffected).
- **Live lightning:** reuse `panels/lightning.py`'s data; surface the dynamic
  tile + masthead flag on the almanac screen, decaying on the existing timeout.
  **Distance only — the sensor has no bearing** (strikes are rings, not points).

### Data the UI binds to
temp (current/feels-like/trend/24h Δ), today observed low+high w/ times, forecast
low/high, humidity, dew point, conditions + "clear until", next-hour forecast,
wind (avg/gust/max/dir°/cardinal), barometer (SLP/trend/24h hi-lo/outlook), rain
(today/yest/month/year/rate + dry-spell), sun (radiation/UV/rise/set/daylight/peak),
moon (phase/illum/rise-set/next full-new), lightning (distance/time-since/counts),
Sager code+text, clock/date. All already computed by the existing lib modules.

## Phases
1. ✅ Branch + PII-scrubbed prototype (design spec) + this plan.
2. Flag plumbing in `lib/config.py` (default `classic`, inert) + layout branch in `main.py`.
3. Almanac `.kv` layout + static panels (masthead, hero, grid, footer) wired to live data.
4. Canvas instruments (gauges, sparkline).
5. Live lightning dynamic tile + upgrade prompt.
6. Verify on the Pi under the `almanac` flag; classic path unchanged.
