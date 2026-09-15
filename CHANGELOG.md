# Changelog

Changes in this fork's Almanac work, newest first. The upstream WeatherFlow
PiConsole keeps its own release notes; entries under **Core** below are fixes
to shared upstream code that the classic console benefits from too.

## 2026-09-15

### Radar
- **Local failures back off.** A dead route or resolver is never the provider's
  fault, so it never opens a host breaker, and each failed pass retried after a
  flat two seconds for as long as the network stayed down. Consecutive local
  failures now double the retry from 2 s up to 60 s; any other outcome or a
  completed pass resets it. Ambiguous stalls on a reused socket keep the
  two-second retry, and the fallback chain is unchanged.
- **Radar v6.0 carries closest-site evidence into the picker.** `nexrad` adds
  `reporting`, `newestTs`, `ageSec`, `reason`, `checkedTs`, `checkedAt`,
  `nextCheckTs` and `nextCheckAt` from the last listing and existing discovery
  schedule. Region checks the closest eligible site on that same wakeup, within
  the shared deadline/budget and footprint-aware reserve (at least 34 requests).
  Listing results are shared across the pass; tile failure cannot erase the last check.
  Empty-listing site taps are refused immediately, with `refresh.reason:
  "not reporting"` and `sourceFallback:"site-not-reporting"`, while Region
  continues updating. The site remains tappable and names its last check or
  measured scan age. The note uses the scheduled check time; Region caption
  copy stays unchanged. Unknown/failed listings never claim an outage start.
- **Zoom notes separate retained playback from incoming acquisition.** While the
  previous view loops, the note says “Playing previous view · sharpening N of M,”
  counting only incoming decoded composites. A coincident dirty-camera paint and
  playback deadline now paint the successor once. Local Chromium counters found
  a maximum of two echo paints per RAF before, one after; native decode admission
  remains at most one per RAF. With eight scans actively looping, renderer task time was 92–122 ms per
  0.6-second step before and 99–116 ms after, without a material CPU reduction.
  Engine, both-theme state-machine and v5.9 retained-window checks cover the
  change; `tools/benchmark_radar_v60.py` records reproducible local counters.
- **Radar v5.6 adds durable Smooth.** A quiet 44px-target toggle beside zoom reset
  defaults off and persists through the loopback-only `radar_smooth` marker.
  Smooth upsamples native reflectivity 2× with valid-coverage bilinear weights,
  then applies the legend LUT; missing/below-floor gates never supply intensity.
  Echo scaling follows the selected variant. Both rendered revisions share the
  existing disk cap, and browser accounting includes larger tiles while preserving
  visible geography and the 40 MiB cap. Native byte reuse and warming tiers remain
  unchanged. Offline field/persistence tests and both-theme browser checks cover
  the toggle; an offline benchmark reports per-tile CPU cost.
- **Radar v5.9 keeps zoom/pan playback alive.** Camera settle and manifest geometry
  changes retain and reproject the playing cycle until four replacement composites
  decode, then adopt at the existing wrap. The frame read keeps its timestamp;
  the corner note reports acquisition. Decode admission is serialized to one per
  animation frame after paints. Incoming history pauses at four until adoption,
  with oldest-first outgoing release and the existing 40 MiB admission cap.
- **Stalled bodies get warm hedges.** Two seconds without received-byte progress
  now races headers/body stalls as well as silent first bytes. Every received
  chunk rearms inactivity; `stallHedges` identifies the progressed-response subset.
  Connection, request, attempt and absolute deadline limits remain unchanged.
  Real TLS regressions barrier the body stall and drive hedge time explicitly.
- **Radar v5.4: greener light rain and measured site operating copy.** The first
  three reflectivity bands now use Fable v5.2 greens; the remaining ramp, clear-air
  slate, alpha mapping and legend geometry stay unchanged. Legend and remap
  revisions invalidate previous rendered tiles. Contrast is computed across all
  26 LUT entries for both themes. Site captions use the primary listing's median
  of the latest three gaps, infer precipitation/clear-air mode only outside the
  ambiguous range, and reserve “scanning slowly” for intervals over 15 minutes.
  Caption fitting drops the interval before the inferred mode.
- **Warm hedge admission recovers when a lease returns.** A denied hedge check
  no longer disables hedging for the rest of a tile race. Bounded 50ms checks
  reuse the original deadline, request cap and sole second attempt. A scripted
  clock regression reproduces the missing tile on the old race and delivers all
  ten tiles with healthy second attempts; real loopback TLS coverage remains.
- **Radar v5.8 removes cache inventory from the worker's filesystem path.** A
  bounded startup worker validates the disk cache once; writes, evictions and
  explicit page damage reports maintain its memory index. Warm hits skip PNG
  decode/read-back and executor work. Scan/level masks and site coverage are
  reused and pressure eviction uses indexed records. Geography raster palette
  composition now uses Pillow operations
  with identical output pixels. JSON publication remains on its two-second tick.
- **Ordinary tile retries retain their second attempt.** Each attempt may use
  six seconds within the unchanged batch/pass deadline; committed-camera tiles
  retain their two-second total deadline and warm-only hedges. No-warm-lease
  timeout/failure and synchronized warm-hedge regressions cover both policies.
- Local before/after worker profiles, cache-I/O counting tests, bounded startup
  measurements and the real TLS/page switch harness cover the v5.8 root fix.
  The three-site warm switch and cold 15-tile Region newest both reach eight
  decoded frames in both themes; no Pi or LAN access is needed by these checks.
- **Radar v5.7 makes intent ordered and switch completion bounded.** Session,
  generation and heartbeat fencing prevent delayed requests or old failure
  payloads from replacing a newer choice. Reload reconciles accepted intent;
  Auto survives durable restart. Pending captions paint in the input frame.
  Healthy switches/zoom with rate capacity must decode four frames and advance
  playback within 20 seconds; expiry visibly retries while keeping old imagery.
- **Mandatory visible work comes first:** newest → four → eight frames, then
  margin/opposite-source/adjacent-zoom warming, then deep history. Pending work
  survives discovery and busy-worker wakeups. Reserves count actual missing
  tiles and site layers; camera footprint controls capped-source coverage.
- **Both sides now bound acquisition.** Mandatory camera tiles get two seconds
  for at most two attempts; browser headers/body/decode get 2.5 seconds and
  cancellation, with a three-second compositor progress escape. DNS/local and
  ambiguous reused-path errors do not blame providers. Acquisition-objective
  failures qualify fallback, and failed fallback can escape recovery dwell.
- **Publication and long sessions retain honest state.** Panned regression
  guards preserve newer history; captions age the painted frame. Bounded caches,
  process-wide resolver ownership and exact visible-loop disk pins prevent
  accumulated work. Publication uses inventory membership rather than per-tile
  filesystem probes (v5.8 extends this ownership across passes). Phase/request telemetry and deterministic plus real TLS
  loopback acceptance cover both themes, budget pressure and 30% hangs.

### Radar v5.3 baseline (superseded where v5.7 differs)
- **Radar v5.3 uses one settled camera intent.** Activity reports supply runtime
  zoom and centre; durable zoom follows after a debounce and is used only for
  cold start. Stepper, pinch and restored cameras share this path. Provider zoom
  caps select native tile resolution without moving the camera.
- **Connection setup is bounded per host.** Hedges reserve warm pooled sockets
  and cannot open connections. At most two connections perform TCP/TLS setup at
  once; shared TLS contexts reuse session tickets. Client setup/pool timeouts and
  cancelled attempts do not lower host health. Losing more than half of issued
  hedges in 60 seconds suspends hedging for five minutes. A tile retains its
  two-attempt, six-second total budget, including sequential stale-socket recovery.
- **Source transitions retain the displayed loop.** Three consecutive provider
  failures are required for fallback; local failures cannot trigger it. A new
  source builds at least four complete frames before publication, and recovery
  waits for a five-minute dwell. Every source switch logs its reason.

## 2026-09-14

### Radar
- **Radar v5.1 keeps the closest-site control stable.** The site segment and its
  accessible name stay on `nexrad.id` when that site is dark, with the existing
  drawn-contributor count. Captions retain the drawn site's name and outage
  suffix. “High resolution” appears in the accessible name and, when it fits,
  the caption; it yields before the existing distance/cadence/nearby drop order.
  Health counters now separate overlapping two-second hedges from second
  attempts after failure; a hedge no longer increments retries. Local unit and
  both-theme browser/picker regressions cover identity, copy, fitting and counters.
- **Radar v5.0 discovers scans at expected readiness.** A cancellable one-shot
  replaces the unaligned 180-second poll. MRMS predicts the next stamp plus a
  300-second provider lag, then re-polls every 20 seconds; site listings predict
  the next volume from recent spacing and re-poll every 30 seconds. Six fast
  attempts are bounded by 120-second backoff, host breakers and the shared
  240/minute budget. Unchanged discovery ends after metadata/listings. New scans
  retain the sliding manifest and wrap playback, reserve, partial repair and
  prefetch tiers. The lag predicts discovery without suppressing early scans;
  newest archive misses expire within a re-poll. Health/INFO expose expected
  readiness, next/last poll, bounded polling state and live age. Deterministic
  scheduler tests and a five-stamp, both-theme loopback TLS/browser harness cover
  publication jitter and the warm-path fetchability-to-screen/age targets.
- **Radar v4.9 isolates request failures from the displayed window.** Newest
  tiles hedge after two seconds without a response byte, using fresh connections
  and three reserved rescue leases. Each tile gets at most two six-second
  attempts; admitted hedges/retries share the unchanged 240/minute gate and a
  per-pass hedge cap. Failed tiles stay local, partial newest inventory publishes,
  and its repair precedes history/warming. A 60-second host health window opens
  a 30-second circuit below 50% success with six samples, then admits one recovery
  probe. Source acquisition gets 16 seconds inside the existing 25-second pass,
  with immediate eligible fallback. The artificial five-minute MRMS readiness
  delay is removed: acquisition starts at the advertised scan. `/health.radar`
  and per-pass INFO expose success, hedges, retries, breakers and last error.
  Existing failure copy appears only after a failed pass with newest older than
  two cadences. Fake-origin, real loopback TLS and both-theme browser scenarios
  cover recovery, deadlines, partial repair, rate gates and retained playback.
- **Radar v4.8 bounds silently dead pooled sockets.** Reused connections wait at
  most three seconds for the first response byte, then retry a zero-byte
  GET/HEAD failure once on a fresh connection. Partial responses and fresh hangs
  are never replayed. One absolute 25-second budget now spans the whole pass,
  including pooled workers, DNS waits, TLS, headers/body, retries and history;
  trickled reads cannot restart the timeout. Idle reuse defaults to two seconds
  (or the shorter advertised Keep-Alive), with guarded TCP keepalive settings
  of 2/2/2 seconds/seconds/probes on Linux. INFO telemetry separately counts
  stale-first-byte retries; successful recovery keeps an honest idle refresh
  state. Real loopback HTTPS tests cover six hanging reused sockets recovering
  on fresh connections in about three seconds and bounded failure when retries
  also hang. No panel or mesh timing claim is made.
- **Radar v4.7 keeps the manifest sliding during a new scan.** At unchanged
  source, geometry and legend, the first pending-newest publish retains the
  previous hour's frames and complete counts. Slow newest tiles cannot replace
  the window with one frame; completion updates that scan in place. Site history
  keeps its original per-site scan identities. Cold starts and new geometry keep
  their existing first-measurement behavior. The page independently retains
  omitted in-hour frames across truncated manifests, preserving composites,
  playback deadlines and scan-time reads; true identity changes release them.
  Mocked engine tests and both-theme loopback checks cover a 25-second newest
  fetch, retained bitmaps, zero old-scan fetches and adoption at the v4.6 wrap.
- **Radar v4.6 slides the window at the wrap.** Manifest updates keep the running
  scan, 120ms crossfade and deadline intact. After the old-newest 1100ms hold,
  playback wraps to the slid window's oldest decoded scan and reaches the new
  newest at the existing 350ms cadence. Decoded frames on both sides of a gap
  count; late frames join at the next wrap. Playback now enforces the four-scan
  start shown by the buffering read. Paused updates preserve the displayed scan;
  cold acquisition shows newest immediately and AS OF follows the manifest.
  Stable frame identities retain composites; aged-out bitmaps close after their
  last display/blend use. Native acquisition order and cache reuse are preserved.
  Deterministic both-theme checks cover wraps, zero resident-native refetches,
  late decode, paused/cold states, reads and reduced-motion single sweeps.
- **The loop breathes: 350 ms between scans, and the read tells the truth.** After 200 ms
  the user asked for a longer pause; scans now advance every 350 ms (newest hold and
  crossfade unchanged, hard cuts under reduced motion). The inventory read measured
  itself against a hard eight and said "Buffering · 6 of 8" forever on a ten-minute
  site with six scans an hour, while the loop was in fact running; it now counts the
  scans that exist and the loop's own start of four.
- **Radar v4.5 removes the hatch entirely (user decision, superseding P4/v4.4).**
  Missing tiles draw nothing; same-stamp parent tiles still fill in while finer
  tiles load. The drawing pattern, hatch token use, 400ms timer, coverage/40%
  gates and partial-coverage aria suffix are deleted. Incomplete inventory
  reads `Buffering · N of 8` (or `Paused · N of 8`); the loop read and refresh
  note carry acquisition state. Empty regions may mean not yet loaded.
  Both-theme regression checks assert no hatch elements or pixels.
- **Radar v4.4 keeps hatch to a few real holes.** The 400ms hatch requires the
  manifest's expected bit and drawn site-range/MRMS coverage, clips at coverage
  edges, and disappears when more than 40% of expected cells are missing.
  Acquiring views use the loop/refresh copy. The reporting nearest radar keeps
  its subject while tiles are late, with `KATX loading` instead of `timeline KATX`.
- **Smoother local playback.** Frames advance every 200ms with the existing
  1100ms newest hold and a 120ms linear blend of two real scans. RainViewer's
  interval formula scales by 200/110. Nearest-neighbour sampling stays; reduced
  motion hard-cuts through its opt-in single sweep. Eight cached composites fit
  the existing 40MiB cap; playback fetches and decodes nothing.
- **Radar v4.3b explains the source choice.** Region replaces Mosaic; the live
  callsign keeps its nearby count, with descriptive accessible names on both
  segments. Captions name the radar and new-image cadence, keep visible provider
  credit, and attach station distance only to the nearest drawn primary. The
  grey-band explanation now sits under the legend. Overflow drops optional
  detail in order and clips within the caption's box.
- **Switch copy follows the picture.** The displayed source stays named while
  pending; `switching` appears after the existing 600ms grace. A refused choice
  uses `Couldn't switch · showing …` in the existing note. Immediate intent,
  pending treatment, bounded fast polling and warm-cache behavior remain.
- **Radar v4.3 immediate source choice.** Touch contact, click and keyboard show
  a dotted pending choice immediately (caption copy refined in v4.3b above). The displayed
  source retains `aria-pressed`; the group exposes busy state. Intent uses the
  existing loopback GET immediately, coalesces bursts and supersedes a stalled
  poll. Follow-up polling is 300ms until source acknowledgement, capped at 20s.
- **Warm source switches use the cache.** Current-camera opposite-source tiles
  precede optional zoom neighbours. Interrupted rounds and missing disk tiles
  resume, and a complete mosaic tab return no longer skips missing site warming.
  Shared request limits remain in force. Already decoded tiles complete a source
  transition without waiting for a new fetch; repeated payloads preserve pending
  tile work. Older site volumes can publish their first tile across a source
  change without waiting for the whole viewport.
- **Radar v4.2 geography scheduling.** An independent `geo` worker starts home
  pre-warming with the engine, even on Observations and before radar fetches.
  It renders one missing tile per 250ms scheduling quantum, yields between
  background tiles, and idles after both themes/all home zooms are complete.
  Station/revision changes rebuild the queue. Fresh settled camera reports
  prioritize the current viewport; reported motion suppresses generation.
  Radar network passes cannot block it, and PNG/revision writes are atomic.
- **Kiosk benchmark summary first.** Cached paint, pan/pinch timing and draw
  counts, actual CDP network request counts, graphics memory, disk tile counts
  and fences precede details. `--verbose` includes raw frames/fetches/GPU data;
  `--radar-dir` selects the local tile cache for disk counts.
- **Radar v4.1 raster basemaps.** Paper and night now use opaque, versioned
  Natural Earth PNG-8 tiles with 4× antialiased strokes. The vector cell service
  and backing canvas are removed. Missing geography remains visible as a true
  latitude-adaptive graticule; local maps survive a cold provider outage.
- **Bounded graphics and immutable identities.** One 40 MiB admission budget
  covers both tile LRUs, history, canvases and decode reservations. Geography
  has a separate 32 MB/6,000-file disk cap with pinned home tiles. Radar tiles
  include the remap/ramp/basemap revision; the site table is independently
  versioned. Pruning removes empty directories and tile logs no longer grow.
- **Review fixes.** Dateline placement uses unwrapped rectangles; site range and
  acquisition completeness are separate. Historical echoes are composited once.
  AS OF and age both name the newest primary scan. Durable zoom/source caps,
  pointer cancellation, stationary release, abandoned source transitions,
  primary-site timestamp changes, viewport retries and first-view warming now
  preserve their state invariants. Partial histories no longer count as complete.
- **Verification.** Both-theme loopback checks include real captured input,
  cold acquisition, source recovery, fetch/decode instrumentation and independent
  graphics accounting. `tools/benchmark_radar_kiosk.py` prints live-page CDP
  pan/pinch frame times, draw counts, cached first paint and memory as JSON.
  Panel measurements remain the performance acceptance authority.
- **Tab-to-loop uses the warm hour immediately.** Entering Radar posts its view
  marker immediately and polls at 100ms until history arrives (20-second cap).
  The intent watcher schedules one view-start pass; an unchanged warm cache
  publishes history with zero provider HTTP. Off-tab passes retain the current
  geometry's hour of crops while fetching only newest, with the existing grace
  for abandoned generations and unchanged request budgets.
- **Play reflects intent while buffering.** The always-enabled button flips
  immediately between Play and Pause, with inventory feedback and a 120ms dip.
  Playback starts automatically at two decoded frames; pausing during buffering
  prevents a later automatic start. Reduced-motion single sweeps are preserved.
- **Site mode joins the lazy local radar cache.** Site listings run concurrently
  and retain each site's scan slots for 300 seconds on intent passes. Idle mosaic
  viewing warms selected site newest tiles after its loop and adjacent zooms;
  idle site viewing warms the mosaic and neighbouring zooms. Warm mode/zoom
  presses reuse discovery and native tiles with zero newest network requests.
  The eight-frame multi-site identity, 240/minute cap, 34-request reserve,
  60-slot admission headroom and 20-second deep-history view gate are unchanged.
- **Faint returns in site mode, from 5 dBZ.** Single and neighbouring NEXRAD
  sites gain one flat slate band at 5–10 dBZ, with alpha 180/255 multiplied by
  native coverage. The nine rain bands keep their exact pixels; MRMS and the
  global mosaic still start at 10, including the wide-view site fallback. The
  legend re-ticks inside the same box, with a theme-composited swatch, a short
  caption tail and an accessible explanation. Reflectivity only, never a
  precipitation-type inference.
- **The loop controls stay while frames arrive.** Zoom staging no longer hides
  Play, the rail or the read. Play remains enabled during buffering and clear
  content. With no frames the read shows the play/pause inventory and the marker
  is absent; the single corner note owns refresh progress. One frame pins the marker at newest. Every phase keeps the same box
  on paper and night.
- **Loop first, next zoom second, deep history last.** After the newest scan,
  acquire eight loop frames and warm adjacent zooms before spending requests on
  the rest of the hour. Deep history starts after 20 seconds of continuous viewing
  at the current geometry and leaves 60 spare requests above the interaction
  reserve, including archive probes and transport retries. It resumes as capacity
  returns and keeps the warmed newest tiles recent in the bounded cache. Each
  geometry can warm its neighbours once per scan; scheduled new scans follow the
  same order. New intents cancel at tile boundaries; multi-site keeps eight frames.
- **Zoom without asking what is newest again.** Intent passes reuse validated
  source timestamps and per-site scan listings for one source cadence, then go
  straight to tiles. Scheduled refreshes, expiry and failures revalidate; purged
  newest tiles trigger validation in the same pass. Successful archive probes
  are remembered per stamp. Interaction never extends freshness or cadence.
- **Map first, echoes follow.** Zoom and pan publish the new geography before any
  radar request. The map, station, rings and scale settle immediately; the drawn
  scan keeps its own reprojection at stale opacity until matching echoes decode.
  A second press continues from that held scan. The plate stays painted throughout.
- **Stop paying twice for the same tiles.** A bounded native-tile cache keeps valid
  downloads from superseded passes. Concurrent tile fetches share persistent
  IPv4/SNI connections across passes; failures and idle sockets are discarded.
  The shared limiter keeps its history reserve for the next widest mosaic. Worker
  publications emit immediately, intent checks run at 100 ms, and the page polls at
  400 ms until acknowledged with a frame, capped at 20 seconds. The single refresh
  note keeps its 600 ms suppression and honest restarted/failure copy.
- **A dropped keep-alive socket is not an outage.** The provider closes idle
  connections after a few seconds; a reused one that fails before any response
  byte is retried once on a fresh connection, idle reuse is bounded to four
  seconds (or the advertised Keep-Alive window), and a transport hiccup on the
  primary keeps the drawn scan and retries in seconds instead of switching to
  the global mosaic. The request cap rises from 90 to 240 a minute so an hour of
  history fills in about a minute and a half rather than five; the tile cache
  means re-zooms do not spend it again. The attribution in the caption is text,
  never a link: a kiosk has no way back from another site.

- **DNS stays warm when sockets go cold.** Resolved IPv4 addresses now live for
  15 minutes independently of keep-alive expiry. One worker resolves outside the
  pool lock; expired addresses serve tiles immediately while a background refresh
  runs, and a resolver failure keeps the last good answer until the next pass.
- **Warm the next zoom when idle.** A viewed radar pass with 60 spare requests
  above the interaction reserve warms the newest tiles one zoom out and one zoom
  in, once per scan. A new intent stops submissions; valid downloads stay in the
  same bounded cache without becoming frames. Newest fetches use six connections
  for parallel cold renders; history and idle warming stay at four.

## 2026-09-13

### Radar
- **Radar refresh recovery and Pi remapping.** Local budget deferrals keep fresh
  scans idle and retry when capacity returns; history reserves the next newest
  frame. Whole intents publish atomically, survive failed delivery, and stay
  monotonic across reloads. Verified lossless palette swaps replace repeated
  full-tile masks. Crop pixel loss and intensity ambiguity are disclosed; damaged
  cache entries rebuild. Primary recovery, actual-contributor captions, held
  historical frames and live failure timestamps now follow their own identities.
- **Radar v3: one reflectivity scale everywhere.** MRMS, NEXRAD and the global
  mosaic now share nine measured bands and a true dBZ scale, in the same pixels
  on paper and night. Returns below 10 dBZ disappear; stale echoes recede to .66.
  Native colour tables are pinned and remapped once per distinct tile colour,
  preserving alpha. Unrecognised colours are transparent and counted; an incomplete
  remap says so on the caption. The separate precipitation-type ramp is retired;
  RainViewer explicitly reads “reflectivity only.” Old native-colour crops cannot
  enter the new cache namespace. Secondary radar ink now clears 4.5:1 over the scrim.
- **Neighbouring radars fill the plate.** Site mode selects up to four intersecting
  230 km circles nearest the view, fetches only each site's intersecting tiles,
  and stacks the nearest on top. The nearest reporting site to your station owns
  the real scan times; neighbours contribute their latest scan within 15 minutes.
  A dark or failed neighbour does not discard working layers. Multi-site history
  stops at eight frames, with newest still published first. The picker names
  “KATX +2”; hairline coverage arcs and site labels explain where returns end,
  and the caption names a site that is not reporting.
- **Zoom out without losing your site choice.** Below zoom 7 a saved site choice
  shows the mosaic, with a dotted underline on the chosen site and an honest
  “resumes at zoom 7” note. Zooming back restores it; tapping the site while wide
  sets source and zoom 7 together. The shared zoom-out floor is now 4.
- **Fetches follow your hands.** A new pan or pinch continues from the held preview,
  keeping the drawn scan until matching geometry decodes. Every intent carries a
  sequence; a newer intent abandons the old pass at the next tile boundary without
  poisoning its retry cache. Rapid controls share a 120 ms trailing debounce.
  One non-blocking corner line replaces “Updating”: newest/history progress,
  restarted work, or a failed refresh naming the scan still shown. It waits 600 ms
  before appearing, and shares its space with zoom-cap and site-resume notes.
- **Zoom +/− responds instantly.** A stepper press now previews the new level on
  screen at once (the same centre-scale, held-until-the-frame-lands path a pinch
  uses) and posts the intent within a fraction of a second instead of waiting for
  the next poll — a burst of presses still coalesces into a single re-composite at
  the final level. Before, a press showed nothing until the poll and the server
  round-trip (~5 s), which read as broken.
- **Touch: pinch to zoom, drag to pan.** On the Pi's touchscreen (and with a mouse
  or trackpad in a browser) a two-finger pinch scales the whole plate live about your
  fingers and, on release, snaps to the nearest zoom level — the same remembered zoom
  the +/− stepper sets, capped per source with a rubber-band at the limits. A
  one-finger drag moves the map 1:1 with no inertia (every pan is a real
  re-composite around the lifted point, so momentum would be dishonest); on release
  the offset converts to a lat/lon through the exact Mercator projection the emitter
  uses, and the emitter re-centers the crop there — basemap, rings, scale bar and
  echoes all follow, and the scale stays truthful as latitude changes. Your station's
  marker moves to its true position with an accent ring, the meaningless plate-center
  crosshair goes away while panned, and a quiet "Recenter on station" button appears
  (it also auto-recenters after 90 s idle). Pan is deliberately transient — a wall
  display wakes on its own station — while zoom persists. The control clusters never
  start a gesture, the page never scrolls or browser-zooms, the loop freezes on the
  drawn frame during a gesture and resumes when the new frame lands, and
  reduced-motion turns the spring-backs into snaps.
- **Radar top-band cleanup.** The masthead content was overflowing its own 41px
  reservation and landing on the header line below it (the station subtitle and a
  30px clock, whose hidden alert/lightning flags pinned an oversized line box). On
  the radar tab the subtitle is dropped (it's already in the footer and the source
  caption), the clock drops to 22px with a fixed line-height, and the double-rule
  tightens under an alert — so nothing overlaps in either alert state. The word
  "REFLECTIVITY" was printed twice in one band; the legend's duplicate title is
  gone and the unit now sits inline as "dBZ" (mixed case — it's a unit) before the
  ramp. The header and the plate chrome share two clean columns instead of a
  four-step staircase, and the age suffix only appears once a frame is genuinely
  late (80% of the source's stale window) rather than on every routine MRMS scan.
- **Stale no longer false-alarms on a healthy 2-minute feed.** The stale flag was a
  bare 3× cadence (6 min for the mosaic), but the emitter never shows an MRMS frame
  younger than ~5 min (IEM renders on demand and returns 503 for newer minutes), so
  the freshest-possible frame already tripped it — the console read STALE, in alarm
  red, almost constantly. Stale now uses a per-source threshold set above each
  source's inherent latency (MRMS 10 min, single-site 15, global 20); "stale" once
  again means the feed actually stopped, not that the source runs its normal few
  minutes behind. Exposed as `staleSec` so the console stays consistent.
- **Radar v2: the radar is the star of its tab.** The plate grows from a 480px
  square to the whole 956 × 490 body — twice the echo area, all of it horizontal,
  where weather comes from — and the rail is gone: its eight elements become
  four quiet overlays confined to the top and bottom bands (nothing within 150px
  of the station marker), the dBZ legend turns into a horizontal ramp with ticks,
  and RANGE/UPDATED/FRAMES rows are cut (the scale bar states distance; scan time
  and lateness live only in the "As of" line). Overlays sit on a flat, theme-aware
  scrim (no filters — the Pi's GPU can't afford them) that clears WCAG 4.5:1 over the
  worst-case echo colours in both themes; no accent on any chrome.
- **Smooth playback.** Frames are pre-decoded to bitmaps and drawn on a canvas by a
  clock-referenced animation frame — a tick does no fetch, no decode, no image-src
  write and never touches the basemap. About nine frames a second (110 ms) with a
  1.1 s hold on the newest, hard cuts between scans (a crossfade would paint a state
  the radar never observed), one 120 ms dip on the wrap. Buffering is a visible
  state ("Buffering · N of M"); partial history loops what exists and labels its true
  start; reduced-motion gets a static newest frame and a one-sweep play button. To fit
  the wider plate on a Pi the decoded loop holds the last 16 frames (~32 min on the
  2-minute feed), and the caption reports the span it actually shows.
- **Tighter, source-aware default zoom.** Auto now targets 200 km across the plate's
  height (zoom 8 at mid-latitudes — half the ground scale of before, with the same
  horizontal reach), and may go finer where the live source can render it: the US
  mosaic to 9, a single site to 10, the global fallback stays at 7 with the existing
  "closest view" note.
- **Single-site NEXRAD, switchable.** A `MOSAIC | KATX` picker on the plate swaps the
  composite for the nearest radar's own super-resolution base reflectivity (IEM's
  per-scan RIDGE tiles, animated over real scan times, ~5-minute volumes, native
  palette verified against IEM's colour curve). The picker is honest about its
  states — pending, no site in range, site not reporting, hidden entirely on the
  global fallback, a failed switch reverts with a note — the choice persists across
  restarts, and stale is judged per source (three scans, not a fixed clock).
- **The 2-minute feed now actually keeps up.** Root causes fixed, not a deadline
  raised: one TLS connection and one DNS lookup per host per pass instead of a fresh
  handshake per tile (a 9-tile frame in ~1.2 s, down from ~3 s on a good link and far
  worse on the Pi), the not-yet-rendered freshest minutes are skipped and a bad tile
  ends its candidate immediately, a failed pass keeps the last good frame instead of
  flapping to the 10-minute source, a newer scan can never be replaced by an older
  one, and every fallback and source switch is now logged with its cause. The "As
  of" line carries an age suffix only when a frame is late (≥ 2 × cadence).
- **Diagnosed why the 2-minute US feed loses to the fallback on IPv6-broken
  networks.** IEM's host advertises an IPv6 address; where IPv6 is a black hole
  (as on the test Pi — `curl -6` times out, `curl -4` answers in 0.23s), Python's
  urllib has no Happy-Eyeballs and stalls ~10s on the dead address before falling
  back to IPv4, so the IEM adapter starves and the console silently stays on the
  10-minute global source. The cure is to reach IEM over IPv4 (disable IPv6 on the
  appliance). The primary-attempt cap stays tight (25s) — a longer cap only delays
  the fallback and leaves the plate empty longer when a source is unreachable; it
  was never the real limit. (Reverts a 55s bump from earlier the same day that had
  mis-attributed the stall to wifi bandwidth.)

### Console
- **Temperature curve: the forecast now begins where the temperature is.** The
  hourly forecast line used to weld its start onto the sensor reading and then run
  to the model's first future hour — so whenever the sensor and the model disagreed
  (sensor 55°, model 53° in rain) it drew a drop-and-recover the forecast never
  predicted, and printed a phantom low label ("53° · 14:00"). A first fix anchored
  the forecast at the model's own now-value with a vertical seam; on the live panel
  that read as a cliff to the chart floor — the same false story. The forecast now
  starts at the current reading and blends onto the model over the next few hours
  (a standard nowcast bias correction: the sensor is the better guide near-term,
  the model for the rest of the day), with a smoothstep decay so there's no kink at
  the join, and a horizon that ends exactly at the model's first turning point so
  the drawn peak *is* the model's peak. Result on the day that exposed it: 55.0 →
  55.1 → 55.1 → 55.6 → 56.5, no dip, and "57° · 17:00" still agrees with HIGH 57°.
  Extreme labels print only where the drawn and model values round the same, so a
  label can never contradict the HIGH/LOW row. Observed-only rendering (no hourly
  data) is byte-identical to before. Guarded by a headless check that fails on the
  old rendering.
- **No more phantom forecast on a cold boot.** The page ships as the design
  artboard, and until the first data frame arrived it kept showing the artboard's
  sample values — 64.0°, "Rising 4.6° per hour", **Low 50° / High 82°**, "Clear &
  Sunny", a July date, sunrise 05:43 — as if they were real, behind only a small
  STALE mark. On a freshly rebooted Pi (or with the engine down) that read as a
  plausible, wildly wrong forecast. The console now paints its no-data pose
  (dashes everywhere, gauges neutral) before the first poll, using the same
  missing-value rules every panel already follows, so it never shows a number it
  hasn't received. The headless verifier now guards this (a cold load with no
  `wx.json` must show no sample values).

## 2026-09-12

### Console
- **New Radar tab.** A regional radar mosaic from RainViewer, centered on the
  station so it works anywhere on Earth (not just the US). The echoes keep
  RainViewer's true reflectivity colors with a real dBZ scale and snow shown
  distinctly from rain — the one deliberately bordered exception inside the
  four-pigment console — framed as an instrument with a console-drawn basemap
  (range rings, scale bar, station marker), light and dark both first-class.
  Honest states: fetching / no echoes shown / stale; the tab hides where there
  is no location. Zoom adapts to hold a consistent ~256 km view at any latitude
  (clamped to the free tier). The emitter keeps only the latest frame current
  when the tab is unwatched and builds the full history only when it's been
  viewed — an idle radar tab costs one frame's fetch, not thirteen. Radar runs
  off-thread and never affects engine health.
- **Radar animation loop.** While the Radar tab is open the past hour of frames
  plays as a loop (oldest → newest, hold on the latest), so motion reads as "now";
  a Play/Pause control and a relative "−N min → newest" counter sit under the plate.
  The loop touches only the echo layer — never the basemap — pauses off-tab and
  under reduced-motion, and never fetches per frame.
- **Finer cadence where it's available.** For US stations the radar now leads with
  IEM's MRMS reflectivity mosaic (~2-minute frames), falling back to RainViewer's
  10-minute global mosaic elsewhere or when the primary is unavailable — it always
  tries the finest source first. The plate shows both an "As of" scan time and a
  distinct "Updated" refresh time (RadarScope-style), plus the source's frame
  cadence, and the legend switches to match whichever source is live (IEM's native
  reflectivity table or RainViewer's Universal Blue). Source hand-offs stage
  atomically so a switch never shows a half-loaded or mislabeled frame.
- **Persistent radar zoom.** A quiet +/− stepper in the rail with a reset-to-auto,
  so you can pin the radar closer or wider than the latitude-auto default; the level
  is saved per station and survives refreshes and reboots (kept in durable state,
  not tmpfs). The control is honest about each source's real range — RainViewer's
  free tier caps at zoom 7, the US MRMS feed reaches 9 — so it never offers a step
  that does nothing, and a level set closer than the live source reaches shows that
  source's closest view while remembering your intent (a saved zoom 8 shows 7 on
  RainViewer and restores to 8 when the US feed returns). Keyboard-operable (+/−/0),
  both themes, no accent on the chrome. Zoom rides a loopback preference the emitter
  reads, so the plate re-composites server-side at the chosen level (no CSS scaling,
  no blur); scale bar, range rings, and the loop all follow, and a zoom change
  respects the same demand-gate, rate limiter, and cooldowns as every other refresh.
- **Geographic basemap under the echoes.** The radar plate now shows real geography
  — coastline and water, country and state/province borders, and major roads — as
  hairline themed lines beneath the reflectivity, RadarScope-style, so a storm reads
  against the land instead of an abstract grid. It's drawn from bundled Natural Earth
  1:10m vector data (public domain, ~3.6 MB), so it needs no API key and works
  offline, anywhere on Earth — a landlocked station shows borders and roads, a coastal
  one shows the shoreline, mid-ocean stays calm water. The emitter projects and clips
  the geography to the exact station-centered viewport (pixel-registered to the
  echoes) and writes a class-tagged SVG the console colors entirely through its own
  palette tokens, so both light and dark are correct with no accent on the map
  furniture. It's generated once per viewport (never per animation frame), only while
  the tab is watched, cached and pruned like the echo frames, and it falls back to the
  old graticule if anything is missing — radar never breaks. The abstract lat/lon grid
  gives way to the basemap when it's present. Build tooling and provenance live in
  `tools/RADAR_BASEMAP.md`.

### Core (shared with the classic console)
- Sager Weathercaster no longer fails on a clear sky: it keyed on CheckWX's
  parsed `clouds` array, which is omitted for CLR/SKC, so on a clear day the
  forecast errored with "Missing METAR cloud information." It now selects the
  nearest station whose raw METAR carries a sky group (clear codes included).

## 2026-09-08

The Pi 4's DSI panel was replaced with a 1024×600 HDMI **capacitive
touchscreen** (Elecrow 7"), which surfaced a set of touch/layout issues.

### Console
- Navigation tabs (Observations / Moon & Sky / Lightning / Sager) can be
  enabled for a touch kiosk. The tab bar is hidden by default; the launcher
  adds `tabs=1` (env `WFP_TABS=0` opts out).
- When the tab bar is shown, the Observations screen now fits the panel: the
  forecast chart yields its slack height, the barometer panel reflows so the
  value/trend and both charts keep room, and the AQI category/trend wraps to a
  full line instead of truncating ("Moderate to…"). All of it is scoped to the
  tabbed mode, so the default single-dashboard layout is unchanged.

### Kiosk
- WiFi keepalive now probes a stable LAN **peer** (the other Pi) instead of the
  default gateway. The gateway stays reachable while the box is isolated from
  the rest of the LAN, so the old probe never fired during an hour-long dropout.
  Adds a recovery cooldown, run serialization, and a post-recovery check that
  only clears the failure count once the peer actually answers.
- Display/touch setup documented for the new panel: 1024×600 is the console's
  native artboard (fit 1.0), the `vc4-kms-v3d` driver and native-mode rule
  (never force a higher mode a small panel only downscales), and the labwc
  touch mapping — labwc matches the libinput device name exactly, and a rule
  pinned to a connector or device that is later swapped out silently breaks
  touch.

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
