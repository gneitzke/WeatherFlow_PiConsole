# Almanac UX + headless core — architecture & upstream notes

This change set does two independent, additive things. **Neither alters default
behaviour**: with a stock config (`[Display] LayoutStyle = classic`, no
`WFP_HEADLESS`), the console runs exactly as before.

## 1. A headless data-engine mode for the core (`WFP_HEADLESS=1`)

Running the console on a display-less host (e.g. Xvfb, for a data feed) still
software-renders the full Kivy GUI via `llvmpipe` — ~70 % of a core drawing
frames nobody sees. `WFP_HEADLESS=1` takes an early branch in `App.build()`
(`build_headless()`) that runs the **exact same data pipeline** — websocket/UDP
ingestion → `obs_parser` → derivations → `station`/`astro`/`forecast`/`sager` —
via a Kivy-free data holder (`HeadlessConditions`, an `EventDispatcher` with the
same `Obs/Astro/Met/Sager/System/Status` DictProperties as `CurrentConditions`
but **no panels**). Nothing is rendered, so CPU drops to just the real data work
(~72 % → ~25 % on a Pi 3).

This is useful to anyone who wants the console as a **data service** (feed Home
Assistant, MQTT, a web dashboard, etc.) without a GUI.

* Panel updates in `obs_parser`/`astro`/`forecast` are already `hasattr`-guarded,
  so they are simply skipped when no panels exist.
* The data pipeline is wired in **one** function, `start_conditions_core(cc)`,
  called by both the classic Kivy screen (`CurrentConditions`) and the headless
  holder (`HeadlessConditions`). It is the single source of truth for the core —
  a core change (new service, different schedule) reaches every UI at once.
  Presentation (panels, kv, animation) lives entirely in the UI layer on top.

## 2. An almanac UX that consumes the core

The same weather data can be presented three ways, all off the one core:

| UI | How to enable | Renders |
|----|---------------|---------|
| **Classic Kivy console** (default) | `LayoutStyle = classic` | native Kivy, 6 panels |
| **Native Kivy almanac** | `LayoutStyle = almanac` | native Kivy, single almanac screen (`panels/almanac.py`) |
| **HTML almanac overlay** | run the kiosk (below) | `chromium --kiosk` over the headless core |

The **HTML overlay** is the recommended low-power kiosk: the console runs
`WFP_HEADLESS=1`, `lib/almanac_emit.py` writes `wx.json` (see `DATA_CONTRACT.md`),
a tiny server (`design/almanac/kiosk/serve.py`) serves the page + `/health`, and
`chromium --kiosk` shows `console_live.html`. Full deploy/revert in
`design/almanac/kiosk/README.md`.

`/health` → `{status, dataAgeSec, station, temp, updateAvailable}` (200 fresh,
503 stale) for monitoring; binds `127.0.0.1` by default, `WFP_BIND=0.0.0.0` to
expose it.

## Files

**Modified (upstream, minimal + opt-in):**
* `main.py` — headless branch + `build_headless()` + `HeadlessConditions`; lazy
  `LayoutStyle=almanac` layout selection.
* `lib/config.py` — one new default: `[Display] LayoutStyle = classic`.
* `wfpiconsole.kv` — `#:include kvlang/almanac.kv` (only needed for the native
  Kivy almanac layout).

**Added (self-contained):**
* `lib/almanac_emit.py` — the `wx.json` emitter (data output for external UIs).
* `panels/almanac.py`, `kvlang/almanac.kv` — the native Kivy almanac layout.
* `design/almanac/` — the HTML overlay (`console_live.html`), sample prototype
  (`console.html`, Seattle sample data), kiosk scripts, and docs.

## Suggested split if landing upstream in pieces

1. **Core:** `WFP_HEADLESS` headless mode (+ the shared-wiring refactor).
2. **HTML UX:** emitter + `design/almanac/` overlay + kiosk + `/health`.
3. **Native Kivy almanac:** `LayoutStyle=almanac` + `panels/almanac.py` +
   `kvlang/almanac.kv` (optional; renders via software GL on GPU-less Pis).
