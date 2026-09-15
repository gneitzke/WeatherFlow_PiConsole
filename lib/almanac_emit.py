""" Additive JSON data-emitter for the "almanac" overlay layout.

This module is NEW and does not alter the classic console data path in any
way. It periodically reads the same DictProperties the classic screen already
populates (`CurrentConditions.Obs` / `.Astro` / `.Met` / `.Sager` / `.System`,
plus `app.config['Station']`) and writes them out as a flat, display-ready
JSON file that an external HTML overlay polls (design/almanac/console.html).
The exact shape is documented in design/almanac/DATA_CONTRACT.md - read that
file before changing any key here.

Wiring: panels/almanac.py's AlmanacConditions.add_panels() calls
`AlmanacEmitter(self).start()` once. Nothing in main.py's classic path
(CurrentConditions) imports this module, so the classic screen is completely
unaffected.

Copyright (C) 2018-2025 Peter Davis (classic console) / almanac add-on.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
"""

from kivy.logger import Logger
from kivy.clock  import Clock

from collections import OrderedDict, deque, namedtuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta, timezone
import json
import io
import hashlib
from functools import lru_cache
from pathlib import Path
from statistics import median
import math
import os
import re
import threading
from threading import Event as _Event, Thread as _InventoryThread, RLock as _RLock   # kept apart from `threading`, which tests stub
import time
import pytz

from lib.radar_geometry import (world_point, world_inverse, parse_center,
                                circle_intersects_bounds, distance_meters)
from lib.radar_http import failure_class, RadarSession, is_transport_error, LocalTransportError
from lib.radar_fetch import HostHealth, CircuitOpen, Attempt, AttemptCancelled, tile_race
from lib.radar_discovery import DiscoverySchedule
import logging
# Pillow's PNG reader logs every chunk at DEBUG ("STREAM b'IDAT' ..."), and Kivy's
# root logger passes DEBUG through to its file handler: on the Pi that was ~800 SD-card
# writes per radar pass, serialising the four tile workers on the log lock (measured
# 1.7 s of a 2.4 s zoom pass). Third-party chatter never belongs in the console log.
logging.getLogger('PIL').setLevel(logging.INFO)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Default output path. This is a deployment concern for the HTML overlay
# (which is served/polled independently of the console), not a user-facing
# console setting, so it is kept as a plain module constant rather than wired
# into lib/config.py. Override by editing this constant.
OUTPUT_PATH   = '/tmp/wfp_data/wx.json'
EMIT_INTERVAL = 2.0     # seconds, per DATA_CONTRACT.md ("~every 2 s")
VERSION_CHECK_INTERVAL = 900   # seconds (15 min) — how often we poll GitHub for a newer release
AQI_CHECK_INTERVAL     = 600   # seconds (10 min) — refresh air quality; short enough to recover fast
ALERTS_CHECK_INTERVAL  = 900   # seconds (15 min) — NWS alerts change slowly; be gentle on api.weather.gov
FORECAST_CHECK_INTERVAL = 3600 # seconds (1 h) — the daily outlook barely moves intra-hour
FORECAST_RETRY_SEC      = 120  # seconds — boot retry cadence until the FIRST forecast succeeds
RADAR_RETRY_SEC = 120
RADAR_LOCAL_RETRY_MAX_SEC = 60  # ceiling for the doubling retry after consecutive local failures
RADAR_HISTORY_SEC = 3600
RADAR_IEM_FRAME_INTERVAL_SEC = 120
RADAR_RAINVIEWER_FRAME_INTERVAL_SEC = 600
RADAR_IEM_STALE_SEC = 600
RADAR_RAINVIEWER_STALE_SEC = 1200
# Politeness cap for one console against a public tile cache. The window fits
# the eight-frame loop and adjacent newest tiles; deep history uses only capacity
# above their headroom floor. The native-tile LRU avoids spending it on re-zooms.
RADAR_REQUESTS_PER_MIN = 240
# Two worst-case frames (5x3 mosaic tiles + metadata + archive probe each): history
# backfill must leave the NEXT interaction's newest frame able to start at once even
# while the previous zoom's history is still streaming. One frame's worth left a
# zoom's first echoes waiting 3-4 s for headroom on the Pi.
RADAR_HISTORY_RESERVE = 34
RADAR_MAX_FRAME_BUILDS_PER_PASS = 20
RADAR_HTTP_TIMEOUT_SEC = 8
RADAR_TILE_TIMEOUT_SEC = 6
RADAR_HEDGE_SEC = 2
RADAR_SOURCE_DEADLINE_SEC = 16
# Preserve the tight primary deadline; persistent IPv4 TLS and readiness lag
# address acquisition cost rather than hiding it behind a longer timeout.
RADAR_PRIMARY_DEADLINE_SEC = 25
RADAR_BUILD_DEADLINE_SEC = RADAR_PRIMARY_DEADLINE_SEC
RADAR_TILE_CACHE_SIZE = 400
RADAR_TILE_WORKERS = 4
RADAR_NEWEST_TILE_WORKERS = 6
RADAR_PREFETCH_HEADROOM = 60
RADAR_LOOP_FRAMES = 8
RADAR_DEEP_VIEW_SEC = 20
RADAR_VIEW_POLL_GAP_SEC = 5  # normal wx.json polling is every two seconds
RADAR_INTENT_CHECK_SEC = .1
RADAR_GEO_QUANTUM_SEC = .25
RADAR_GEO_UNVIEWED_SLEEP_SEC = .05
RADAR_NEGATIVE_CACHE_SEC = 120
RADAR_CACHE_GRACE_SEC = 120
RADAR_IEM_METADATA_URL = "https://mesonet.agron.iastate.edu/data/gis/images/4326/mrms/lcref.json"
RADAR_IEM_TILE_TEMPLATE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/mrms::lcref-{stamp}/{z}/{x}/{y}.png"
RADAR_IEM_ARCHIVE_TEMPLATE = "https://mesonet.agron.iastate.edu/archive/data/%Y/%m/%d/GIS/mrms/lcref_%Y%m%d%H%M.png"
RADAR_SITE_MIN_ZOOM = 7
RADAR_SITE_RANGE_METERS = 230000
RADAR_SITE_MAX_COUNT = 4
RADAR_SITE_MAX_AGE_SEC = 900
RADAR_MIN_ZOOM = 4
RADAR_MAX_ZOOM = 9
RADAR_TARGET_METERS = 200000
RADAR_VIEW_TTL = 900
RADAR_VIEWPORT_W = 956
RADAR_VIEWPORT_H = 490
RADAR_VIEWPORT_PX = RADAR_VIEWPORT_H  # short-axis scale compatibility
# MRMS on-demand tiles routinely trail metadata: predict their publication window.
RADAR_IEM_READY_LAG_SEC = 300  # readiness prediction only; never suppress an advertised scan
RADAR_SITE_LIST_URL = "https://mesonet.agron.iastate.edu/json/radar.py"
RADAR_SITE_TILE_TEMPLATE = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/ridge::{site}-N0B-{stamp}/{z}/{x}/{y}.png"
RADAR_RAINVIEWER_COLOR = 2
RADAR_RAINVIEWER_TILE_OPTS = "0_0"
RADAR_RAINVIEWER_MANIFEST_URL = "https://api.rainviewer.com/public/weather-maps.json"
RADAR_DIR = os.environ.get("WFP_RADAR_DIR", os.path.expanduser("~/almanac_web/radar"))
from lib.radar_palette import _RADAR_RAMP, _RADAR_LUT, _RADAR_SITE_RAMP, REMAP_REVISION, SMOOTH_REVISION, remap, smooth_remap, source_palette
_RADAR_SOURCES = {
    'iem-nexrad-n0b': dict(provider='iem', attribution='IEM / NOAA',
        attribution_url='https://mesonet.agron.iastate.edu/GIS/ridge.phtml',
        cadence=300, stale_sec=900, legend=_RADAR_SITE_RAMP, max_zoom=10),
    'iem-mrms-lcref': dict(provider='iem', attribution='IEM / NOAA MRMS',
        attribution_url='https://mesonet.agron.iastate.edu/ogc/',
        cadence=RADAR_IEM_FRAME_INTERVAL_SEC, stale_sec=RADAR_IEM_STALE_SEC,
        legend=_RADAR_RAMP, max_zoom=9),
    'rainviewer': dict(provider='rainviewer', attribution='RainViewer',
        attribution_url='https://www.rainviewer.com/',
        cadence=RADAR_RAINVIEWER_FRAME_INTERVAL_SEC, stale_sec=RADAR_RAINVIEWER_STALE_SEC,
        legend=_RADAR_RAMP, max_zoom=7),
}
# Coarse station-center CONUS land mask (lon, lat), deliberately independent of
# the nearest-NEXRAD caption. Coast/border detail is approximate, not geocoding.
_RADAR_CONUS = ((-124.73,48.4), (-123.1,48.3), (-123.1,49), (-95.16,49),
    (-95.16,49.38), (-94.8,49.38), (-94.6,48.7), (-92.7,48.55), (-90.9,48.25),
    (-89.5,48), (-88.4,48.3), (-84.9,46.9), (-84.5,46.45), (-84.1,46.5),
    (-83.5,45.8), (-82.5,45.3), (-82.1,43.6), (-82.42,43), (-82.42,42.5),
    (-83.1,42.3), (-83.15,42.05), (-83,41.7), (-82.7,41.7), (-80.5,42.3),
    (-79.1,42.85), (-79.05,43.45), (-76.8,43.6), (-76.45,44.1), (-74.7,45),
    (-71.5,45), (-71.4,45.25), (-70.7,45.4), (-70,46.4), (-69.2,47.45),
    (-68.3,47.35), (-67.8,47), (-67.8,45.7), (-67,44.8), (-70,43),
    (-69.8,41.5), (-72,41), (-74,40.5), (-75.5,35.2), (-80.5,32),
    (-80,26.7), (-80.05,25.6), (-80.4,25.1), (-81.2,24.5), (-82,26), (-82.8,28),
    (-84,30), (-89,29), (-90,29), (-93.8,29.7), (-97.15,25.95),
    (-97.5,25.85), (-99.1,26.4), (-99.5,27.5), (-100.3,28.3),
    (-101.4,29.8), (-102.8,29.2), (-103,29), (-104.5,29.65),
    (-106.5,31.78), (-108.2,31.78), (-108.2,31.33), (-111.07,31.33),
    (-114.72,32.72), (-117.13,32.54), (-120.6,34.5), (-122.5,37.7),
    (-124.4,40.4), (-124.6,42), (-124,46.3))
# Live original PNG was 7000 x 3500, .01 degrees/cell, world-file upper-left
# center (-129.995, 54.995): outer edges west/east -130/-60, south/north 20/55.
_RADAR_IEM_DOMAIN = dict(w=-130, e=-60, s=20, n=55)


FC_STALE_SEC           = 86400 # seconds (24 h) without a successful forecast fetch -> fcStale (band hides)
RAIN_WINDOW_SEC        = 600   # seconds (10 min) — light rain is bridged across the sensor's dry minutes
ALERTS_TIMEOUT         = 20    # seconds — socket timeout for the alerts fetch
ALERT_STALE_SEC        = 3600  # seconds (1 h) without a successful alerts fetch -> mark alertsStale
AQI_STALE_SEC          = 3600  # seconds (1 h) without a successful AQI fetch -> mark aqiStale
ALERT_MAX              = 3     # cap the alerts array (the HTML strip renders only the lead)
# NWS asks for a User-Agent that identifies the app with a contact. Keep any real
# address OUT of git: read Station/Contact from config, else env ALMANAC_CONTACT,
# else this generic repo URL (NWS rejects a blank/absent UA with 403).
ALERTS_UA_FALLBACK = 'WeatherFlow-PiConsole-almanac (+https://github.com/gneitzke/WeatherFlow_PiConsole)'

# NWS product level parsed from the LAST word of the event name — a controlled
# vocabulary that stays reliable even when CAP severity/urgency are 'Unknown'.
# 'Alert' products (e.g. Air Quality Alert) bucket as advisory-tier. Higher = more urgent.
_ALERT_LEVEL     = {'emergency': 4, 'warning': 4, 'watch': 3, 'advisory': 2, 'alert': 2, 'danger': 2, 'statement': 1, 'outlook': 0}
_ALERT_LEVELNAME = {4: 'warning', 3: 'watch', 2: 'advisory', 1: 'statement', 0: 'outlook'}
# Hazard family — for the label/nuance only; the banner colour is derived from the level.
_EVENT_CLASS_MAP = [
    ('tornado', 'severe'), ('thunderstorm', 'severe'), ('hurricane', 'severe'), ('tsunami', 'severe'),
    ('air quality', 'air'), ('smoke', 'air'), ('red flag', 'air'), ('fire', 'air'), ('heat', 'heat'),
    ('winter', 'winter'), ('snow', 'winter'), ('ice', 'winter'), ('freeze', 'winter'),
    ('wind', 'wind'), ('flood', 'water'), ('coastal', 'water'), ('fog', 'water'),
    ('gale', 'water'), ('surf', 'water'),
]
                               # from a transient boot-time network failure on the flaky USB wifi

# Placeholder strings used throughout properties.py / observation_format.py to
# mean "no data yet" ('-', '--', '---', ...). Any of these should collapse to
# None rather than being emitted as a literal dash.
_PLACEHOLDERS = {'-', '--', '---', '----', '-----', '------'}

# Strips Kivy markup, e.g. '[color=ff8837ff]Rising[/color]' -> 'Rising'
_MARKUP_RE = re.compile(r'\[/?color[^\]]*\]')

# Single-glyph degree symbols produced by observation_format.format(...,'Temp')
# (u'\N{DEGREE FAHRENHEIT}' / u'\N{DEGREE CELSIUS}') normalised to the plain
# two-character form the HTML/JSON contract expects.
_DEGREE_GLYPHS = {'℉': '°F', '℃': '°C'}

_COMPASS_16 = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
               'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']


# ==============================================================================
# SAFE ACCESSORS
# ==============================================================================
# The console's Obs/Astro/Met/Sager DictProperties hold values that are lists
# produced by observation_format.format()/units() (e.g. ['64.0', 'F']), but
# early in the app lifecycle - or after a station/device change resets a
# DictProperty - individual entries can still be the bare placeholder string
# ('-', '--', ...) or, in principle, missing/None. These helpers make every
# lookup below crash-proof.
def _idx(seq, i, default=None):
    """ Safely index into a value that SHOULD be a list/tuple, tolerating a
    bare placeholder scalar, None, or a too-short list. Never raises. """
    if seq is None:
        return default
    if isinstance(seq, (list, tuple)):
        if 0 <= i < len(seq):
            val = seq[i]
            return default if val is None else val
        return default
    # Not a list - a bare scalar/placeholder string. Only index 0 applies.
    return seq if i == 0 else default


def _get(d, key, default=None):
    """ Safely fetch `key` from a dict-like object that might not be one. """
    try:
        return d[key]
    except (KeyError, TypeError, IndexError):
        return default


class _RainWindow:
    """ Time-weighted mean rain rate over the last `span` seconds. Each sample
    holds until the next one, so sampling on the 2 s emit tick reproduces the
    sensor's per-minute steps faithfully; the mean over the window is the
    rain that actually fell, expressed as a rate. effective() returns
    max(raw, mean): a drizzle's dry minutes are bridged, a downpour is never
    understated, and the window drains linearly once rain stops. """

    def __init__(self, span):
        self.span    = span
        self.samples = []            # [(epoch_s, mm_per_hr), ...] oldest first

    def effective(self, now, rate_mm_hr):
        if rate_mm_hr is not None:
            self.samples.append((now, float(rate_mm_hr)))
        cutoff = now - self.span
        self.samples = [s for s in self.samples if s[0] >= cutoff]
        if rate_mm_hr is None or not self.samples:
            return None
        total = 0.0
        for (t0, r), (t1, _) in zip(self.samples, self.samples[1:]):
            total += r * (t1 - t0)
        t_last, r_last = self.samples[-1]
        total += r_last * (now - t_last)
        covered = max(now - self.samples[0][0], 1.0)
        mean = total / covered
        return round(max(float(rate_mm_hr), mean), 4)


def _cfg(config, section, option, default=None):
    """ Safely read a Kivy ConfigParser value. Kivy's ConfigParser needs BOTH
    section and option to .get() (subscripting a section internally calls the
    one-arg .get() and raises), so always pass both and guard. """
    try:
        return config.get(section, option)
    except Exception:
        return default


def _clean_str(value):
    """ Strip whitespace and return None for known placeholder strings. """
    if not isinstance(value, str):
        return value
    text = value.strip()
    return None if (not text or text in _PLACEHOLDERS) else text


def _num(value, default=None):
    """ Coerce a formatted display value ('64.0', '--', 'Trace', 4.6, ...)
    into a float. 'Trace' (a sub-measurable rain amount) is treated as 0.0
    since that is closer to the truth than null. Never raises. """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return default
        return number if math.isfinite(number) else default
    if isinstance(value, str):
        text = _clean_str(value)
        if text is None:
            return default
        if text.lower() == 'trace':
            return 0.0
        # Strike counts can render as e.g. "1.2 k" for >= 1000
        if text.lower().endswith('k'):
            try:
                number = float(text[:-1].strip()) * 1000
                return number if math.isfinite(number) else default
            except (OverflowError, ValueError):
                return default
        try:
            number = float(text)
            return number if math.isfinite(number) else default
        except (OverflowError, ValueError):
            return default
    return default


def _json_safe(value):
    """ Replace non-finite measurements before strict JSON serialization. """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _range_mid(value, default=None):
    """ Midpoint of a formatted uncertainty range. The console core renders the
    last strike distance as a +/-3 km band ("13-17"), never a bare number, so
    every numeric consumer of it needs the middle of that band. Accepts a plain
    number/numeric string too. Never raises. """
    text = _text(value)
    if text is None:
        return default
    parts = text.replace(u'\u2013', '-').split('-')
    nums = [_num(p) for p in parts if _num(p) is not None]
    if not nums:
        return default
    return sum(nums) / len(nums)


def _text(value, default=None):
    """ Coerce a value into a clean display string: strips Kivy colour markup
    and blanks known placeholders. Never raises. """
    if value is None:
        return default
    if not isinstance(value, str):
        return str(value)
    text = _clean_str(value)
    if text is None:
        return default
    if '[' in text and ']' in text:
        text = _MARKUP_RE.sub('', text).strip()
    return text or default


def _wind_desc(value):
    """ Beaufort description word, trimmed for a status label
    ('Calm Conditions' -> 'Calm'). """
    text = _text(value)
    return text.replace(' Conditions', '') if text else text


def _temp_unit(value):
    """ Normalise the single-glyph degree unit (u'\N{DEGREE FAHRENHEIT}' etc.)
    produced by observation_format into the plain "°F"/"°C" the contract
    shows, falling back to whatever text is present. """
    text = _text(value)
    if text is None:
        return None
    return _DEGREE_GLYPHS.get(text, text)


def _cardinal_from_degrees(deg):
    if deg is None:
        return None
    try:
        return _COMPASS_16[int(round((float(deg) % 360) / 22.5)) % 16]
    except (TypeError, ValueError):
        return None


def _age_sec(ts, now):
    """ Whole seconds since `ts`, or None when that source has never reported.
    Clamped at 0 - a sensor clock a little ahead of ours must not produce a
    negative age. Never raises. """
    if ts is None:
        return None
    try:
        return max(0, int(now - float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def _ago_text(sec):
    """ "just now" / "12 minutes ago" / "5 hours ago" / "3 days ago" from an age
    in seconds, matching the vocabulary observation_format's 'TimeDelta' uses. """
    if sec is None:
        return None
    sec = int(sec)
    for span, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        if sec >= span:
            count = sec // span
            return f'{count} {unit}{"" if count == 1 else "s"} ago'
    return 'just now'


def _since_ago_text(strike_delta_t):
    """ Build a "3 days ago" / "5 hours ago" / "12 minutes ago" style string
    from the ['d','days','h','hours', epoch] shape that
    observation_format.format(..., 'TimeDelta') produces for StrikeDeltaT.
    Returns None if the underlying value is a placeholder (no strikes seen). """
    n1, u1 = _text(_idx(strike_delta_t, 0)), _text(_idx(strike_delta_t, 1))
    if n1 is None or u1 is None:
        return None
    return f'{n1} {u1} ago'


# NOAA NCEI HOMR station inventory, verified 2026-09-12:
# https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt
# 160 WSR-88D sites; excludes TDWR, test radars KCRI/KOUN, retired KLIX (KHDC replaces it).
_NEXRAD_SITES = {
    'KABR': (45.455833, -98.413333, 'Aberdeen'),
    'KABX': (35.149722, -106.82388, 'Albuquerque'),
    'KAKQ': (36.98405, -77.007361, 'Norfolk Rich'),
    'KAMA': (35.233333, -101.70927, 'Amarillo'),
    'KAMX': (25.611083, -80.412667, 'Miami'),
    'KAPX': (44.90635, -84.719533, 'Gaylord'),
    'KARX': (43.822778, -91.191111, 'La Crosse'),
    'KATX': (48.194611, -122.49569, 'Camano Island'),
    'KBBX': (39.495639, -121.63161, 'Beale Afb'),
    'KBGM': (42.199694, -75.984722, 'Binghamton'),
    'KBHX': (40.498583, -124.29216, 'Eureka'),
    'KBIS': (46.770833, -100.76055, 'Bismarck'),
    'KBLX': (45.853778, -108.6068, 'Billings'),
    'KBMX': (33.172417, -86.770167, 'Birmingham'),
    'KBOX': (41.955778, -71.136861, 'Boston'),
    'KBRO': (25.916, -97.418967, 'Brownsville'),
    'KBUF': (42.948789, -78.736781, 'Buffalo'),
    'KBYX': (24.5975, -81.703167, 'Key West'),
    'KCAE': (33.948722, -81.118278, 'Columbia'),
    'KCBW': (46.03925, -67.806431, 'Houlton'),
    'KCBX': (43.490217, -116.23603, 'Boise'),
    'KCCX': (40.923167, -78.003722, 'State College'),
    'KCLE': (41.413217, -81.859867, 'Cleveland'),
    'KCLX': (32.655528, -81.042194, 'Charleston'),
    'KCRP': (27.784017, -97.51125, 'Corpus Christi'),
    'KCXX': (44.511, -73.166431, 'Burlington'),
    'KCYS': (41.151919, -104.80603, 'Cheyenne'),
    'KDAX': (38.501111, -121.67783, 'Sacramento'),
    'KDDC': (37.760833, -99.968889, 'Dodge City'),
    'KDFX': (29.273139, -100.28033, 'Laughlin Afb'),
    'KDGX': (32.279944, -89.984444, 'Jackson Brandon'),
    'KDIX': (39.947089, -74.410731, 'Philadelphia'),
    'KDLH': (46.836944, -92.209722, 'Duluth'),
    'KDMX': (41.7312, -93.722869, 'Des Moines'),
    'KDOX': (38.825767, -75.440117, 'Dover Afb'),
    'KDTX': (42.7, -83.471667, 'Detroit'),
    'KDVN': (41.611667, -90.580833, 'Davenport'),
    'KDYX': (32.5385, -99.254333, 'Dyess Afb'),
    'KEAX': (38.81025, -94.264472, 'Kansas City'),
    'KEMX': (31.89365, -110.63025, 'Tucson'),
    'KENX': (42.586556, -74.064083, 'Albany'),
    'KEOX': (31.460556, -85.459389, 'Fort Rucker'),
    'KEPZ': (31.873056, -106.698, 'El Paso'),
    'KESX': (35.70135, -114.89165, 'Las Vegas'),
    'KEVX': (30.565033, -85.921667, 'Eglin Afb'),
    'KEWX': (29.704056, -98.028611, 'Austin San Antonio'),
    'KEYX': (35.09785, -117.56075, 'Edwards'),
    'KFCX': (37.0244, -80.273969, 'Roanoke'),
    'KFDR': (34.362194, -98.976667, 'Altus Afb'),
    'KFDX': (34.634167, -103.61888, 'Cannon Afb'),
    'KFFC': (33.36355, -84.56595, 'Atlanta'),
    'KFSD': (43.587778, -96.729444, 'Sioux Falls'),
    'KFSX': (34.574333, -111.19844, 'Flagstaff'),
    'KFTG': (39.786639, -104.5458, 'Denver Front Range Ap'),
    'KFWS': (32.573, -97.30315, 'Dallas'),
    'KGGW': (48.206361, -106.62469, 'Glasgow'),
    'KGJX': (39.062169, -108.21376, 'Grand Junction'),
    'KGLD': (39.366944, -101.70027, 'Goodland'),
    'KGRB': (44.498633, -88.111111, 'Green Bay'),
    'KGRK': (30.721833, -97.382944, 'Fort Hood'),
    'KGRR': (42.893889, -85.544889, 'Grand Rapids'),
    'KGSP': (34.883306, -82.219833, 'Greer'),
    'KGWX': (33.896917, -88.329194, 'Columbus Afb'),
    'KGYX': (43.891306, -70.256361, 'Portland'),
    'KHDC': (30.5193, -90.4074, 'Hammond Municipal Airport'),
    'KHDX': (33.077, -106.12003, 'Holloman Afb'),
    'KHGX': (29.4719, -95.078733, 'Houston'),
    'KHNX': (36.314181, -119.63213, 'San Joaquin Valley'),
    'KHPX': (36.736972, -87.285583, 'Fort Campbell'),
    'KHTX': (34.930556, -86.083611, 'Huntsville'),
    'KICT': (37.654444, -97.443056, 'Wichita'),
    'KICX': (37.59105, -112.86218, 'Cedar City'),
    'KILN': (39.420483, -83.82145, 'Cincinnati'),
    'KILX': (40.1505, -89.336792, 'Lincoln'),
    'KIND': (39.7075, -86.280278, 'Indianapolis'),
    'KINX': (36.175131, -95.564161, 'Tulsa'),
    'KIWA': (33.289233, -111.66991, 'Phoenix'),
    'KIWX': (41.358611, -85.7, 'Fort Wayne'),
    'KJAX': (30.484633, -81.7019, 'Jacksonville'),
    'KJGX': (32.675683, -83.350833, 'Robins Afb'),
    'KJKL': (37.590833, -83.313056, 'Jackson'),
    'KLBB': (33.654139, -101.81416, 'Lubbock'),
    'KLCH': (30.125306, -93.215889, 'Lake Charles'),
    'KLGX': (47.116944, -124.10666, 'Langley Hill'),
    'KLNX': (41.957944, -100.57622, 'North Platte'),
    'KLOT': (41.604444, -88.084444, 'Chicago'),
    'KLRX': (40.73955, -116.8027, 'Elko'),
    'KLSX': (38.698611, -90.682778, 'St Louis'),
    'KLTX': (33.98915, -78.429108, 'Wilmington'),
    'KLVX': (37.975278, -85.943889, 'Louisville'),
    'KLWX': (38.976111, -77.4875, 'Sterling'),
    'KLZK': (34.8365, -92.262194, 'Little Rock'),
    'KMAF': (31.943461, -102.18925, 'Midland Odessa'),
    'KMAX': (42.081169, -122.71736, 'Medford'),
    'KMBX': (48.393056, -100.86444, 'Minot Afb'),
    'KMHX': (34.775908, -76.876189, 'Morehead City'),
    'KMKX': (42.9679, -88.550667, 'Milwaukee'),
    'KMLB': (28.113194, -80.654083, 'Melbourne'),
    'KMOB': (30.679444, -88.24, 'Mobile'),
    'KMPX': (44.848889, -93.565528, 'Minneapolis'),
    'KMQT': (46.531111, -87.548333, 'Marquette'),
    'KMRX': (36.168611, -83.401944, 'Knoxville'),
    'KMSX': (47.041, -113.98622, 'Missoula'),
    'KMTX': (41.262778, -112.44777, 'Salt Lake City'),
    'KMUX': (37.155222, -121.89844, 'San Francisco'),
    'KMVX': (47.527778, -97.325556, 'Grand Forks'),
    'KMXX': (32.53665, -85.78975, 'Maxwell Afb'),
    'KNKX': (32.919017, -117.0418, 'San Diego'),
    'KNQA': (35.344722, -89.873333, 'Memphis'),
    'KOAX': (41.320369, -96.366819, 'Omaha'),
    'KOHX': (36.247222, -86.5625, 'Nashville'),
    'KOKX': (40.865528, -72.863917, 'New York City'),
    'KOTX': (47.680417, -117.62677, 'Spokane'),
    'KPAH': (37.068333, -88.771944, 'Paducah'),
    'KPBZ': (40.531717, -80.217967, 'Pittsburgh'),
    'KPDT': (45.69065, -118.85293, 'Pendleton'),
    'KPOE': (31.155278, -92.976111, 'Fort Polk'),
    'KPUX': (38.45955, -104.18135, 'Pueblo'),
    'KRAX': (35.665519, -78.48975, 'Raleigh Durham'),
    'KRGX': (39.754056, -119.46202, 'Reno'),
    'KRIW': (43.066089, -108.4773, 'Riverton'),
    'KRLX': (38.311111, -81.722778, 'Charleston'),
    'KRTX': (45.715039, -122.965, 'Portland'),
    'KSFX': (43.1056, -112.68613, 'Pocatello'),
    'KSGF': (37.235239, -93.400419, 'Springfield'),
    'KSHV': (32.450833, -93.84125, 'Shreveport'),
    'KSJT': (31.371278, -100.4925, 'San Angelo'),
    'KSOX': (33.817733, -117.636, 'Santa Ana Mountains'),
    'KSRX': (35.290417, -94.361889, 'Fort Smith'),
    'KTBW': (27.7055, -82.401778, 'Tampa'),
    'KTFX': (47.459583, -111.38533, 'Great Falls'),
    'KTLH': (30.397583, -84.328944, 'Tallahassee'),
    'KTLX': (35.333361, -97.277761, 'Oklahoma City'),
    'KTWX': (38.99695, -96.23255, 'Topeka'),
    'KTYX': (43.755694, -75.679861, 'Fort Drum'),
    'KUDX': (44.124722, -102.83, 'Rapid City'),
    'KUEX': (40.320833, -98.441944, 'Hastings'),
    'KVAX': (30.890278, -83.001806, 'Moody Afb'),
    'KVBX': (34.83855, -120.39791, 'Vandenberg Afb'),
    'KVNX': (36.740617, -98.127717, 'Vance Afb'),
    'KVTX': (34.412017, -119.17875, 'Los Angeles'),
    'KVWX': (38.26025, -87.724528, 'Evansville'),
    'KYUX': (32.495281, -114.65671, 'Yuma'),
    'LPLA': (38.73028, -27.32167, 'Lajes Ab'),
    'PABC': (60.791944, -161.87638, 'Bethel Faa'),
    'PACG': (56.852778, -135.52916, 'Sitka'),
    'PAEC': (64.511389, -165.295, 'Nome'),
    'PAHG': (60.725914, -151.35146, 'Anchorage'),
    'PAIH': (59.460767, -146.30344, 'Middleton Island'),
    'PAKC': (58.679444, -156.62944, 'King Salmon'),
    'PAPD': (65.035114, -147.50143, 'Fairbanks'),
    'PGUA': (13.455833, 144.811111, 'Andersen Afb Agana'),
    'PHKI': (21.893889, -159.5525, 'South Kauai'),
    'PHKM': (20.125278, -155.77777, 'Kamuela'),
    'PHMO': (21.132778, -157.18027, 'Molokai'),
    'PHWA': (19.095, -155.56888, 'South Shore'),
    'RKJK': (35.924167, 126.622222, 'Kunsan'),
    'RKSG': (37.207569, 127.285561, 'Camp Humphreys'),
    'RODN': (26.3078, 127.903469, 'Kadena'),
    'TJUA': (18.115667, -66.078167, 'San Juan'),
}


@lru_cache(maxsize=128)
def _radar_iem_eligible(lat, lon):
    if not (math.isfinite(lat) and math.isfinite(lon) and 20 < lat < 55 and -130 < lon < -60):
        return False
    inside = False
    x0, y0 = _RADAR_CONUS[-1]
    for x1, y1 in _RADAR_CONUS:
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            inside = not inside
        x0, y0 = x1, y1
    return inside


class _RadarSuperseded(Exception):
    """The current preference generation no longer owns this pass."""


class _RadarRevalidate(Exception):
    """Remembered newest tiles failed; rediscover the source in this pass."""


class _RadarUnchanged(Exception):
    """A discovery-only pass found the current complete scan."""


class _RadarBudget(Exception):
    """Yield unfinished history to a later pass; never bypass the shared limit."""


def _radar_zoom_for(lat):
    """Target 200 km across the short axis; the adapter applies its source bounds."""
    zoom = round(math.log2(156543.03392 * math.cos(math.radians(lat)) /
                          (RADAR_TARGET_METERS / RADAR_VIEWPORT_PX)))
    return max(RADAR_MIN_ZOOM, min(RADAR_MAX_ZOOM, zoom))


def _radar_viewport(lat, lon, zoom, size, height=None):
    """Pure Web Mercator viewport geometry; offsets refer to unwrapped world pixels."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2 ** zoom
    world = n * 256
    cx, cy = world_point(lat, lon, zoom)
    height = size if height is None else height
    left, top = cx - size / 2, cy - height / 2
    tiles = [(tx % n, ty, int(tx * 256 - left), int(ty * 256 - top))
             for ty in range(max(0, math.floor(top / 256)),
                             min(n, math.ceil((top + height) / 256)))
             for tx in range(math.floor(left / 256), math.ceil((left + size) / 256))]
    def latitude(y):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / world))))
    def longitude(x):
        return (x / world * 360) % 360 - 180
    bounds = dict(n=latitude(top), s=latitude(top + height),
                  e=longitude(left + size), w=longitude(left))
    return tiles, 156543.03392 * math.cos(math.radians(lat)) / n, bounds, (cx - left, cy - top)


def _radar_distance_unit(config):
    # Same Units/Distance setting used by observation_format.units for lightning.
    return 'mi' if str(_cfg(config, 'Units', 'Distance') or '').lower() in ('mi', 'miles') else 'km'


def _radar_scale(mpp, size, unit, max_fraction=.4):
    factor = 1609.344 if unit == 'mi' else 1000
    choices = [d for d in (5, 10, 20, 25, 50, 100, 150, 200, 250)
               if d * factor / mpp <= size * max_fraction]
    if not choices:  # extreme polar latitudes: no listed distance fits
        return dict(distDisp='0 ' + unit, meters=0, pixels=0, unit=unit), ()
    dist = max(choices)
    pixels = dist * factor / mpp
    bar = dict(distDisp=f'{dist} {unit}', meters=dist * factor, pixels=pixels, unit=unit)
    rings = tuple(dict(label=f'{dist * multiple} {unit}', px=pixels * multiple)
                  for multiple in (1, 2) if pixels * multiple <= size / math.sqrt(2))
    return bar, rings


def _radar_nexrad(lat, lon, unit):
    """Nearest WSR-88D and unrounded eligibility distance; station -> radar bearing."""
    nearest = None
    a = math.radians(lat)
    for ident, (site_lat, site_lon, name) in _NEXRAD_SITES.items():
        b, dl = math.radians(site_lat), math.radians(site_lon - lon)
        h = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dl / 2) ** 2
        meters = 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))
        if nearest is None or meters < nearest[0]:
            bearing = math.degrees(math.atan2(math.sin(dl) * math.cos(b),
                                  math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(dl))) % 360
            nearest = meters, ident, name, bearing
    if nearest is None or nearest[0] > 285 * 1609.344:
        return None
    meters, ident, name, bearing = nearest
    dist = round(meters / (1609.344 if unit == 'mi' else 1000))
    return dict(id=ident, name=name, distanceMeters=meters, distanceDisp=f'{dist} {unit}',
                bearing=('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')[int((bearing + 22.5) / 45) % 8])


def _radar_sites(station, bounds):
    """Cap by viewport distance; station distance only chooses the primary."""
    center = (math.degrees(math.atan(math.sinh((math.asinh(math.tan(math.radians(bounds['n'])))+math.asinh(math.tan(math.radians(bounds['s']))))/2))),
              (bounds['w']+(bounds['e']-bounds['w']) % 360/2+180) % 360-180)
    sites = [dict(id=ident, lat=lat, lon=lon, distanceMeters=distance_meters(*station, lat, lon),
                  viewportDistanceMeters=distance_meters(*center, lat, lon))
             for ident, (lat, lon, _) in _NEXRAD_SITES.items()
             if circle_intersects_bounds(lat, lon, RADAR_SITE_RANGE_METERS, bounds)]
    sites.sort(key=lambda s: (s['viewportDistanceMeters'], s['id']))
    return list(reversed(sites[:RADAR_SITE_MAX_COUNT])), len(sites)


@lru_cache(maxsize=512)
def _radar_covered_tiles(lat, lon, zoom, radius, tiles):
    def intersects(tile):
        x, y = tile[:2]
        n, w = world_inverse(x*256, y*256, zoom)
        south, e = world_inverse((x+1)*256, (y+1)*256, zoom)
        return circle_intersects_bounds(lat, lon, radius, dict(n=n, s=south, w=w, e=e))
    return tuple(tile for tile in tiles if intersects(tile))


def _radar_site_tiles(ctx, site):
    if site is None:
        return ctx['tiles']
    lat, lon, _ = _NEXRAD_SITES[site]
    return _radar_covered_tiles(lat,lon,ctx['zoom'],RADAR_SITE_RANGE_METERS,tuple(ctx['tiles']))


@lru_cache(maxsize=1)
def _radar_native_table_revision():
    from lib.radar_palette import _FILES
    return hashlib.sha256(b''.join((Path(__file__).parent/'data'/name).read_bytes()
                                  for name in sorted(_FILES.values()))).hexdigest()


@lru_cache(maxsize=8)
def _radar_revision_digest(remap_revision,basemap_revision,native_revision):
    # Render constants are code, fixed for a process. Compute their identity once
    # per revision, rather than serializing three palettes for every inventory stat.
    pixels=repr([(source,source_palette(source)) for source in sorted(_RADAR_SOURCES)])
    return hashlib.sha256(('tile-wire-visible-v41-1'+remap_revision+repr(_RADAR_RAMP)+
                          repr(_RADAR_SITE_RAMP)+pixels+native_revision+basemap_revision).encode()).hexdigest()[:12]


def _radar_render_revision(smooth=False):
    from lib.radar_basemap import version
    return _radar_revision_digest(REMAP_REVISION + (SMOOTH_REVISION if smooth else ""),version(),_radar_native_table_revision())


def _radar_sites_revision():
    return hashlib.sha256(json.dumps(_NEXRAD_SITES,sort_keys=True).encode()).hexdigest()[:12]


def _radar_tile_path(source, site, stamp, zoom, x, y, smooth=False):
    if isinstance(stamp, (int, float)):
        stamp = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M')
    return Path(RADAR_DIR) / 't' / _radar_render_revision(smooth) / source / (site or '-') / stamp / str(zoom) / str(x % 2**zoom) / f'{y}.png'


def _radar_disk_key(source, site, stamp, zoom, x, y, smooth=False):
    key = (source, site, _radar_stamp_text(stamp), zoom, x % 2**zoom, y)
    return key + (True,) if smooth else key


def _radar_tile_metadata(path, source):
    from PIL import Image
    with Image.open(path) as image:
        smooth = len(path.parents) > 5 and path.parents[5].name == _radar_render_revision(True)
        if image.format != 'PNG' or image.size != ((512,512) if smooth else (256,256)):
            raise ValueError('invalid cached tile')
        image.load()
        meta=json.loads(image.info['radarRemap'])
        if meta['revision'] != (SMOOTH_REVISION if smooth else REMAP_REVISION) or type(meta['remapped']) is not bool:
            raise ValueError('cached tile revision')
        for key in ('unmatchedColors','opaqueColors','unmatchedPixels','opaquePixels','ambiguousPixels'):
            if type(meta[key]) is not int or meta[key]<0: raise ValueError('cached tile counts')
        if (meta['opaquePixels']>65536 or meta['unmatchedColors']>meta['opaqueColors'] or
                meta['unmatchedPixels']>meta['opaquePixels'] or meta['ambiguousPixels']>meta['opaquePixels'] or
                meta['remapped'] != (meta['unmatchedPixels']<=.02*meta['opaquePixels'] and not meta['ambiguousPixels'])):
            raise ValueError('cached tile honesty')
        allowed={color[:3] for _,color in source_palette(source)}
        with image.convert('RGBA') as rgba:
            colors=rgba.getcolors(image.width*image.height)
            if any(color[3] and color[:3] not in allowed for _,color in colors): raise ValueError('cached tile palette')
            visible=sum(n for n,color in colors if color[3])
            if int(image.info['radarVisiblePixels'])!=visible:raise ValueError('cached tile visibility count')
            if not smooth and visible>meta['opaquePixels']-meta['unmatchedPixels']:
                raise ValueError('cached tile visibility')
        return meta


def _radar_grid(ctx, zoom=None, margin=0):
    zoom = ctx['zoom'] if zoom is None else zoom
    tiles, _, _, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'], zoom,
                                   RADAR_VIEWPORT_W * 2**(zoom-ctx.get('camera_zoom', zoom)) + margin*512,
                                   RADAR_VIEWPORT_H * 2**(zoom-ctx.get('camera_zoom', zoom)) + margin*512)
    return tiles


def _radar_site_pairs(ctx, ts):
    """Only reporting sites, no future scans or scans aged 15 minutes."""
    pairs = []
    for site in ctx['sites']:
        stamps = ctx['site_scans'].get(site['id'], ()) if site['reporting'] else ()
        stamp = next((t for t in reversed(stamps) if t <= ts), None)
        if stamp is not None and ts - stamp <= RADAR_SITE_MAX_AGE_SEC:
            pairs.append((site['id'], stamp))
    return tuple(pairs)


def _radar_frame(source, ts, ctx, pairs=None):
    return dict(ts=ts, stamp=datetime.fromtimestamp(ts, timezone.utc).strftime('%Y%m%d%H%M'),
                complete=False, levels={}, siteScans=[dict(id=site, ts=scan) for site,scan in pairs or ()])


@lru_cache(maxsize=512)
def _radar_stamp_text(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M') if isinstance(stamp,(int,float)) else stamp


def _radar_present(ctx, source, site, stamp, zoom, x, y):
    inventory = ctx['inventory']  # missing ownership is a bug, never a disk probe
    return _radar_disk_key(source,site,stamp,zoom,x,y,ctx.get('smooth',False)) in inventory


def _radar_tile_manifest(source, frames, ctx):
    """Exact disk availability; frame booleans use the viewport at each level.

    The newest mask is the union of available site tiles. Site scans retain their
    own stamps and are fetched/drawn in farthest-to-nearest order on the page.
    """
    levels = [z for z in (ctx['zoom']-1,ctx['zoom'],ctx['zoom']+1)
              if RADAR_MIN_ZOOM <= z <= _RADAR_SOURCES[source]['max_zoom']]
    px,py=world_point(ctx['center']['lat'],ctx['center']['lon'],ctx['zoom'])
    scale = 2**(ctx['zoom']-ctx.get('camera_zoom',ctx['zoom']))
    width, height = RADAR_VIEWPORT_W*scale, RADAR_VIEWPORT_H*scale
    x0,y0=math.floor((px-width/2)/256),max(0,math.floor((py-height/2)/256))
    grid=dict(x0=x0,y0=y0,w=math.ceil((px+width/2)/256)-x0,
              h=min(2**ctx['zoom'],math.ceil((py+height/2)/256))-y0)
    def inventory(frame,z,tiles):
        pairs=[(p['id'],p['ts']) for p in frame.get('siteScans',())] or [(None,frame['ts'])]
        cache = ctx.get('manifest_cache')
        index = ctx.get('inventory')
        key = (source, tuple(pairs), z, tuple(tiles), ctx.get('smooth',False))
        versions = tuple(index.groups.get((source,site,_radar_stamp_text(stamp),z),0) for site,stamp in pairs) if hasattr(index,'groups') else None
        if cache is not None and versions is not None:
            cached = cache.get(key)
            if cached is not None and cached[0] == versions:
                cache.move_to_end(key)
                return cached[1]
        availability=expected=complete=0
        for i,(x,y) in enumerate(tiles):
            required=[]
            for site,stamp in pairs:
                if site:
                    lat,lon,_=_NEXRAD_SITES[site]
                    n,w=world_inverse(x*256,y*256,z);south,e=world_inverse((x+1)*256,(y+1)*256,z)
                    if not circle_intersects_bounds(lat,lon,RADAR_SITE_RANGE_METERS,dict(n=n,s=south,w=w,e=e)):continue
                required.append(_radar_present(ctx,source,site,stamp,z,x,y))
            if required:expected|=1<<i
            if any(required):availability|=1<<i
            if required and all(required):complete|=1<<i
        result = availability,expected,complete
        if cache is not None and versions is not None:
            cache[key] = (versions, result)
            while len(cache) > 1024: cache.popitem(last=False)
        return result
    result=[]
    for frame in frames:
        present={}
        for z in levels:
            tiles=[(x,y) for x,y,_,_ in _radar_grid(ctx,z)]
            _,expected,complete=inventory(frame,z,tiles)
            present[str(z)]=bool(expected) and complete==expected
        result.append(dict(frame,levels=present))
    grid_tiles=[(x,y) for y in range(y0,y0+grid['h']) for x in range(x0,x0+grid['w'])]
    mask,expected,complete=inventory(result[-1],ctx['zoom'],grid_tiles) if result else (0,0,0)
    width=math.ceil(grid['w']*grid['h']/4)
    return dict(base='radar/t/',revision=_radar_render_revision(ctx.get('smooth',False)),
                remapRevision=SMOOTH_REVISION if ctx.get('smooth') else REMAP_REVISION,
                smooth=ctx.get('smooth',False), tileSize=512 if ctx.get('smooth') else 256,
                source=source,site=ctx.get('site_id') or '-',z=ctx['zoom'],levels=levels,grid=grid,
                camera=dict(ctx['center'], zoom=ctx.get('camera_zoom', ctx['zoom'])),
                geometry=[list(ctx.get('station', ctx['center'].values())), ctx['center'], ctx.get('camera_zoom',ctx['zoom']), ctx['zoom'], source, ctx.get('site_id')],
                intent=dict(ctx.get('intent', {})), publishedAt=time.time(),
                newest=dict(stamp=result[-1]['stamp'] if result else None,mask=f'{mask:0{width}x}',
                            expectedMask=f'{expected:0{width}x}',completeMask=f'{complete:0{width}x}'),frames=result)


# ==============================================================================
# PROVIDER SNAPSHOTS
# ==============================================================================
# Every provider (air quality, forecast, alerts) is fetched on a daemon thread
# while the emit tick reads the result from the main thread. Publishing field by
# field lets the tick observe a mix of old and new values (a new AQI beside the
# previous peak/trend). Each worker therefore builds one COMPLETE immutable
# result locally and publishes it with a single attribute assignment, and the
# tick reads that one reference once.
_RadarResult = namedtuple('_RadarResult',
    'available reason frames ts_frame center zoom mpp bounds scalebar rings nexrad ts_fetch '
    'source_id provider attribution attribution_url cadence stale_sec legend partial_coverage '
    'max_zoom zoom_desired zoom_auto_level geo source_mode site_id sources scanning_slowly sites sites_considered source_pref source_fallback tiles units scan_cadence_sec scan_mode scan_mode_source',
    defaults=('rainviewer', 'rainviewer', 'RainViewer', 'https://www.rainviewer.com/',
              RADAR_RAINVIEWER_FRAME_INTERVAL_SEC, RADAR_RAINVIEWER_STALE_SEC, _RADAR_RAMP, False,
              7, None, 7, None, 'mosaic', None, (), False, (), 0, 'mosaic', None, None, 'mi', None, None, None))
_RADAR_NONE = _RadarResult(False, 'no data yet', (), None, None, None, None, None,
                           None, None, None, None)

def _radar_scan_cadence(stamps):
    """Infer only from the primary listing, independent of fetched tile history."""
    recent = stamps[-4:]
    gaps = [b-a for a,b in zip(recent, recent[1:])]
    cadence = median(gaps) if gaps else None
    mode = None
    if len(gaps) == 3:
        if cadence <= 390:
            mode = 'precipitation'
        elif 540 <= cadence <= 900:
            mode = 'clear-air'
    return dict(scan_cadence_sec=cadence, scan_mode=mode,
                scan_mode_source='cadence' if mode else None,
                scanning_slowly=cadence is not None and cadence > 900)


_AqiResult = namedtuple('_AqiResult',
                        'aqi category pm25 ts forecast peak peak_time fc_cat trend trend_text')
_AQI_NONE = _AqiResult(None, None, None, None, (), None, None, None, None, None)

_FcResult = namedtuple('_FcResult', 'daily hourly ts')
_FC_NONE  = _FcResult((), (), None)

_AlertsResult = namedtuple('_AlertsResult', 'features alerts ts')
_ALERTS_NONE  = _AlertsResult(None, (), None)

_VerResult = namedtuple('_VerResult', 'available latest current')
_VER_NONE  = _VerResult(False, None, None)


def _snapshot_field(snapshot_attr, field):
    """ Expose one field of a provider snapshot as a plain attribute, for the
    callers (and tests) that seed or read a single value. Reads see the current
    snapshot; a write replaces the whole snapshot, so even a one-field
    assignment is published atomically. """
    def _read(self):
        return getattr(getattr(self, snapshot_attr), field)

    def _write(self, value):
        setattr(self, snapshot_attr, getattr(self, snapshot_attr)._replace(**{field: value}))

    return property(_read, _write)


# ==============================================================================
# EMITTER
# ==============================================================================
class AlmanacEmitter:
    """ Periodically snapshots the console's live Obs/Astro/Met/Sager/System
    DictProperties into a flat JSON file for the almanac HTML overlay.

    This is purely a read-side tap: it never writes back to the app's Kivy
    properties, so it cannot perturb the classic (or almanac) display path.
    """

    def __init__(self, screen, output_path=OUTPUT_PATH, interval=EMIT_INTERVAL):
        self.screen      = screen                      # AlmanacConditions instance
        self.app         = screen.app
        self.output_path = output_path
        self.interval    = interval
        self._event      = None
        self._radar_result = _RADAR_NONE
        self._radar_result_stamp = None
        self._radar_request_times = []  # ALL attempts, shared across sources and retries
        self._radar_negative = {}
        self._radar_newest = {}  # (source, site) -> (validated monotonic, knowledge)
        self._radar_archive_positive = set()  # immutable successful archive URLs
        self._radar_tiles = OrderedDict()
        self._radar_native_groups = {}
        self._radar_disk_files = 0
        self._radar_disk_bytes = 0
        self._radar_idle_context = None
        self._radar_warm_pending = False
        self._radar_prefetched = {}  # (source, zoom, centre) -> completed scan set
        self._radar_was_viewed = False
        self._radar_view_pending = False
        self._radar_view_session = None
        self._radar_view_geometry = None
        self._radar_geometry_since = 0.
        self._radar_geo_state = None
        self._radar_geo_idle = None
        self._radar_lock = _RLock()
        self._radar_session = None
        self._radar_provider = None
        self._radar_emit_pending = None
        self._radar_cooldowns = {}
        self._radar_transport_failures = {}
        self._radar_local_failure_streak = 0  # consecutive local-failure passes, for retry backoff
        self._radar_source_since = time.monotonic()
        self._radar_switch_reason = None
        self._radar_transport_retries = 0
        self._radar_stale_first_byte_retries = 0
        self._radar_health = HostHealth()
        self._radar_site_status = {}  # last listing evidence, independent of tile validity
        self._radar_discovery = DiscoverySchedule()
        self._radar_discovery_event = None
        self._radar_discovery_pending = False
        self._radar_probe_reuse = {}
        self._radar_metadata = {}
        self._radar_zoom_stamp = None
        self._radar_refresh = dict(state='idle', frameIndex=0, frameTotal=0)
        self._radar_pending = {}
        self._radar_acquisition_pending = False
        from lib.radar_cache import TileInventory
        self._radar_disk_inventory = TileInventory()
        self._radar_cache_ready = _Event()
        self._radar_cache_thread = None
        self._radar_manifest_cache = OrderedDict()
        self._radar_bad_stamp = None
        self._radar_phase_metrics = []
        self._radar_request_metrics = []
        self._radar_metadata_at = {}
        self._radar_restart = False
        # scheduling registry: EVERY handle we hand to Clock (intervals and
        # one-shots alike) so stop() can cancel all of them, plus the guards
        # that keep one failing provider from stacking work.
        self._events     = []        # live Clock handles
        self._running    = False     # fences callbacks belonging to a stopped instance
        self._life_lock  = _RLock()            # start/stop vs. worker-thread scheduling
        self._inflight   = set()     # provider keys with a fetch thread running
        self._retries    = {}        # provider key -> its ONE pending retry handle
        self._warned     = False
        # barograph 24h SLP series cache (refreshed every BARO_SERIES_TTL s so we
        # don't re-parse the 1440-point REST payload on every 2 s emit tick)
        self._baro_series_cache = []
        self._baro_series_t     = 0.0
        # rolling rain-rate window: the Tempest's haptic sensor reports drizzle
        # as an occasional 0.01 in minute with zeros between, so the raw
        # per-minute rate flickers 0 <-> trace and the gauge went dry mid-drizzle
        self._rain_win = _RainWindow(RAIN_WINDOW_SEC)
        self._started_at = time.time()   # obsAgeSec counts from here until the first observation

    # Provider results, each published as ONE snapshot by its worker thread.
    # Class-level so the "no data yet" state needs no instance setup.
    _aqi_result    = _AQI_NONE      # air quality, Open-Meteo or WAQI by lat/lon
    _fc_result     = _FC_NONE       # 7-day outlook, Open-Meteo by lat/lon
    _alerts_result = _ALERTS_NONE   # NWS alerts, api.weather.gov by lat/lon
    _ver_result    = _VER_NONE      # GitHub release check

    # Snapshot fields, readable/writable one at a time (see _snapshot_field).
    _radar_available = _snapshot_field('_radar_result', 'available')
    _radar_reason = _snapshot_field('_radar_result', 'reason')
    _radar_frames = _snapshot_field('_radar_result', 'frames')
    _radar_ts_frame = _snapshot_field('_radar_result', 'ts_frame')
    _radar_center = _snapshot_field('_radar_result', 'center')
    _radar_zoom = _snapshot_field('_radar_result', 'zoom')
    _radar_mpp = _snapshot_field('_radar_result', 'mpp')
    _radar_bounds = _snapshot_field('_radar_result', 'bounds')
    _radar_scalebar = _snapshot_field('_radar_result', 'scalebar')
    _radar_rings = _snapshot_field('_radar_result', 'rings')
    _radar_nexrad = _snapshot_field('_radar_result', 'nexrad')
    _radar_ts_fetch = _snapshot_field('_radar_result', 'ts_fetch')
    _aqi            = _snapshot_field('_aqi_result', 'aqi')
    _aqi_category   = _snapshot_field('_aqi_result', 'category')
    _aqi_pm25       = _snapshot_field('_aqi_result', 'pm25')
    _aqi_ts         = _snapshot_field('_aqi_result', 'ts')          # last SUCCESSFUL fetch (staleness)
    _aqi_forecast   = _snapshot_field('_aqi_result', 'forecast')    # [[epoch, us_aqi], ...] next hours
    _aqi_peak       = _snapshot_field('_aqi_result', 'peak')        # max us_aqi over the next 6 h
    _aqi_peak_time  = _snapshot_field('_aqi_result', 'peak_time')   # station-local hour of the peak
    _aqi_fc_cat     = _snapshot_field('_aqi_result', 'fc_cat')      # AQI category at the peak
    _aqi_trend      = _snapshot_field('_aqi_result', 'trend')       # 'rising' | 'falling' | 'steady'
    _aqi_trend_text = _snapshot_field('_aqi_result', 'trend_text')  # "Moderate by 5 PM" | "Improving"
    _fc_daily       = _snapshot_field('_fc_result', 'daily')        # [{day,hi,lo,code,pp}, ...]
    _fc_hourly      = _snapshot_field('_fc_result', 'hourly')       # [[epoch, temp], ...] hero curve
    _fc_ts          = _snapshot_field('_fc_result', 'ts')
    _alert_features = _snapshot_field('_alerts_result', 'features') # last-good NWS properties
    _alerts         = _snapshot_field('_alerts_result', 'alerts')   # processed + collapsed + sorted
    _alerts_ts      = _snapshot_field('_alerts_result', 'ts')
    _update_available = _snapshot_field('_ver_result', 'available')
    _latest_version   = _snapshot_field('_ver_result', 'latest')
    _current_version  = _snapshot_field('_ver_result', 'current')

    def start(self):
        """ Schedule the periodic emit and the provider polls. Idempotent -
        calling twice (e.g. if add_panels() is re-invoked by a PanelCount
        change) cancels every handle from the previous run rather than
        stacking a second set of timers. """
        with self._life_lock:
            self.stop()
            self._running = True
            try:
                os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
            except OSError as error:
                Logger.warning(f'almanac_emit: could not create output directory - {error}')
            self._event = self._schedule(self._emit, self.interval, interval=True)
            # update check: soon after start, then periodically (off the main thread)
            self._schedule(self._check_version, 8)
            self._schedule(self._check_version, VERSION_CHECK_INTERVAL, interval=True)
            # air quality: after the USB wifi has settled post-boot, then periodically
            self._schedule(self._check_aqi, 30)
            self._schedule(self._check_aqi, AQI_CHECK_INTERVAL, interval=True)
            # weather alerts: staggered a little after AQI, then periodically
            self._schedule(self._check_alerts, 40)
            self._schedule(self._check_alerts, ALERTS_CHECK_INTERVAL, interval=True)
            # 7-day outlook: staggered after alerts, then hourly
            self._schedule(self._check_forecast, 50)
            self._schedule(self._check_forecast, FORECAST_CHECK_INTERVAL, interval=True)
            self._radar_start_inventory()
            self._schedule(self._check_radar, 60)
            self._radar_zoom_stamp = self._radar_preference_stamp()
            self._schedule(self._check_radar_zoom, RADAR_INTENT_CHECK_SEC, interval=True)
            self._schedule(self._check_radar_geo, RADAR_GEO_QUANTUM_SEC, interval=True)
            return self._event

    def stop(self):
        """ Cancel every scheduled handle, including the boot one-shots and any
        pending provider retry, and fence the callbacks that are already due.
        Held under the lifecycle lock so a worker thread that is mid-way through
        arming a retry cannot slip a handle in after the registry is cleared. """
        with self._life_lock:
            self._running = False
            for handle in self._events:
                try:
                    handle.cancel()
                except Exception:                                             # noqa: BLE001
                    pass
            self._events = []
            self._retries.clear()
            self._event = None
            self._radar_emit_pending = None
            self._radar_discovery_event = None
            self._radar_discovery_pending = False
            if self._radar_session is not None and 'radar' not in self._inflight:
                self._radar_session.close()
                self._radar_session = None

    def _schedule(self, callback, timeout, interval=False):
        """ Schedule through the registry, so stop() reaches every handle. The
        callback is fenced: a stopped instance's timer does nothing and (by
        returning False) unschedules itself, and a fired one-shot leaves the
        registry so a long run cannot accumulate dead handles. """
        handles = []

        def _fenced(dt):
            if not interval and handles:
                try:
                    self._events.remove(handles[0])
                except ValueError:
                    pass
            if not self._running:
                return False
            return callback(dt)

        with self._life_lock:
            if not self._running:
                return None                      # stopped between the caller's check and here
            handle = (Clock.schedule_interval if interval else Clock.schedule_once)(_fenced, timeout)
            handles.append(handle)
            self._events.append(handle)
            return handle

    def _spawn(self, key, worker):
        """ Run a provider fetch on a daemon thread, at most ONE per provider: a
        slow or hung request must not stack a second behind it, and the poll
        interval must not overtake a retry that is already running. """
        with self._life_lock:
            if not self._running or key in self._inflight:
                return
            self._inflight.add(key)

        def _run():
            try:
                worker()
            finally:
                with self._life_lock:
                    self._inflight.discard(key)
                    if key == 'radar':
                        if self._radar_restart:
                            self._schedule(self._check_radar_zoom, 0)
                        elif self._radar_acquisition_pending:
                            self._schedule(self._check_radar, 0)

        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            self._inflight.discard(key)

    def _schedule_retry(self, key, callback, timeout):
        """ Arm the ONE pending retry a provider is allowed. Without this, every
        failure of a periodic poll starts its own retry chain and the chains
        multiply for as long as the network is down. """
        def _retry(dt):
            self._retries.pop(key, None)
            callback(dt)

        with self._life_lock:
            if not self._running or self._retries.get(key) is not None:
                return
            handle = self._schedule(_retry, timeout)
            if handle is not None:
                self._retries[key] = handle

    def _check_radar(self, _dt=None):
        with self._life_lock:
            if 'radar' in self._inflight:
                self._radar_acquisition_pending = True
                return
            self._radar_acquisition_pending = False
            if self._radar_discovery.due is not None and self._radar_discovery.due <= time.time():
                self._check_radar_discovery(_dt)
                return
            self._spawn('radar', lambda: self._do_radar(intent_triggered=False))

    def _radar_arm_discovery(self, min_delay=0):
        """One readiness wakeup, separate from repair/warming retries and emit."""
        with self._life_lock:
            old = self._radar_discovery_event
            if old is not None:
                old.cancel()
                if old in self._events:
                    self._events.remove(old)
            self._radar_discovery_event = None
            if not self._running:
                return
            now = time.time()
            plan = self._radar_discovery
            plan.observe(self._radar_result, now, RADAR_IEM_READY_LAG_SEC)
            due = plan.due if plan.due is not None else now + RADAR_RETRY_SEC
            delay = max(1, min_delay, due-now)
            source = self._radar_result.source_id
            probe = self._radar_probe_delay()
            if probe is not None:
                # Preserve recovery of a failed preferred source while on fallback.
                delay = max(min_delay, 1, probe)
            delay = max(delay, self._radar_headroom_delay(source, 1))
            plan.due = now + delay
            self._radar_discovery_event = self._schedule(self._check_radar_discovery, delay)

    def _check_radar_discovery(self, _dt=None):
        with self._life_lock:
            # A repair/history retry can reach the same deadline first. It
            # becomes discovery and consumes the pending readiness handle too.
            old = self._radar_discovery_event
            if old is not None:
                old.cancel()
                if old in self._events:
                    self._events.remove(old)
            self._radar_discovery_event = None
            if 'radar' in self._inflight:
                self._radar_discovery_pending = True
                self._radar_arm_discovery(min_delay=5)
                return
            self._radar_discovery_pending = False
            self._radar_discovery.started(time.time())
            self._spawn('radar', lambda: self._do_radar(intent_triggered=False, discovery=True))

    def _radar_discovery_unchanged(self, source, newest, ctx, validated=None):
        snap = self._radar_result
        if source == 'iem-nexrad-n0b' and (snap.site_id != ctx.get('site_id') or
                not snap.frames or [(p['id'], p['ts']) for p in snap.frames[-1]['siteScans']]
                != _radar_site_pairs(ctx, newest)):
            return  # a secondary layer may advance between primary volumes
        if (ctx.get('discovery') and snap.source_id == source and snap.ts_frame == newest
                and self._radar_result_stamp == ctx.get('preference_stamp')
                and snap.frames and snap.frames[-1]['complete']
                and not any(self._radar_pending.get(k) for k in ('newest','four','eight'))
                and (not ctx.get('viewed') or all(f['complete'] for f in snap.frames[-RADAR_LOOP_FRAMES:]))):
            if validated is not None:
                # The complete current scan has already passed tile validation.
                # Refresh intent/prefetch knowledge even though no build runs.
                self._radar_newest[(source, None)] = (time.monotonic(), validated)
            raise _RadarUnchanged()

    def _radar_health_payload(self):
        health = self._radar_health.snapshot()
        health['phases'] = list(self._radar_phase_metrics)
        health['requests'] = list(self._radar_request_metrics)
        health['pending'] = dict(self._radar_pending)
        health['discovery'] = self._radar_discovery.telemetry(time.time(), self._radar_result.ts_frame)
        return health

    def _radar_probe_delay(self):
        # Recover the active/preferred chain. An expired breaker belonging to
        # an unused fallback must not turn healthy discovery into 1-second polls.
        snap = self._radar_result
        sources = {snap.source_id}
        if snap.source_id == 'rainviewer':
            sources.add('iem-mrms-lcref')
        if snap.source_pref == 'site' and not snap.source_fallback:
            sources.add('iem-nexrad-n0b')
        probe = self._radar_health.probe_delay(sources)
        now = time.monotonic()
        delays = [until-now for source, until in self._radar_cooldowns.items()
                  if source in sources and until > now]
        if probe is not None:
            delays.append(probe)
        return min(delays) if delays else None

    def _radar_read_intent(self):
        try:
            record = json.loads(Path(os.path.join(os.path.dirname(self.output_path), 'radar_intent')).read_text())
            if not isinstance(record, dict): return None  # legacy startup marker
            seq, zoom, source, center = (record[k] for k in ('seq','zoom','source','center'))
            if type(seq) is not int or not 0 <= seq <= 999999999999: return None
            if zoom != 'auto' and (type(zoom) is not int or not RADAR_MIN_ZOOM <= zoom <= 10): return None
            if source not in ('site','mosaic'): return None
            if center != 'station':
                if (not isinstance(center,dict) or type(center.get('lat')) not in (int,float)
                        or type(center.get('lon')) not in (int,float)
                        or not -85.05112878 <= center['lat'] <= 85.05112878 or not -180 <= center['lon'] <= 180): return None
            return record
        except (OSError, ValueError, KeyError, TypeError): return None

    def _radar_stamp_names(self):
        """Which marker files carry intent right now (one JSON parse)."""
        record = self._radar_read_intent()
        return ('radar_intent', 'radar_smooth') if record is not None else ('radar_zoom', 'radar_source', 'radar_center', 'radar_intent', 'radar_smooth')

    def _radar_preference_stamp(self, names=None):
        # Supersede checkpoints run at every tile boundary. A pass hands them the file
        # set decided at its start so each checkpoint is one stat per file, not a JSON
        # parse: on the Pi the parse-per-checkpoint was ~90 SD-card reads, 1.7 s of a
        # 2.4 s zoom pass. Callers without a pass context still parse (watcher, publish).
        stamps = []
        for name in names or self._radar_stamp_names():
            try:
                stat = os.stat(os.path.join(os.path.dirname(self.output_path), name))
                stamps.append((stat.st_ino, stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    def _check_radar_zoom(self, _dt=None):
        # Geometry can publish while the single-flight transport worker drains.
        # Keep only the newest intent for network work; share all request budgets.
        self._radar_consume_bad_tiles()
        stamp = self._radar_preference_stamp()
        viewed = self._radar_is_viewed()
        # The demand hint lasts 15 minutes; the live session also catches a
        # return inside that window, without making every poll a new event.
        session = None
        try:
            record = json.loads(Path(self.output_path).with_name('radar_viewing').read_text())
            if 0 <= time.time()-record['last'] < RADAR_VIEW_POLL_GAP_SEC:
                session = record['since']
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if viewed and (not self._radar_was_viewed or
                       session is not None and session != self._radar_view_session):
            self._radar_view_pending = True
        self._radar_was_viewed = viewed
        self._radar_view_session = session
        if not viewed:
            self._radar_view_pending = False
        if self._running and (stamp != self._radar_zoom_stamp or self._radar_view_pending):
            if 'radar' not in self._inflight:
                view_started = self._radar_view_pending
                self._radar_view_pending = False
                self._radar_zoom_stamp = stamp
                self._spawn('radar', lambda: self._do_radar(intent_triggered=True, view_started=view_started))
        elif self._running and self._radar_acquisition_pending and 'radar' not in self._inflight:
            self._check_radar()
        elif self._running and viewed and self._radar_warm_pending and 'radar' not in self._inflight:
            self._radar_warm_pending = False
            self._spawn('radar', self._radar_resume_warm)

    def _radar_resume_warm(self):
        if self._radar_idle_context is None:
            return
        source, warm = self._radar_idle_context
        warm = dict(warm, viewed=True, refresh=dict(state='idle'),
                    deadline=time.monotonic()+RADAR_BUILD_DEADLINE_SEC)
        try:
            if self._radar_session is not None:
                self._radar_session.begin_pass(warm['deadline'])
            self._radar_prefetch(source, warm)
        except _RadarSuperseded:
            pass  # the watcher owns the newer camera/source
        finally:
            self._radar_log_pass(warm['deadline']-RADAR_BUILD_DEADLINE_SEC)

    def _check_radar_geo(self, _dt=None):
        if not self._running or 'geo' in self._inflight:
            return
        from lib.radar_basemap import version
        config = getattr(self.app, 'config', {}) or {}
        try:
            stat = Path(self.output_path).with_name('radar_activity').stat()
            activity_stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            activity_stamp = None
        token = (_cfg(config, 'Station', 'Latitude'), _cfg(config, 'Station', 'Longitude'),
                 version(), activity_stamp, self._radar_is_viewed())
        if token != self._radar_geo_idle:
            self._spawn('geo', lambda: self._radar_geo_work(token))

    def _radar_geo_work(self, token=None):
        # No radar/session/result lock or transport context: home warming starts
        # with the engine, including before its first scheduled radar fetch.
        from lib.radar_basemap import WarmState, warm
        config = getattr(self.app, 'config', {}) or {}
        station = (_num(_cfg(config, 'Station', 'Latitude')),
                   _num(_cfg(config, 'Station', 'Longitude')))
        lat, lon = station
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            self._radar_geo_idle = token
            return
        activity = {}
        try:
            record = json.loads(Path(self.output_path).with_name('radar_activity').read_text())
            if isinstance(record, dict):
                activity = record
        except (OSError, ValueError):
            pass
        # Motion suppresses both queues, even if the last report is old. Only a
        # subsequent settled report (or removal of the marker) clears it.
        if activity.get('moving') and 0 <= time.time()-activity.get('at', 0) < 5:
            self._radar_geo_idle = token
            return
        center, zoom = None, None
        theme = activity.get('theme', 'paper')
        if theme not in ('paper', 'night'):
            theme = 'paper'
        viewed = self._radar_is_viewed()
        try:
            candidate, level = activity['center'], activity['zoom']
            if (viewed and 0 <= time.time()-activity['at'] < 5
                    and type(level) is int and 4 <= level <= 10
                    and isinstance(candidate, dict)
                    and type(candidate.get('lat')) in (int, float)
                    and type(candidate.get('lon')) in (int, float)
                    and -85.05112878 <= candidate['lat'] <= 85.05112878
                    and -180 <= candidate['lon'] <= 180):
                center, zoom = candidate, level
        except (KeyError, TypeError):
            pass
        if self._radar_geo_state is None:
            self._radar_geo_state = WarmState()
        try:
            made = warm(RADAR_DIR, station, center, zoom, _radar_zoom_for(lat), theme,
                        state=self._radar_geo_state)
            if not made and not self._radar_geo_state.home:
                self._radar_geo_idle = token
            if made and center is None:
                time.sleep(RADAR_GEO_UNVIEWED_SLEEP_SEC)
        except (OSError, ValueError, ImportError) as error:
            Logger.warning(f'almanac_emit: geography warming failed - {error}')

    def _radar_inventory_valid(self,snap):
        grid=(snap.tiles or {}).get('grid')
        if not grid:return False
        for frame in snap.frames[-8:]:
            pairs=[(p['id'],p['ts']) for p in frame.get('siteScans',())] or [(None,frame['ts'])]
            expected=False
            for y in range(grid['y0'],grid['y0']+grid['h']):
                for x in range(grid['x0'],grid['x0']+grid['w']):
                    for site,stamp in pairs:
                        if site:
                            lat,lon,_=_NEXRAD_SITES[site];n,w=world_inverse(x*256,y*256,snap.zoom);south,e=world_inverse((x+1)*256,(y+1)*256,snap.zoom)
                            if not circle_intersects_bounds(lat,lon,RADAR_SITE_RANGE_METERS,dict(n=n,s=south,w=w,e=e)):continue
                        expected=True
                        if _radar_disk_key(snap.source_id,site,stamp,snap.zoom,x,y,snap.tiles.get('smooth',False)) not in self._radar_disk_inventory:return False
            if not expected:return False
        return True

    def _radar_checkpoint(self, ctx):
        if 'preference_stamp' in ctx and ctx['preference_stamp'] != self._radar_preference_stamp(ctx.get('stamp_names')):
            raise _RadarSuperseded('radar intent changed')
        if self._radar_discovery_pending and (ctx.get('prefetch') or ctx.get('request_reserve')):
            raise _RadarBudget('radar warming yielded to readiness discovery')
        if ctx.get('deep_history') and self._radar_deep_view_delay(ctx) != 0:
            raise _RadarBudget('radar continuous view ended')
        if ctx.get('prefetch') and not self._radar_is_viewed():
            raise _RadarBudget('radar tab no longer viewed')

    def _radar_is_viewed(self):
        try:
            with open(os.path.join(os.path.dirname(self.output_path), 'radar_viewed')) as marker:
                age = time.time() - float(marker.read(128))
            return 0 <= age < RADAR_VIEW_TTL
        except (OSError, ValueError, UnicodeError):
            return False

    def _radar_deep_view_delay(self, ctx):
        """Recent continuous loopback viewing AND residence at this geometry."""
        try:
            marker = Path(self.output_path).with_name('radar_viewing')
            viewing = json.loads(marker.read_text())
            since, last = viewing['since'], viewing['last']
            now = time.time()
            if not (0 <= now-last < RADAR_VIEW_POLL_GAP_SEC and since <= last):
                return None
        except (OSError, ValueError, TypeError, KeyError):
            return None
        geometry = (ctx['identity'], ctx['preference_stamp'])
        with self._radar_lock:
            if geometry != self._radar_view_geometry:
                return None
            duration = min(now-since, time.monotonic()-self._radar_geometry_since)
        return max(0, RADAR_DEEP_VIEW_SEC-duration)

    def _radar_emit_now(self):
        # Worker publications replace immutable snapshots. wx.json is built by
        # the normal two-second emit tick, not once per tile or cache hit.
        return

    def _radar_publish_refresh(self, ctx, snapshot=None, **changes):
        self._radar_checkpoint(ctx)
        refresh = dict(state='newest', frameIndex=0, frameTotal=1)
        refresh.update(ctx.get('refresh', {}))
        refresh.update({k:v for k,v in changes.items() if k in refresh})
        phase = refresh['state']
        marks = ctx.setdefault('milestones', set())
        for name, reached in (('listings', refresh['frameTotal']>1), ('fourServerFrames', refresh['frameIndex']>=4), ('eightServerFrames', refresh['frameIndex']>=8)):
            if reached and name not in marks:
                marks.add(name)
                self._radar_phase_metrics.append(dict(phase=name,at=time.monotonic(),cpuSec=time.process_time(),
                    intent=dict(ctx.get('intent',{})),requests=len(self._radar_request_times),bytes=getattr(self,'_radar_received_bytes',0)))
        if ctx.get('metric_phase') != phase:
            ctx['metric_phase'] = phase
            self._radar_phase_metrics.append(dict(phase=phase, at=time.monotonic(), cpuSec=time.process_time(),
                intent=dict(ctx.get('intent', {})), requests=len(self._radar_request_times),
                bytes=getattr(self, '_radar_received_bytes', 0)))
            self._radar_phase_metrics = self._radar_phase_metrics[-128:]
        refresh.update(reason='not reporting' if ctx.get('source_fallback') == 'site-not-reporting' else None,
                       intent=dict(ctx.get('intent', {})), pending=dict(self._radar_pending),
                       nextRetry=getattr(self, '_radar_next_retry', None))
        ctx['refresh'] = refresh
        with self._radar_lock:
            if snapshot is not None:
                self._radar_result = snapshot
            self._radar_refresh = dict(refresh)
        self._radar_emit_now()

    def _radar_headroom_delay(self, source, needed):
        with self._radar_lock:
            now = time.monotonic()
            self._radar_request_times = sorted(t for t in self._radar_request_times if now-t < 60)
            count = len(self._radar_request_times)
            missing = count + needed - RADAR_REQUESTS_PER_MIN
            window = self._radar_request_times[min(missing, count)-1]+60-now if missing > 0 and count else 0
            return max(0, window, self._radar_cooldowns.get(source, 0)-now)

    def _radar_budget_retry(self, source, needed, min_delay=0):
        delay = max(min_delay, self._radar_headroom_delay(source, needed))
        # Build/deadline yields with free transport resume on the next watcher.
        delay = delay if delay > 0 else 2
        with self._life_lock:
            old = self._retries.pop('radar', None)
            if old is not None:
                old.cancel()
                if old in self._events: self._events.remove(old)
        self._radar_next_retry = time.time()+delay
        self._radar_refresh = dict(self._radar_refresh, nextRetry=self._radar_next_retry, pending=dict(self._radar_pending))
        self._schedule_retry('radar', self._check_radar, delay)

    def _radar_note_source(self, source, ctx):
        old = self._radar_result
        if old.available and old.source_id != source:
            self._radar_source_since = time.monotonic()
            self._radar_switch_reason = ctx.get('switch_reason', 'initial source selection')
            Logger.info(f'almanac_emit: radar source SWITCH {old.source_id} -> {source}; '
                        f'reason={self._radar_switch_reason}')

    def _radar_log_pass(self, started):
        health = self._radar_health_payload()
        health.update(source=self._radar_result.source_id, zoom=self._radar_result.zoom,
                      switchReason=self._radar_switch_reason,
                      elapsedSec=round(time.monotonic()-started, 3))
        Logger.info('almanac_emit: radar pass '+json.dumps(health, sort_keys=True))

    def _radar_retained_refresh(self, state):
        snap = self._radar_result
        with self._radar_lock:
            self._radar_refresh = dict(state=state, frameIndex=sum(f['complete'] for f in snap.frames),
                                      frameTotal=len(snap.frames), intent=dict(self._radar_refresh.get('intent', {})),
                                      reason='not reporting' if snap.source_fallback == 'site-not-reporting' else None,
                                      pending=dict(self._radar_pending), nextRetry=getattr(self, '_radar_next_retry', None))
        self._radar_emit_now()

    def _radar_request_gate(self, source, deadline, reserve=0):
        with self._radar_lock:
            now = time.monotonic()
            self._radar_request_times = [t for t in self._radar_request_times if now - t < 60]
            if now >= deadline:
                raise TimeoutError('radar acquisition deadline')
            if (now < self._radar_cooldowns.get(source, 0) or
                    len(self._radar_request_times) >= RADAR_REQUESTS_PER_MIN - reserve):
                raise _RadarBudget('radar request budget/cooldown')
            self._radar_request_times.append(now)
        return now

    def _radar_transport_retry(self, source, deadline, reserve=0, first_byte=False):
        self._radar_request_gate(source, deadline, reserve)
        with self._radar_lock:
            self._radar_transport_retries += 1
            self._radar_stale_first_byte_retries += int(first_byte)
            count = self._radar_transport_retries
            first_byte_count = self._radar_stale_first_byte_retries
        with self._radar_health.lock:
            self._radar_health.retries += 1
        Logger.info(f'almanac_emit: radar {source} stale connection retry; transport_retries={count}; '
                    f'stale_first_byte_retries={first_byte_count}')

    def _radar_request(self, source, url, deadline, method='GET', metadata=False, reserve=0, attempt=None, retry=False):
        """Validated transport; every attempt uses one monotonic rate/cooldown gate."""
        import urllib.request
        import urllib.error
        from email.utils import parsedate_to_datetime
        if metadata:
            with self._radar_lock:
                probed = self._radar_probe_reuse.pop(url, None)
            if probed is not None:
                return probed  # this pass already paid for the half-open probe
        # Host and rate admission are atomic; an open host costs no budget and
        # a denied rate slot must not strand a half-open probe.
        with self._radar_health.lock:
            if attempt is not None:
                attempt.check()
            probe = self._radar_health.admit(source, url, metadata)
            try:
                self._radar_request_gate(source, deadline, reserve)
            except Exception:
                if probe:
                    self._radar_health._host(source, url)['probe'] = False
                raise
        if retry:
            with self._radar_health.lock:
                if attempt is not None and attempt.hedged:
                    attempt.issued = True
                    self._radar_health.issue_hedge(stall=attempt.stall_hedge)
                else:
                    self._radar_health.retries += 1
        headers = {'User-Agent': 'WeatherFlow-PiConsole-almanac'}
        cached = self._radar_metadata.get(url)
        if metadata:
            headers['Cache-Control'] = 'no-cache'
            if cached:
                headers.update(cached[1])
        request_started, request_cpu = time.monotonic(), time.thread_time()
        outcome, byte_count = 'success', 0
        req = urllib.request.Request(url, headers=headers, method=method)
        req.radar_retry_failure = lambda error: self._radar_health.record(source, url, False, error)
        req.radar_retry_check = lambda: self._radar_health.admit(source, url)
        if attempt is not None or probe:
            req.radar_attempt = attempt or Attempt(fresh=True)
        try:
            timeout = RADAR_HTTP_TIMEOUT_SEC if metadata or method == "HEAD" else RADAR_TILE_TIMEOUT_SEC
            with self._radar_session.open(req, timeout=min(timeout, deadline - time.monotonic())) as response:
                if getattr(response, 'status', 200) != 200:
                    raise ValueError('unexpected radar HTTP status')
                raw = response.read(2 * 1024 * 1024 + 1) if method == 'GET' else b''
                with self._radar_lock:
                    byte_count = len(raw)
                    self._radar_received_bytes = getattr(self, '_radar_received_bytes', 0)+byte_count
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('oversized radar response')
                if time.monotonic() >= deadline:
                    raise TimeoutError('radar response exceeded deadline')
                if metadata:
                    validators = {}
                    response_headers = getattr(response, 'headers', {})
                    for name, request_name in [('ETag', 'If-None-Match'), ('Last-Modified', 'If-Modified-Since')]:
                        if response_headers.get(name):
                            validators[request_name] = response_headers[name]
                    self._radar_metadata[url] = (raw, validators)
                    self._radar_metadata_at[url] = time.monotonic()
                    while len(self._radar_metadata) > 128:
                        victim = next(iter(self._radar_metadata))
                        self._radar_metadata.pop(victim, None); self._radar_metadata_at.pop(victim, None)
                if attempt is not None:
                    attempt.check()
                    self._radar_validate_tile(raw, source)
                self._radar_health.record(source, url, True, probe=probe)
                return raw
        except urllib.error.HTTPError as error:
            outcome = 'http-'+str(error.code)
            if error.code == 304 and metadata and cached:
                self._radar_health.record(source, url, True, probe=probe)
                return cached[0]
            self._radar_health.record(source, url, error.code == 404, error, probe=probe)
            if error.code == 429:
                retry = error.headers.get('Retry-After', '') if error.headers else ''
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = RADAR_RETRY_SEC
                if not math.isfinite(delay):
                    delay = RADAR_RETRY_SEC
                self._radar_cooldowns[source] = time.monotonic() + max(1, delay)
                raise _RadarBudget('radar provider rate limited') from error
            raise
        except Exception as error:
            outcome = failure_class(error)
            if attempt is not None and attempt.cancelled.is_set():
                if attempt.discarded:
                    error = AttemptCancelled("discarded radar attempt")
                elif not attempt.waiting_response:
                    error = LocalTransportError("radar connection setup exceeded tile deadline")
                else:
                    error = TimeoutError("radar response exceeded tile deadline")
            if not getattr(req, "radar_gate_failed", False):
                self._radar_health.record(source, url, False, error, probe=probe)
            # Validation errors occur after open(); discard that untrusted pool.
            # Transport errors already discarded their own lease only.
            if isinstance(error, ValueError):
                self._radar_session.discard(url)
            raise
        finally:
            with self._radar_lock:
                self._radar_request_metrics.append(dict(at=request_started,elapsedSec=time.monotonic()-request_started,
                    cpuSec=time.thread_time()-request_cpu,source=source,method=method,bytes=byte_count,
                    failureClass=outcome,queueWaitSec=getattr(req,'radar_queue_wait',0)))
                self._radar_request_metrics=self._radar_request_metrics[-128:]

    @staticmethod
    def _radar_validate_tile(raw, source):
        from PIL import Image
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("radar response is not PNG")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (256, 256):
                raise ValueError("unexpected radar tile size/format")
            image.load()
            if source.startswith("iem-"):
                with image.convert("RGBA") as rgba:
                    if rgba.getextrema() == ((255,255),(0,0),(0,0),(255,255)):
                        raise ValueError("IEM placeholder tile")

    def _radar_tile_batch(self, source, stamp, ctx, deadline, url, site):
        """Six newest / four background requests; retain successes on cancellation."""
        from PIL import Image
        workers = ctx.get('tile_workers', RADAR_TILE_WORKERS)
        interactive = workers == RADAR_NEWEST_TILE_WORKERS and not ctx.get('prefetch')
        hedge_budget = ctx.setdefault('hedge_budget', dict(count=0, limit=len(ctx['tiles'])//2))
        def claim_hedge(tile_url):
            # Same lock order as wire admission: health -> rate gate -> pool.
            with self._radar_health.lock, self._radar_lock:
                reserve = ctx.get('request_reserve', 0)
                if (not self._radar_health.hedge_allowed() or
                        hedge_budget['count'] >= hedge_budget['limit'] or
                        self._radar_headroom_delay(source, reserve+1)):
                    return False
                lease = self._radar_session.reserve_hedge(tile_url)
                if lease is None:
                    return False
                hedge_budget['count'] += 1
                return lease
        def discarded(count):
            self._radar_health.discard_hedges(count)

        def fetch(tile):
            tx, ty, _, _ = tile
            target = _radar_tile_path(source, site, stamp, ctx['zoom'], tx, ty, ctx.get('smooth',False))
            disk_key = _radar_disk_key(source,site,stamp,ctx['zoom'],tx,ty,ctx.get('smooth',False))
            if disk_key in self._radar_disk_inventory:
                return tile, None  # bytes/metadata already validated; page decodes
            # Native IEM bytes have no dependency on our remapping revision.
            # RainViewer's server-side colour scheme/options DO affect raw bytes.
            native = (RADAR_RAINVIEWER_COLOR, RADAR_RAINVIEWER_TILE_OPTS) if source == 'rainviewer' else None
            key = (source, site, native, stamp, ctx['zoom'], tx, ty)
            with self._radar_lock:
                raw = self._radar_tiles.get(key)
                if raw is not None:
                    self._radar_tiles.move_to_end(key)
            self._radar_checkpoint(ctx)
            reserve = ctx.get('request_reserve', RADAR_HISTORY_RESERVE if ctx.get('prefetch') else 0)
            options = dict(reserve=reserve) if reserve else {}
            if raw is None:
                switch_work = bool(ctx.get('intent', {}).get('camera')) and not ctx.get('prefetch') and not ctx.get('deep_history')
                tile_deadline = min(deadline, time.monotonic()+(2 if switch_work else 2*RADAR_TILE_TIMEOUT_SEC))
                def request(control, retry):
                    # Count hedges at wire admission, independently from retries.
                    attempt_end = min(tile_deadline, time.monotonic()+(.75 if switch_work and not retry else RADAR_TILE_TIMEOUT_SEC))
                    return self._radar_request(source, url(tx, ty), attempt_end,
                        attempt=control, retry=retry, **options)
                raw = tile_race(request, tile_deadline,
                                (.5 if switch_work else RADAR_HEDGE_SEC) if interactive else None,
                                lambda: claim_hedge(url(tx, ty)), discarded)
            self._radar_validate_tile(raw, source)
            with self._radar_lock:
                self._radar_tiles[key] = raw
                self._radar_native_groups.setdefault((source,site,stamp),set()).add(key)
                self._radar_tiles.move_to_end(key)
                while len(self._radar_tiles) > RADAR_TILE_CACHE_SIZE:
                    victim, _ = self._radar_tiles.popitem(last=False)
                    group = (victim[0],victim[1],victim[3])
                    members = self._radar_native_groups.get(group,set())
                    members.discard(victim)
                    if not members: self._radar_native_groups.pop(group,None)
            from PIL.PngImagePlugin import PngInfo
            with Image.open(io.BytesIO(raw)) as native_tile:
                with (smooth_remap if ctx.get('smooth') else remap)(native_tile, source, source_palette(source)) as mapped:
                    metadata = {k:mapped.info[k] for k in ('remapped','unmatchedColors','opaqueColors',
                        'unmatchedPixels','opaquePixels','ambiguousPixels')}
                    metadata['revision'] = SMOOTH_REVISION if ctx.get('smooth') else REMAP_REVISION
                    info = PngInfo(); info.add_text('radarRemap',json.dumps(metadata,separators=(',',':')))
                    with mapped.getchannel('A') as alpha:
                        info.add_text('radarVisiblePixels',str(mapped.width*mapped.height-alpha.histogram()[0]))
                    encoded = io.BytesIO()
                    mapped.save(encoded, format='PNG', pnginfo=info)
                    rendered = encoded.getvalue()
                    # All raster work is on radar-tile threads. One write, no
                    # read-back, PNG reopen or getsize. Publication uses the index.
                    with self._radar_lock:
                        parent = str(target.parent)
                        if parent not in self._radar_disk_inventory.directories:
                            chain = list(reversed(target.parent.parents)) + [target.parent]
                            root = Path(RADAR_DIR)
                            for directory in chain:
                                if directory != root and root not in directory.parents:
                                    continue
                                name = str(directory)
                                if name not in self._radar_disk_inventory.directories:
                                    try: os.mkdir(name)
                                    except FileExistsError: pass
                                    self._radar_disk_inventory.directories.add(name)
                        length = len(rendered)
                        if not self._radar_disk_inventory.writable:
                            raise _RadarBudget('startup inventory limit reached')
                        self._radar_prune(incoming_size=length,incoming_files=1)
                        if len(self._radar_disk_inventory)+1>8000 or self._radar_disk_inventory.bytes+length>64_000_000:
                            raise _RadarBudget('protected tile cache full')
                        tmp = str(target)+'.tmp'
                        try:
                            with open(tmp, 'wb') as output:
                                output.write(rendered)
                            os.replace(tmp,target)
                            self._radar_disk_inventory.add(disk_key,target,length,metadata)
                            self._radar_disk_files=len(self._radar_disk_inventory)
                            self._radar_disk_bytes=self._radar_disk_inventory.bytes
                        finally:
                            try: os.unlink(tmp)
                            except FileNotFoundError: pass
            return tile, rendered

        def checkpoint():
            self._radar_checkpoint(ctx)
            if time.monotonic() >= deadline:
                raise TimeoutError('radar tile batch exceeded deadline')

        missing = []
        for tile in _radar_site_tiles(ctx, site):
            tx,ty,_,_ = tile
            if _radar_disk_key(source,site,stamp,ctx['zoom'],tx,ty,ctx.get('smooth',False)) in self._radar_disk_inventory:
                yield tile, None
            else:
                missing.append(tile)
        if not missing:
            return
        tiles = iter(missing)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='radar-tile') as pool:
            pending = set()
            try:
                for tile in tiles:
                    checkpoint()
                    pending.add(pool.submit(fetch, tile))
                    if len(pending) == workers:
                        break
                while pending:
                    done, pending = wait(pending, timeout=max(0, deadline-time.monotonic()),
                                         return_when=FIRST_COMPLETED)
                    checkpoint()
                    # Inspect the whole completed batch before submitting more.
                    ready=[];error=None
                    for future in done:
                        try:ready.append(future.result())
                        except Exception as caught:
                            if error is None or isinstance(caught, (_RadarBudget, _RadarSuperseded, CircuitOpen)):
                                error = caught
                    for result in ready:
                        yield result
                    if isinstance(error, (_RadarBudget, _RadarSuperseded, CircuitOpen)):
                        raise error
                    if error is not None:
                        if failure_class(error) != 'host':
                            ctx['local_failure'] = True
                        ctx['last_error'] = str(error)
                        ctx['missing_tiles'] = True
                    for _ in done:
                        checkpoint()
                        tile = next(tiles, None)
                        if tile is not None:
                            pending.add(pool.submit(fetch, tile))
            finally:
                for future in pending:
                    future.cancel()
                # The executor drains active requests, whose successes enter LRU
                # even when the main worker has already detected supersession.

    def _radar_sliding_frames(self, source, newest, ctx):
        """Retain published scan identities only within the same render geometry."""
        snap = self._radar_result
        if (not snap.available or snap.source_id != source or
                snap.site_id != (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) or
                snap.zoom != ctx['zoom'] or snap.bounds != ctx['bounds'] or
                snap.center != dict(lat=ctx['station'][0], lon=ctx['station'][1]) or
                snap.legend != _RADAR_SOURCES[source]['legend'] or
                not snap.tiles or snap.tiles.get('revision') != (_radar_render_revision(True) if ctx.get('smooth') else _radar_render_revision())):
            return {}
        return {f['ts']: dict(f) for f in snap.frames
                if max(newest, snap.ts_frame or newest) - RADAR_HISTORY_SEC <= f['ts']}

    def _radar_fill_frame(self, source, ts, ctx, deadline, tile_url, archive_url=None, layers=None, on_validated=None):
        """Fill independent immutable tiles; never allocate viewport RGBA buffers."""
        self._radar_checkpoint(ctx)
        pairs = tuple((s,t) for s,t,_ in layers) if layers is not None else None
        frame = _radar_frame(source,ts,ctx,pairs)
        if time.monotonic() >= deadline:
            raise TimeoutError('radar acquisition deadline')
        if ctx['builds'] >= RADAR_MAX_FRAME_BUILDS_PER_PASS:
            raise _RadarBudget('radar tile-set budget')
        ctx['builds'] += 1
        work = list(reversed(layers)) if layers is not None else [(None,ts,tile_url)]
        if archive_url:
            try: self._radar_archive_probe(source,archive_url,deadline,ctx.get('request_reserve',0),
                negative_ttl=20 if not ctx.get('prefetch') and ctx.get('refresh', {}).get('state') == 'newest'
                else RADAR_NEGATIVE_CACHE_SEC)
            except (_RadarBudget,_RadarSuperseded,CircuitOpen): raise
            except Exception as error:
                if is_transport_error(error): raise
                ctx['last_error']=str(error)
                return frame
        if on_validated is not None: on_validated()
        # A validated discovery can list a pending scan before its first slow tile.
        # Cold starts and changed geometry still publish on first measurement.
        retained = self._radar_sliding_frames(source, ts, ctx)
        if (retained and not ctx.get('prefetch') and self._radar_result.ts_frame < ts):
            retained[ts] = dict(frame)
            window = tuple(retained[t] for t in sorted(retained))
            self._radar_result = self._radar_result._replace(frames=window,
                tiles=_radar_tile_manifest(source, window, ctx))
            self._radar_emit_now()
        drawn = []; present = set()
        for site,stamp,url in work:
            try:
                for tile, raw in self._radar_tile_batch(source,stamp,ctx,deadline,url,site):
                    present.add((site,stamp))
                    if raw is None:
                        continue
                    self._radar_checkpoint(ctx)
                    if not ctx.get('prefetch') and 'firstVisibleTile' not in ctx.setdefault('milestones',set()):
                        ctx['milestones'].add('firstVisibleTile')
                        self._radar_phase_metrics.append(dict(phase='firstVisibleTile',at=time.monotonic(),cpuSec=time.process_time(),intent=dict(ctx.get('intent',{})),requests=len(self._radar_request_times),bytes=getattr(self,'_radar_received_bytes',0)))
                        self._radar_phase_metrics=self._radar_phase_metrics[-128:]
                    # Publish partial newest inventory as each independent tile lands.
                    snap = self._radar_result
                    if (snap.source_id != source or
                            source == 'iem-nexrad-n0b' and snap.site_id != ctx.get('site_id') or
                            snap.ts_frame is None or ts >= snap.ts_frame) and not ctx.get('prefetch') and not ctx.get('staging_source'):
                        settings = _RADAR_SOURCES[source]
                        # A tile is an independently validated measurement; publish
                        # its stamp before the remaining viewport tiles finish.
                        partial = _RADAR_NONE._replace(available=True,reason=None,frames=(dict(frame),),
                            ts_frame=ts,center=dict(lat=ctx['station'][0],lon=ctx['station'][1]),
                            zoom=ctx['zoom'],mpp=ctx['mpp'],bounds=ctx['bounds'],scalebar=ctx['bar'],
                            rings=ctx['rings'],nexrad=ctx['nexrad'],ts_fetch=time.time(),source_id=source,
                            **settings,zoom_desired=ctx['desired'],zoom_auto_level=ctx['auto_zoom'],
                            geo=ctx.get('geo'),units=ctx['unit'],source_pref=ctx['source_pref'],
                            source_fallback=ctx['source_fallback'],sources=tuple(ctx['sources']),
                            source_mode='site' if source=='iem-nexrad-n0b' else 'mosaic',
                            site_id=ctx.get('site_id'),sites=tuple(ctx.get('sites',())),
                            sites_considered=ctx.get('sites_considered',0),
                            tiles=_radar_tile_manifest(source,[frame],ctx))
                        retained = self._radar_sliding_frames(source, ts, ctx)
                        if retained:
                            retained.setdefault(ts, dict(frame))
                            window = tuple(retained[t] for t in sorted(retained))
                            partial = partial._replace(frames=window,
                                tiles=_radar_tile_manifest(source, window, ctx),
                                ts_fetch=snap.ts_fetch if snap.ts_frame == ts else partial.ts_fetch)
                        self._radar_note_source(source, ctx)
                        self._radar_result=partial
                        self._radar_health.last_success = time.time()
                        self._radar_emit_now()
                if ctx.get('reuse_newest') and not any(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
                        for x,y,_,_ in _radar_site_tiles(ctx,site)):
                    self._radar_forget(source,site)
                    raise _RadarRevalidate('remembered newest unavailable')
                drawn.append((site,stamp))
            except (_RadarSuperseded, _RadarBudget, CircuitOpen, _RadarRevalidate):
                raise
            except Exception as error:
                if is_transport_error(error): raise
                self._radar_forget(source,site)
                if ctx.get('reuse_newest'): raise _RadarRevalidate('remembered newest unavailable') from error
                ctx['last_error'] = str(error)
                if site: ctx.setdefault('site_reasons',{})[site] = 'scan unavailable'
            # Executor drain may finish paid-for tiles after the first failure.
            if any(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
                   for x,y,_,_ in _radar_site_tiles(ctx,site)):
                present.add((site,stamp))
        if layers is not None:
            frame['siteScans'] = [dict(id=s,ts=t) for s,t in pairs]
        # Admission needs a real measurement; manifest coverage separately
        # determines whether the entire tile set is ready for playback.
        frame['acquiredSites'] = [dict(id=s,ts=t) for s,t in (pairs or ()) if (s,t) in present]
        frame['publishable'] = bool(present)
        frame['complete'] = bool(present) and all(all(_radar_present(ctx,source,site,stamp,ctx['zoom'],x,y)
            for x,y,_,_ in _radar_site_tiles(ctx,site)) for site,stamp in (pairs or [(None,ts)]))
        return frame

    def _radar_known(self, source, ctx, site=None):
        entry = self._radar_newest.get((source, site))
        if (ctx.get('intent_triggered') and entry is not None and
                0 <= time.monotonic() - entry[0] < _RADAR_SOURCES[source]['cadence']):
            return entry[1]
        return None

    def _radar_forget(self, source, site=None):
        for key in list(self._radar_newest):
            if key[0] == source and (site is None or key[1] == site):
                del self._radar_newest[key]

    def _radar_archive_probe(self, source, url, deadline, reserve=0, negative_ttl=RADAR_NEGATIVE_CACHE_SEC):
        if url in self._radar_archive_positive:
            return
        if time.monotonic() < self._radar_negative.get(url, 0):
            raise ValueError('archive unavailable')
        try:
            self._radar_request(source, url, deadline, method='HEAD', **(dict(reserve=reserve) if reserve else {}))
        except (_RadarBudget, _RadarSuperseded):
            raise
        except Exception as error:
            if not is_transport_error(error):
                self._radar_negative[url] = time.monotonic() + negative_ttl
            raise
        if len(self._radar_archive_positive) >= 128:
            self._radar_archive_positive.clear()
        self._radar_archive_positive.add(url)

    def _radar_iem_scan(self, ctx):
        """Discover newest; foreground and idle warming share validation lifetime."""
        source = 'iem-mrms-lcref'
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        known = self._radar_known(source, ctx)
        now = time.time()
        if known is not None and not 0 <= now - known['newest'] <= RADAR_IEM_STALE_SEC:
            known = None
        if known is None:
            self._radar_forget(source)
            meta = json.loads(self._radar_request(source, RADAR_IEM_METADATA_URL, deadline, metadata=True,
                **(dict(reserve=ctx['request_reserve']) if ctx.get('request_reserve') else {})))['meta']
            valid = datetime.fromisoformat(meta['end_valid'].replace('Z', '+00:00'))
            ts = int(valid.timestamp())
            if (valid.utcoffset() != timedelta(0) or meta.get('product') != 'lcref' or
                    meta.get('units') != '0.5 dBZ' or ts % RADAR_IEM_FRAME_INTERVAL_SEC or
                    not 0 <= now - ts <= RADAR_IEM_STALE_SEC):
                raise ValueError('invalid or stale IEM metadata')
            newest = ts
        else:
            newest = known['newest']
        return newest, known, deadline

    def _radar_iem_frames(self, ctx):
        """Acquire a fresh complete primary first, probing even UTC slots backward."""
        source = 'iem-mrms-lcref'
        newest, known, deadline = self._radar_iem_scan(ctx)
        self._radar_discovery_unchanged(source, newest, ctx, dict(newest=newest))
        now = time.time()
        self._radar_publish_refresh(ctx, frameTotal=RADAR_HISTORY_SEC // 120 + 1 if ctx['viewed'] else 1)
        def build(stamp, limit):
            utc = datetime.fromtimestamp(stamp, timezone.utc)
            def validated():
                # Superseding intents can reuse discovery even mid-tile-batch.
                self._radar_newest[(source, None)] = (time.monotonic(), dict(newest=stamp))
            return self._radar_fill_frame(source, stamp, ctx, limit,
                lambda x, y: RADAR_IEM_TILE_TEMPLATE.format(stamp=utc.strftime('%Y%m%d%H%M'),
                    z=ctx['zoom'], x=x, y=y),
                None if known is not None else utc.strftime(RADAR_IEM_ARCHIVE_TEMPLATE),
                on_validated=validated if known is None and stamp == newest else None)
        for candidate in range(newest, int(now - RADAR_IEM_STALE_SEC) - 1, -120):
            ctx['candidates'].append(candidate)
            ctx['reuse_newest'] = known is not None
            latest = build(candidate, deadline)
            ctx['reuse_newest'] = False
            if not latest.get('publishable',latest['complete']):
                self._radar_forget(source)
                if known is not None:
                    raise _RadarRevalidate('remembered newest unavailable')
            if latest.get('publishable',latest['complete']):
                if time.time() - candidate > RADAR_IEM_STALE_SEC:
                    break
                return self._radar_history(source, candidate, newest, ctx, build, latest)
        raise ValueError('no fresh complete IEM frame: ' + ctx.get('last_error', 'unavailable'))

    def _radar_site_listing(self, ctx, site):
        """One listing owner for viewport acquisition and Region's cadence check."""
        from urllib.parse import urlencode
        source = 'iem-nexrad-n0b'
        now = time.time()
        fmt = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        self._radar_checkpoint(ctx)
        url = RADAR_SITE_LIST_URL + '?' + urlencode(dict(operation='list', radar=site['id'][1:],
            product='N0B', start=fmt(now - RADAR_HISTORY_SEC - RADAR_SITE_MAX_AGE_SEC), end=fmt(now)))
        cached = ctx.get('listing_results', {}).get(site['id'])
        if cached is not None:
            state, result = cached
            site.update(state)
            return result
        stamps = []
        reason = 'not reporting'
        try:
            known = self._radar_known(source, ctx, site['id'])
            if known is not None:
                stamps = [t for t in known['stamps'] if 0 <= now-t <= RADAR_HISTORY_SEC + RADAR_SITE_MAX_AGE_SEC]
            else:
                self._radar_forget(source, site['id'])
                listing = json.loads(self._radar_request(source, url, deadline, metadata=True,
                    **(dict(reserve=ctx['request_reserve']) if ctx.get('request_reserve') else {})))
                for scan in listing['scans']:
                    valid = datetime.fromisoformat(scan['ts'].replace('Z', '+00:00'))
                    if valid.utcoffset() != timedelta(0):
                        raise ValueError('non-UTC site scan')
                    ts = int(valid.timestamp())
                    if 0 <= now - ts <= RADAR_HISTORY_SEC + RADAR_SITE_MAX_AGE_SEC and ts % 60 == 0:
                        stamps.append(ts)
                stamps = sorted(set(stamps))
                self._radar_newest[(source, site['id'])] = (time.monotonic(),
                    dict(newest=stamps[-1] if stamps else None, stamps=tuple(stamps), checkedTs=now))
        except (_RadarBudget, _RadarSuperseded):
            raise
        except Exception as error:
            stamps = []
            reason = 'scan unavailable'
            Logger.warning(f'almanac_emit: radar site {site["id"]} listing failed: {type(error).__name__}: {error}')
        newest = stamps[-1] if stamps else None
        site.update(reason=None if newest is not None and now-newest < RADAR_SITE_MAX_AGE_SEC else reason)
        site.update(reporting=newest is not None and now-newest < RADAR_SITE_MAX_AGE_SEC,
                    newestTs=newest, ageSec=int(now-newest) if newest is not None else None)
        checked = known.get('checkedTs') if known is not None else now
        if known is None:
            # Cached acquisition may age scans out of its history window, but
            # only an actual listing attempt can replace the last-check evidence.
            with self._radar_lock:
                self._radar_site_status[site['id']] = dict(reporting=site['reporting'], newestTs=newest,
                    reason=site['reason'], checkedTs=checked)
        result = site['id'], tuple(stamps), known is not None
        if 'listing_results' in ctx and known is None:
            ctx['listing_results'][site['id']] = ({k: site[k] for k in ('reporting', 'newestTs', 'ageSec', 'reason')}, result)
        return result

    def _radar_site_discover(self, ctx):
        """Concurrent per-site listings, reused by intent and idle tile warming."""
        sites, considered = _radar_sites(ctx['station'], ctx['bounds'])
        if not sites:
            ctx['site_failure'] = 'out of view'
            raise ValueError('no viewport coverage')
        for site in sites:
            site.update(reporting=False, newestTs=None, ageSec=None, primary=False, contributing=False, reason='not reporting')
        ctx.update(sites=sites, sites_considered=considered, site_scans={})
        deadline = min(ctx['deadline'], time.monotonic() + RADAR_PRIMARY_DEADLINE_SEC)
        # Timeline ownership is independent of viewport selection and arrival order.
        timeline = sorted((dict(id=i, lat=a, lon=b, distanceMeters=distance_meters(*ctx['station'],a,b))
                           for i,(a,b,_) in _NEXRAD_SITES.items()
                           if distance_meters(*ctx['station'],a,b) <= RADAR_SITE_RANGE_METERS),
                          key=lambda s:(s['distanceMeters'],s['id']))[:RADAR_SITE_MAX_COUNT]
        listings = {s['id']:s for s in sites}
        for site in timeline:
            listings.setdefault(site['id'], dict(site, reporting=False, newestTs=None, reason='not reporting'))
        with ThreadPoolExecutor(max_workers=RADAR_TILE_WORKERS, thread_name_prefix='radar-list') as pool:
            futures = [pool.submit(self._radar_site_listing, ctx, site) for site in
                       sorted(listings.values(), key=lambda s:(s['distanceMeters'],s['id']))]
            for future in futures:
                site, stamps, reused = future.result(timeout=max(0, deadline-time.monotonic()))
                ctx['site_scans'][site] = stamps
                ctx['reuse_newest'] = ctx.get('reuse_newest', False) or reused
        self._radar_checkpoint(ctx)
        reporting = sorted((s for s in listings.values() if s['reporting'] and s['id'] in {t['id'] for t in timeline}), key=lambda s: (s['distanceMeters'], s['id']))
        if not reporting:
            ctx.setdefault('site_failure', 'scan unavailable' if any(s['reason']=='scan unavailable' for s in sites) else 'not reporting')
            raise ValueError('no site reporting in viewport')
        ctx['site_id'] = reporting[0]['id']
        for site in sites:
            site.update(primary=site['id']==ctx['site_id'], contributing=site['reporting'],
                        reason=None if site['reporting'] else site['reason'])
        ctx['sources'][1] = dict(mode='site', siteId=ctx['site_id'], available=True, reason=None)
        return ctx['site_scans'][ctx['site_id']], deadline

    def _radar_site_frames(self, ctx):
        source = 'iem-nexrad-n0b'
        now = time.time()
        stamps, deadline = self._radar_site_discover(ctx)
        if self._radar_refuse_dark_site(ctx):
            return
        ctx.update(_radar_scan_cadence(stamps))
        self._radar_discovery_unchanged(source, stamps[-1], ctx)
        def build(ts, limit):
            layers = []
            for site, scan in _radar_site_pairs(ctx, ts):
                stamp = datetime.fromtimestamp(scan, timezone.utc).strftime('%Y%m%d%H%M')
                def url(x, y, site=site, stamp=stamp):
                    return RADAR_SITE_TILE_TEMPLATE.format(site=site[1:], stamp=stamp,
                        z=ctx['zoom'], x=x, y=y)
                layers.append((site, scan, url))
            return self._radar_fill_frame(source, ts, ctx, limit, None, layers=layers)
        for ts in reversed(stamps):
            if now - ts >= RADAR_SITE_MAX_AGE_SEC:
                break
            ctx['candidates'].append(ts)
            slots = [t for t in stamps if ts - RADAR_HISTORY_SEC <= t <= ts][-(8 if len(_radar_site_pairs(ctx, ts)) >= 2 else 31):]
            self._radar_publish_refresh(ctx, frameTotal=len(slots) if ctx['viewed'] else 1)
            latest = build(ts, deadline)
            if ctx.get('reuse_newest') and not latest.get('publishable',latest['complete']):
                raise _RadarRevalidate('remembered site scan unavailable')
            ctx['reuse_newest'] = False
            if latest.get('publishable',latest['complete']):
                cap = 8 if len(latest.get('acquiredSites',latest.get('siteScans', ()))) >= 2 else 31
                slots = [t for t in stamps if ts - RADAR_HISTORY_SEC <= t <= ts][-cap:]
                self._radar_publish_refresh(ctx, frameTotal=len(slots) if ctx['viewed'] else 1)
                return self._radar_history(source, ts, stamps[-1], ctx, build, latest, slots)
        raise ValueError('no complete site scan: ' + ctx.get('last_error', 'unavailable'))

    def _radar_rainviewer_frames(self, ctx):
        """Global past-frame adapter; never include nowcast or more than one hour."""
        source = 'rainviewer'
        known = self._radar_known(source, ctx)
        if known is None:
            self._radar_forget(source)
            manifest = json.loads(self._radar_request(source, RADAR_RAINVIEWER_MANIFEST_URL,
                ctx['deadline'], metadata=True))
            host = manifest['host'].rstrip('/')
            if not host.startswith('https://'):
                raise ValueError('invalid RainViewer host')
            past = {int(f['time']): f['path'] for f in manifest['radar']['past']}
            newest = max(past)
            past = {t: p for t, p in past.items() if newest - RADAR_HISTORY_SEC <= t <= newest}
        else:
            newest, host, past = known['newest'], known['host'], known['past']
        if not 0 <= time.time() - newest <= RADAR_RAINVIEWER_STALE_SEC:
            if known is not None:
                raise _RadarRevalidate('remembered RainViewer scan aged out')
            raise ValueError('invalid or stale RainViewer manifest')
        self._radar_discovery_unchanged(source, newest, ctx, dict(newest=newest, host=host, past=past))
        self._radar_publish_refresh(ctx, frameTotal=len(past) if ctx['viewed'] else 1)
        def build(ts, limit):
            path = past[ts]
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('invalid RainViewer path')
            if known is None and ts == newest:
                self._radar_newest[(source, None)] = (time.monotonic(), dict(newest=newest, host=host, past=past))
            return self._radar_fill_frame(source, ts, ctx, limit,
                lambda x, y: f'{host}{path}/256/{ctx["zoom"]}/{x}/{y}/{RADAR_RAINVIEWER_COLOR}/{RADAR_RAINVIEWER_TILE_OPTS}.png')
        ctx['reuse_newest'] = known is not None
        latest = build(newest, ctx['deadline'])
        ctx['reuse_newest'] = False
        if known is not None and not latest.get('publishable',latest['complete']):
            raise _RadarRevalidate('remembered RainViewer scan unavailable')
        if not latest.get('publishable',latest['complete']):
            raise ValueError('no complete RainViewer latest')
        ctx['rainviewer_prefix'] = host + past[newest]
        return self._radar_history(source, newest, newest, ctx, build, latest, sorted(past))

    def _radar_history(self, source, newest, advertised, ctx, build, latest, slots=None):
        """Publish latest promptly, then atomically replace with bounded backfill."""
        ctx['tile_workers'] = RADAR_TILE_WORKERS
        settings = _RADAR_SOURCES[source]
        slots = slots or list(range(newest - RADAR_HISTORY_SEC, newest + 1, settings['cadence']))
        frames = {t: _radar_frame(source, t, ctx, _radar_site_pairs(ctx, t)
                  if source == 'iem-nexrad-n0b' else None) for t in slots}
        # Discovery may revise per-site lists. Existing published frames retain
        # their exact siteScans; never recombine an old scan under its old stamp.
        frames.update(self._radar_sliding_frames(source, newest, ctx))
        frames[newest] = latest
        slots = sorted(frames)
        previous = ctx.get('previous_result',self._radar_result)
        newest_reasons = dict(ctx.get('site_reasons', {}))
        fetched = time.time()
        if (newest < advertised and previous.available and previous.source_id == source
                and previous.site_id == (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) and previous.center == dict(lat=ctx['station'][0], lon=ctx['station'][1]) and previous.bounds == ctx['bounds']
                and previous.zoom == ctx['zoom'] and previous.ts_frame == newest):
            ctx['retained_failed'] = True
            self._schedule_retry('radar', self._check_radar, RADAR_RETRY_SEC)
            return previous
        if ctx.get('site_budget_limited'):
            self._radar_budget_retry(source, len(ctx['tiles'])+2)
        elif newest < advertised:
            self._schedule_retry('radar', self._check_radar, RADAR_RETRY_SEC)
        if newest < advertised and previous.source_id == source and previous.ts_frame == newest:
            fetched = previous.ts_fetch  # a failed newer frame is not a successful refresh
        def publish():
            self._radar_checkpoint(ctx)
            if source == 'iem-mrms-lcref' and time.time() - newest > settings['stale_sec']:
                return False
            if (previous.available and previous.source_id == source and previous.center == dict(lat=ctx['station'][0], lon=ctx['station'][1]) and previous.bounds == ctx['bounds']
                    and previous.zoom == ctx['zoom'] and previous.site_id == (ctx.get('site_id') if source == 'iem-nexrad-n0b' else None) and previous.ts_frame is not None and previous.ts_frame > newest):
                raise ValueError('source timestamp regressed')
            if (ctx.get('staging_source') and self._radar_result.source_id != source
                    and sum(f['complete'] for f in frames.values()) < min(4, len(frames))):
                return True  # continue building behind the retained manifest
            self._radar_note_source(source, ctx)
            snapshot = _RadarResult(True, None, tuple(dict(frames[t]) for t in sorted(frames)),
                newest, dict(lat=ctx['station'][0],lon=ctx['station'][1]), ctx['zoom'], ctx['mpp'], ctx['bounds'],
                ctx['bar'], ctx['rings'], ctx['nexrad'], fetched, source, **settings,
                zoom_desired=ctx['desired'], zoom_auto_level=ctx['auto_zoom'],
                source_pref=ctx['source_pref'],
                source_fallback=ctx['source_fallback'],
                geo=ctx.get('geo'), tiles=_radar_tile_manifest(source,[frames[t] for t in sorted(frames)],ctx), units=ctx['unit'], source_mode='site' if source == 'iem-nexrad-n0b' else 'mosaic',
                site_id=ctx.get('site_id') if source == 'iem-nexrad-n0b' else None,
                sites=tuple(dict(s, contributing=any(p['id']==s['id'] for p in latest.get('siteScans', ())),
                    reason=None if any(p['id']==s['id'] for p in latest.get('siteScans', ())) else
                    (newest_reasons.get(s['id']) or s.get('reason') or 'scan unavailable')) for s in ctx.get('sites', ())), sites_considered=ctx.get('sites_considered', 0),
                sources=tuple(dict(s) for s in ctx.get('sources', ())),
                **({k:ctx.get(k) for k in ('scan_cadence_sec','scan_mode','scan_mode_source','scanning_slowly')}
                   if source == 'iem-nexrad-n0b' else dict(scanning_slowly=False)),
                partial_coverage=source == 'iem-mrms-lcref' and any(
                    ctx['bounds'][k] < _RADAR_IEM_DOMAIN[k] if k in ('w', 's') else
                    ctx['bounds'][k] > _RADAR_IEM_DOMAIN[k] for k in ('w', 'e', 's', 'n')))
            self._radar_result_stamp = ctx.get('preference_stamp')
            self._radar_publish_refresh(ctx, snapshot=snapshot, frameIndex=sum(f['complete'] for f in frames.values()))
            return True
        # Disk tiles survive tab closure, camera moves and emitter restarts.
        for t in slots:
            f = frames[t]
            pairs = [(p['id'],p['ts']) for p in f['siteScans']] or [(None,t)]
            f['complete'] = all(all(_radar_present(ctx,source,site,scan,ctx['zoom'],x,y)
                for x,y,_,_ in _radar_site_tiles(ctx,site)) for site,scan in pairs)
        target = min(RADAR_LOOP_FRAMES if ctx['viewed'] else 4 if ctx.get('staging_source') else 1, len(slots))
        def pending_work():
            count = sum(frames[t]['complete'] for t in slots[-target:])
            self._radar_pending = dict(newest=not frames[newest]['complete'],
                four=count < min(4,target), eight=count < target, optional=count >= target)
        pending_work()
        starting_complete = sum(frames[t]['complete'] for t in slots[-target:])
        if not publish():
            raise ValueError('IEM frame aged out during acquisition')
        self._radar_health.last_success = time.time()
        ctx['deadline'] = ctx.get('pass_deadline', ctx['deadline'])
        self._radar_session.begin_pass(ctx['deadline'])
        self._radar_idle_context = (source, {k: v for k, v in ctx.items() if k != 'listing_results'})
        if not latest['complete']:
            if self._radar_failed_pass(source, TimeoutError('visible newest incomplete'), ctx):
                raise TimeoutError('visible newest objective failed three passes')
            self._radar_pending.update(newest=True, four=True, eight=True)
            ctx['retained_failed'] = True
            self._radar_budget_retry(source, len(ctx['tiles'])+2)
            return self._radar_result
        if ctx['viewed'] or ctx.get('staging_source'):
            self._radar_publish_refresh(ctx,state='idle')
            ctx['tiles'] = _radar_grid(ctx)
            ordered = list(reversed(slots))
            if not ctx['viewed']:
                ordered = ordered[:4]
            needed = 0
            view_delay = 0
            deferred = False
            retry = self._radar_session.on_retry
            # The callback also charges transparent retries at the current tier's
            # floor. HEAD probes and tile workers use the same atomic gate.
            self._radar_session.on_retry = lambda end, first_byte=False: self._radar_transport_retry(
                source, end, ctx.get('request_reserve', 0), first_byte=first_byte)
            try:
                for deep, tier in ((False, ordered[:RADAR_LOOP_FRAMES]),
                                   (True, ordered[RADAR_LOOP_FRAMES:])):
                    if deep:
                        if deferred or any(not frames[t]['complete'] for t in ordered[:RADAR_LOOP_FRAMES]):
                            break
                        self._radar_publish_refresh(ctx, state='idle')
                        visible_tiles = ctx['tiles']
                        ctx.update(tiles=_radar_grid(ctx, margin=1), prefetch=True,
                                   request_reserve=self._radar_mandatory_reserve(source, ctx, [newest]))
                        mandatory_builds = ctx['builds']
                        try:
                            build(newest, ctx['deadline'])
                        finally:
                            ctx['builds'] = mandatory_builds
                            ctx['tiles'] = visible_tiles
                            ctx.pop('prefetch', None)
                            ctx.pop('request_reserve', None)
                        self._radar_prefetch(source,ctx)
                        self._radar_pending['optional'] = False
                    reserve = self._radar_mandatory_reserve(source, ctx, slots[-4:]) if deep else 0
                    if deep:
                        reserve += RADAR_PREFETCH_HEADROOM
                    ctx['request_reserve'] = reserve
                    for t in tier:
                        self._radar_checkpoint(ctx)
                        if frames[t]['complete']:
                            continue
                        pairs = _radar_site_pairs(ctx,t) if source == 'iem-nexrad-n0b' else [(None,t)]
                        cost = sum(not _radar_present(ctx,source,site,scan,ctx['zoom'],x,y)
                            for site,scan in pairs for x,y,_,_ in _radar_site_tiles(ctx,site))
                        cost += source == 'iem-mrms-lcref'
                        needed = cost + reserve
                        if deep:
                            view_delay = self._radar_deep_view_delay(ctx)
                            if view_delay != 0:
                                deferred = True
                                break
                            ctx['deep_history'] = True
                        if self._radar_headroom_delay(source, needed):
                            deferred = True
                            break
                        # Loop and deep history must not evict the newest
                        # native tiles just warmed for the next press. Touch
                        # these entries before each bounded frame build.
                        stamps = (set((p['id'], p['ts']) for p in latest.get('siteScans', ()))
                                  if source == 'iem-nexrad-n0b' else {(None, newest)})
                        protected = {(source, site, stamp) for site, stamp in stamps}
                        protected.update((key[0], site, stamp)
                            for key, signature in self._radar_prefetched.items()
                            if key[2:] == (ctx['center']['lat'], ctx['center']['lon'])
                            for site, stamp in signature)
                        with self._radar_lock:
                            for group in protected:
                                for key in self._radar_native_groups.get(group, ()):
                                    if key in self._radar_tiles:
                                        self._radar_tiles.move_to_end(key)
                        self._radar_publish_refresh(ctx, state='history', frameTotal=len(slots))
                        try:
                            frame = build(t, ctx['deadline'])
                        except (TimeoutError, _RadarBudget):
                            deferred = True
                            break
                        if frame['complete']:
                            frames[t] = frame
                            pending_work()
                            if not publish():
                                break
            except (TimeoutError, _RadarBudget):
                deferred = True
                view_delay = self._radar_deep_view_delay(ctx) if ctx.get('deep_history') else 0
            finally:
                ctx.pop('deep_history', None)
                ctx.pop('request_reserve', None)
                self._radar_session.on_retry = retry
            if any(not f['complete'] for f in frames.values()):
                if deferred and view_delay is not None:
                    self._radar_budget_retry(source, needed, min_delay=view_delay)
                else:
                    self._radar_budget_retry(source, max(1, needed))
        completed = sum(frames[t]['complete'] for t in slots[-target:])
        if completed < min(4,target) and completed <= starting_complete and ctx.get('missing_tiles'):
            ctx['retained_failed'] = True
            if self._radar_failed_pass(source, TimeoutError('four-frame objective made no progress'), ctx):
                raise TimeoutError('four-frame objective failed three passes')
        if ctx.get('staging_source') and self._radar_result.source_id != source:
            ctx['retained_failed'] = True
            self._radar_budget_retry(source, len(ctx['tiles'])+2)
        return self._radar_result

    def _radar_mandatory_reserve(self, source, ctx, stamps):
        tiles = _radar_grid(ctx)
        count = 0
        def required(stamp):
            if source != 'iem-nexrad-n0b':
                return [(None,stamp)]
            if 'site_scans' in ctx:
                return _radar_site_pairs(ctx,stamp)
            return [(site['id'],site.get('newestTs') or stamp) for site in ctx.get('sites',()) if site.get('reporting')]
        for stamp in stamps:
            pairs = required(stamp)
            for site, scan in pairs:
                for x,y,_,_ in _radar_site_tiles(dict(ctx, tiles=tiles), site):
                    count += not _radar_present(dict(ctx, inventory=self._radar_disk_inventory, manifest_cache=self._radar_manifest_cache), source, site, scan, ctx['zoom'], x, y)
        # Preserve a cold newest at this footprint even when the current loop
        # is already cached; price every required site layer.
        layers = len(required(stamps[-1])) if stamps else 1
        return min(RADAR_REQUESTS_PER_MIN-1, max(count, len(tiles)*max(1,layers))+2)

    def _radar_prefetch(self, source, ctx):
        """Idle source/zoom rounds before history; newest native and disk tiles."""
        if (not ctx['viewed'] or not self._radar_is_viewed() or
                ctx.get('refresh', {}).get('state') != 'idle'):
            return
        known_ctx = dict(ctx, intent_triggered=True)
        newest = self._radar_result.ts_frame
        known = self._radar_known(source, known_ctx) if source != 'iem-nexrad-n0b' else None
        own_fresh = source == 'iem-nexrad-n0b' or known is not None and known['newest'] == newest
        floor = RADAR_SITE_MIN_ZOOM if source == 'iem-nexrad-n0b' else RADAR_MIN_ZOOM
        targets = [(source, z) for z in (ctx['zoom']-1, ctx['zoom']+1)
                   if floor <= z <= _RADAR_SOURCES[source]['max_zoom']]
        if source == 'iem-nexrad-n0b':
            if ctx['zoom'] <= _RADAR_SOURCES['iem-mrms-lcref']['max_zoom']:
                targets.insert(0, ('iem-mrms-lcref', ctx['zoom']))
            targets.extend(('iem-mrms-lcref', z) for z in (ctx['zoom']-1, ctx['zoom']+1)
                           if RADAR_MIN_ZOOM <= z <= _RADAR_SOURCES['iem-mrms-lcref']['max_zoom'])
        elif source == 'iem-mrms-lcref' and ctx['sources'][1]['available'] and ctx['zoom'] >= RADAR_SITE_MIN_ZOOM:
            # Same-centre mode tap first; also cover a mode+zoom press (z8 -> site z7).
            targets.extend(('iem-nexrad-n0b', z) for z in
                           (ctx['zoom'], ctx['zoom']-1, ctx['zoom']+1)
                           if RADAR_SITE_MIN_ZOOM <= z <= 10)
        # The current camera's opposite mode comes before optional zoom neighbours.
        targets.sort(key=lambda item: item[0] == source or item[1] != ctx['zoom'])
        targets = [(target,z) for target,z in targets if target != source or own_fresh]
        reserve = self._radar_mandatory_reserve(source, ctx, [newest])
        retry = self._radar_session.on_retry
        admitted_sources = set()
        denied_sources = set()
        try:
            for target, zoom in targets:
                if target in denied_sources:
                    continue
                warm = dict(ctx, intent_triggered=True, prefetch=True, sources=list(ctx['sources']),
                            request_reserve=reserve, tile_workers=RADAR_TILE_WORKERS)
                self._radar_checkpoint(warm)
                if target not in admitted_sources:
                    # As with the original Z±1 tier, admit a source round once
                    # with 60 spare slots; every request still preserves 34.
                    if self._radar_headroom_delay(target, RADAR_PREFETCH_HEADROOM + reserve):
                        self._radar_budget_retry(target, RADAR_PREFETCH_HEADROOM + reserve)
                        # A source cooldown must not block the other source.
                        # The shared request cap and reserve still apply to both.
                        denied_sources.add(target)
                        continue
                    admitted_sources.add(target)
                tiles, _, bounds, _ = _radar_viewport(ctx['center']['lat'], ctx['center']['lon'],
                    zoom, RADAR_VIEWPORT_W, RADAR_VIEWPORT_H)
                if target == source:
                    cx,cy = world_point(ctx['center']['lat'],ctx['center']['lon'],zoom)
                    tiles = [(x,y,0,0) for y in (int(cy//256)-1,int(cy//256))
                             for x in (int(cx//256)-1,int(cx//256)) if 0<=x<2**zoom and 0<=y<2**zoom]
                warm.update(zoom=zoom, camera_zoom=zoom, tiles=tiles, bounds=bounds)
                if target != source and zoom == ctx['zoom']:
                    warm['tiles'] = _radar_grid(warm,margin=1)
                self._radar_session.on_retry = lambda end, first_byte=False: self._radar_transport_retry(
                    target, end, reserve, first_byte=first_byte)
                try:
                    if target == 'iem-nexrad-n0b':
                        stamps, _ = self._radar_site_discover(warm)
                        pairs = _radar_site_pairs(warm, stamps[-1])
                        # Empty listings participate in the round identity too.
                        signature = tuple(sorted(set(pairs) |
                            {(site, scans[-1] if scans else None)
                             for site, scans in warm['site_scans'].items()}, key=repr))
                    elif target == 'iem-mrms-lcref':
                        stamp, known, deadline = self._radar_iem_scan(warm)
                        if known is None:
                            utc = datetime.fromtimestamp(stamp, timezone.utc)
                            self._radar_archive_probe(target, utc.strftime(RADAR_IEM_ARCHIVE_TEMPLATE),
                                                      deadline, reserve)
                            self._radar_newest[(target, None)] = (time.monotonic(), dict(newest=stamp))
                        pairs = ((None, stamp),)
                        signature = pairs
                    else:
                        pairs = ((None, newest),)
                        signature = pairs
                    key = (target, zoom, ctx['center']['lat'], ctx['center']['lon'])
                    if (self._radar_prefetched.get(key) == signature and
                            all(_radar_present(dict(warm, inventory=self._radar_disk_inventory, manifest_cache=self._radar_manifest_cache), target, site, stamp, zoom, x, y)
                                for site, stamp in pairs
                                for x, y, _, _ in _radar_site_tiles(warm, site))):
                        continue
                    # Bounded completion memory per geometry and scan set. Successful
                    # in-flight tiles survive cancellation in the ordinary tile LRU.
                    if len(self._radar_prefetched) >= RADAR_TILE_CACHE_SIZE:
                        self._radar_prefetched.pop(next(iter(self._radar_prefetched)))
                    # Record completion only after every tile succeeds. An intent
                    # interrupt must not turn a partial round into a permanent hit.
                    self._radar_prefetched.pop(key, None)
                    for site, stamp in pairs:
                        utc = datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d%H%M')
                        if target == 'iem-mrms-lcref':
                            url = lambda x, y: RADAR_IEM_TILE_TEMPLATE.format(stamp=utc, z=zoom, x=x, y=y)
                        elif target == 'iem-nexrad-n0b':
                            url = lambda x, y: RADAR_SITE_TILE_TEMPLATE.format(site=site[1:], stamp=utc, z=zoom, x=x, y=y)
                        else:
                            url = lambda x, y: (f'{ctx["rainviewer_prefix"]}/256/{zoom}/{x}/{y}/'
                                f'{RADAR_RAINVIEWER_COLOR}/{RADAR_RAINVIEWER_TILE_OPTS}.png')
                        try:
                            for _ in self._radar_tile_batch(target, stamp, warm, ctx['deadline'], url, site):
                                pass
                        except (_RadarBudget, _RadarSuperseded):
                            raise
                        except Exception:
                            self._radar_forget(target, site)
                            raise
                    self._radar_prefetched[key] = signature
                except (_RadarBudget, _RadarSuperseded):
                    raise
                except Exception:
                    # Optional warming cannot invalidate the foreground scan.
                    continue
        except _RadarSuperseded:
            raise
        except _RadarBudget:
            pass
        finally:
            self._radar_session.on_retry = retry

    def _radar_prune(self, previous=None, incoming_size=0, incoming_files=0):
        """Pin the displayed and retained visible loops; evict other served LRU."""
        cache = self._radar_disk_inventory
        if len(cache)+incoming_files <= cache.MAX_FILES and cache.bytes+incoming_size <= cache.MAX_BYTES:
            return
        pinned = set()
        for snap in (self._radar_result, previous):
            if snap is None or not snap.tiles:
                continue
            grid = snap.tiles.get('grid', {})
            for frame in snap.frames[-8:]:
                for pair in frame.get('siteScans') or [dict(id=None, ts=frame['ts'])]:
                    for y in range(grid.get('y0',0), grid.get('y0',0)+grid.get('h',0)):
                        for x in range(grid.get('x0',0), grid.get('x0',0)+grid.get('w',0)):
                            pinned.add(_radar_disk_key(snap.source_id,pair['id'],pair['ts'],snap.zoom,x,y,snap.tiles.get('smooth',False)))
        cache.evict(pinned, incoming_size, incoming_files)
        self._radar_disk_files=len(cache);self._radar_disk_bytes=cache.bytes

    def _radar_invalidate_tile(self, key):
        """Explicit eviction/repair notification, also used by local cache tools."""
        with self._radar_lock:
            record = self._radar_disk_inventory.discard(key)
            if record is not None:
                record[0].unlink(missing_ok=True)
                self._radar_disk_files=len(self._radar_disk_inventory)
                self._radar_disk_bytes=self._radar_disk_inventory.bytes
                self._radar_pending.update(newest=True, four=True, eight=True)
                self._radar_acquisition_pending = True
            return record is not None

    def _radar_consume_bad_tiles(self):
        if not self._radar_cache_ready.is_set():
            return
        marker = Path(self.output_path).with_name('radar_bad_tiles')
        try:
            stat = marker.stat()
            stamp = (stat.st_ino,stat.st_mtime_ns,stat.st_size)
            if stamp == self._radar_bad_stamp:
                return
            self._radar_bad_stamp = stamp
            if stat.st_size > 32768:
                return
            paths = json.loads(marker.read_text())
            if not isinstance(paths, list):
                return
            for relative in paths[:128]:
                if not isinstance(relative,str):
                    continue
                parts = relative.split('/')
                if len(parts) != 9 or parts[:2] != ['radar','t'] or parts[2] not in (_radar_render_revision(), _radar_render_revision(True)):
                    continue
                _,_,_,source,site,scan,z,x,y = parts
                try:
                    key = _radar_disk_key(source,None if site=='-' else site,scan,int(z),int(x),int(y[:-4]),parts[2]==_radar_render_revision(True))
                    with self._radar_lock:
                        record = self._radar_disk_inventory.records.get(key)
                        if record is None:
                            continue
                        try:
                            _radar_tile_metadata(record[0],source)
                        except (OSError, ValueError, KeyError, TypeError):
                            self._radar_invalidate_tile(key)
                except ValueError:
                    continue
        except (OSError, ValueError, TypeError):
            pass


    def _radar_start_inventory(self):
        with self._radar_lock:
            if self._radar_cache_thread is not None:
                return
            root = Path(RADAR_DIR)
            def bootstrap():
                try:
                    self._radar_migrate_cache(str(root))
                    self._radar_disk_inventory.scan(root/'t'/_radar_render_revision(), _radar_tile_metadata)
                    smooth_root = root/'t'/_radar_render_revision(True)
                    if self._radar_disk_inventory.writable and smooth_root.is_dir():
                        first = self._radar_disk_inventory.startup
                        self._radar_disk_inventory.scan(smooth_root, _radar_tile_metadata, suffix=(True,),
                            entry_limit=self._radar_disk_inventory.MAX_ENTRIES-first['entries'])
                        second = self._radar_disk_inventory.startup
                        self._radar_disk_inventory.startup = {k:first[k]+second[k] for k in
                            ('entries','files','invalid','wallSec','cpuSec')}
                        self._radar_disk_inventory.startup['bounded'] = first['bounded'] or second['bounded']
                    self._radar_disk_files=len(self._radar_disk_inventory)
                    self._radar_disk_bytes=self._radar_disk_inventory.bytes
                except OSError as error:
                    Logger.warning(f'almanac_emit: radar inventory startup failed - {error}')
                finally:
                    self._radar_cache_ready.set()
            self._radar_cache_thread = _InventoryThread(target=bootstrap, name='radar-inventory', daemon=True)
            self._radar_cache_thread.start()

    def _radar_migrate_cache(self, radar_dir=None):
        import shutil
        from lib.radar_basemap import publish_revision,remove_empty_parents
        root=Path(radar_dir or RADAR_DIR);root.mkdir(parents=True,exist_ok=True)
        site_revision=_radar_sites_revision();site_path=root/('sites-'+site_revision+'.json')
        marker=root/'.sites-revision'
        if not marker.exists() or marker.read_text()!=site_revision or not site_path.is_file():
            sites=[dict(id=i,lat=a,lon=b,name=n) for i,(a,b,n) in _NEXRAD_SITES.items()]
            temp=site_path.with_suffix('.tmp');temp.write_text(json.dumps(sites,separators=(',',':')));os.replace(temp,site_path)
            marker.write_text(site_revision)
            for obsolete in root.glob('sites-*.json'):
                if obsolete!=site_path:obsolete.unlink()
            (root/'sites.json').unlink(missing_ok=True)
        smooth_revision = _radar_render_revision(True)
        for obsolete in (root/'t').glob('*'):
            if (obsolete.name not in (_radar_render_revision(),smooth_revision)
                    and obsolete.is_dir() and not obsolete.is_symlink()):
                shutil.rmtree(obsolete)
        (root/'.smooth-revision').write_text(smooth_revision)
        revision=root/'.tile-revision'
        if revision.exists() and revision.read_text()==_radar_render_revision():return
        for name in ('basemap','t',*{s['legend']['id'] for s in _RADAR_SOURCES.values()}):
            path=root/name
            if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
        for obsolete in (root/'geo').glob('*/*/*/*/*.bin'):
            obsolete.unlink(missing_ok=True);remove_empty_parents(obsolete,root/'geo')
        revision.write_text(_radar_render_revision())
        publish_revision(str(root))

    def _radar_failed_pass(self, source, error, ctx):
        """Only consecutive provider failures may advance the fallback chain."""
        local = (failure_class(error) != 'host' or ctx.get('local_failure')
                 or self._radar_health.local_failures > ctx.get('local_failure_start', self._radar_health.local_failures))
        if local:
            self._radar_transport_failures.pop(source, None)
        else:
            self._radar_transport_failures[source] = self._radar_transport_failures.get(source, 0)+1
        # A dead route or resolver never opens a host breaker (HostHealth.record
        # returns before sampling), so without its own backoff a network outage
        # would rerun a doomed pass every two seconds for as long as it lasts.
        # Ambiguous failures (a reused socket that got no bytes) may be the
        # provider stalling, so they keep the prompt two-second retry.
        truly_local = (failure_class(error) == 'local' or ctx.get('local_failure')
                       or self._radar_health.local_failures > ctx.get('local_failure_start', self._radar_health.local_failures))
        self._radar_local_failure_streak = self._radar_local_failure_streak+1 if truly_local else 0
        if not local and self._radar_transport_failures[source] >= 3:
            return True
        self._radar_retained_refresh('failed')
        probe = self._radar_health.probe_delay({source}) or 0
        backoff = min(RADAR_LOCAL_RETRY_MAX_SEC, 2 ** min(self._radar_local_failure_streak, 6)) if truly_local else 2
        self._radar_budget_retry(source, 1, min_delay=max(backoff, probe))
        return False

    def _radar_refuse_dark_site(self, ctx):
        nearest = ctx.get('nexrad')
        state = self._radar_site_status.get(nearest['id'], {}) if nearest else {}
        if (ctx.get('source_pref') != 'site' or ctx.get('source_fallback') == 'site-zoom-floor'
                or state.get('checkedTs') is None or state.get('reason') != 'not reporting' or state.get('newestTs') is not None
                or self._radar_result.source_mode != 'mosaic' or not self._radar_result.frames):
            return False
        self._radar_checkpoint(ctx)
        choices = [dict(s) for s in ctx['sources']]
        choices[1].update(available=False, reason='not reporting')
        with self._radar_lock:
            self._radar_result = self._radar_result._replace(source_pref='site',
                source_fallback='site-not-reporting', sources=tuple(choices))
            self._radar_pending = {}
            self._radar_refresh = dict(state='idle', reason='not reporting',
                intent=dict(ctx['intent']), frameIndex=0, frameTotal=0, pending={}, nextRetry=None)
        return True

    def _do_radar(self, intent_triggered=None, view_started=False, discovery=False):
        """Primary-first orchestration; radar failures never alter engine health."""
        pass_deadline = time.monotonic() + RADAR_BUILD_DEADLINE_SEC
        self._radar_probe_reuse.clear()
        stamp_names = self._radar_stamp_names()
        stamp = self._radar_preference_stamp(stamp_names)
        if intent_triggered is None:
            # Direct callers follow changed markers; scheduled/retry callbacks
            # explicitly force validation even if an intent arrived meanwhile.
            intent_triggered = stamp != self._radar_zoom_stamp or self._radar_restart
        self._radar_restart = False
        self._radar_warm_pending = False
        self._radar_zoom_stamp = stamp
        self._radar_start_inventory()
        # Production starts this at boot, 60s before the provider. An early tap
        # yields the lane while the bounded scanner finishes; no cache I/O here.
        if not self._radar_cache_ready.wait(0 if 'radar' in self._inflight else 30):
            self._schedule_retry('radar', self._check_radar, .1)
            return
        self._radar_consume_bad_tiles()
        if view_started and stamp == self._radar_result_stamp:
            previous = self._radar_result
            if (previous.available and previous.ts_frame and 0 <= time.time()-previous.ts_frame < previous.stale_sec
                    and len(previous.frames)>=8 and all(f['complete'] for f in previous.frames[-8:])
                    and self._radar_inventory_valid(previous) and self._radar_idle_context is not None):
                self._radar_retained_refresh('idle')
                # Publish the retained loop first. Resume the idle tier on the
                # next watcher tick, in the same single-flight radar lane.
                self._radar_warm_pending = True
                self._radar_log_pass(pass_deadline-RADAR_BUILD_DEADLINE_SEC)
                return
        try:
            config = getattr(self.app, 'config', {}) or {}
            lat = _num(_cfg(config, 'Station', 'Latitude'))
            lon = _num(_cfg(config, 'Station', 'Longitude'))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self._radar_result = _RADAR_NONE._replace(reason='no location')
                self._radar_retained_refresh('failed')
                return
            previous = self._radar_result
            station_lat, station_lon = lat, lon
            station = (lat, lon)
            if getattr(self, '_radar_station', station) != station:
                self._radar_result = previous = _RADAR_NONE  # a changed station must never inherit old pixels
            self._radar_station = station
            try:
                with open(os.path.join(os.path.dirname(self.output_path), 'radar_center')) as preference:
                    raw = preference.read(1024)
                override = parse_center(raw.strip()) if len(raw) < 1024 else None
                if override is not None:
                    lat, lon = override
            except (OSError, ValueError, UnicodeError):
                pass  # absent/station/invalid: station viewport
            intent_record = self._radar_read_intent()
            if intent_record is not None:
                selected = intent_record['center']
                lat, lon = station if selected == 'station' else (selected['lat'], selected['lon'])
            center = dict(lat=lat, lon=lon)
            centered = lat == station_lat and lon == station_lon
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                self._radar_result = _RADAR_NONE._replace(reason='compositor unavailable')
                self._radar_retained_refresh('failed')
                return
            auto_zoom = _radar_zoom_for(station_lat)
            desired = None
            try:
                with open(os.path.join(os.path.dirname(self.output_path), 'radar_zoom')) as preference:
                    raw = preference.read(128)
                value = raw.strip()
                if len(raw) < 128 and re.fullmatch(r'[0-9]{1,2}', value):
                    level = int(value)
                    if RADAR_MIN_ZOOM <= level <= max(s['max_zoom'] for s in _RADAR_SOURCES.values()):
                        desired = level
            except (OSError, ValueError, UnicodeError):
                pass  # absence/auto/invalid all mean latitude-auto
            if intent_record is not None:
                desired = None if intent_record['zoom']=='auto' else intent_record['zoom']
            viewed = self._radar_is_viewed()
            unit = _radar_distance_unit(config)
            # One budget spans both attempts; geometry is source-specific, intent is not.
            try:
                with Path(self.output_path).with_name('radar_smooth').open() as preference:
                    raw = preference.read(128)
                smooth = len(raw) < 128 and raw.strip() == 'on'
            except (OSError, UnicodeError):
                smooth = False
            ctx = dict(listing_results={}, smooth=smooth, center=center, nexrad=_radar_nexrad(station_lat, station_lon, unit), viewed=viewed,
                desired=desired, auto_zoom=auto_zoom, builds=0, station=station, unit=unit, previous_result=previous,
                preference_stamp=stamp, stamp_names=stamp_names, intent_triggered=intent_triggered, discovery=discovery,
                deadline=pass_deadline, pass_deadline=pass_deadline, inventory=self._radar_disk_inventory, manifest_cache=self._radar_manifest_cache)
            self._radar_negative = dict(list((k, v) for k, v in self._radar_negative.items() if v > time.monotonic())[-512:])
            for key in list(self._radar_metadata):
                if time.monotonic()-self._radar_metadata_at.get(key, 0) > 3600:
                    self._radar_metadata.pop(key, None); self._radar_metadata_at.pop(key, None)
            adapters = [('rainviewer', self._radar_rainviewer_frames)]
            if _radar_iem_eligible(station_lat, station_lon):
                adapters.insert(0, ('iem-mrms-lcref', self._radar_iem_frames))
            site = ctx['nexrad']
            site_ok = bool(_radar_iem_eligible(station_lat, station_lon) and site and site['distanceMeters'] <= RADAR_SITE_RANGE_METERS)
            ctx['sources'] = [dict(mode='mosaic', available=True),
                dict(mode='site', siteId=site['id'] if site else None, available=site_ok,
                     reason=None if site_ok else 'no site in range')]
            preference = 'mosaic'
            try:
                preference = Path(os.path.join(os.path.dirname(self.output_path), 'radar_source')).read_text()[:128].strip()
            except (OSError, UnicodeError):
                pass
            if intent_record is not None:
                preference = intent_record['source']
            fallback = preference == 'site' and (desired if desired is not None else auto_zoom) < RADAR_SITE_MIN_ZOOM
            ctx.update(source_pref='site' if preference == 'site' else 'mosaic',
                       source_fallback='site-zoom-floor' if fallback else None)
            if preference == 'site' and site_ok and not fallback:
                adapters.insert(0, ('iem-nexrad-n0b', self._radar_site_frames))
            try:
                raw_seq = Path(os.path.join(os.path.dirname(self.output_path), 'radar_intent')).read_text().strip()
                seq = int(raw_seq) if re.fullmatch(r'[0-9]{1,12}', raw_seq) else 0
            except (OSError, UnicodeError):
                seq = 0
            ctx['intent'] = intent_record or dict(seq=seq, zoom=desired if desired is not None else 'auto',
                                 source=ctx['source_pref'],
                                 center='station' if centered else dict(center))
            self._radar_checkpoint(ctx)
            # Refresh closest-site evidence on Region's existing discovery wakeup,
            # including unchanged MRMS stamps and unviewed/zoom-below-seven maps.
            if discovery and previous.source_mode == 'mosaic' and site_ok:
                camera_zoom = desired if desired is not None else auto_zoom
                check = dict(ctx, zoom=min(camera_zoom, previous.max_zoom), camera_zoom=camera_zoom)
                stamps = [f['ts'] for f in previous.frames[-(8 if viewed else 1):]]
                reserve = max(RADAR_HISTORY_RESERVE, self._radar_mandatory_reserve(previous.source_id, check, stamps))
                check['request_reserve'] = reserve
                if not self._radar_headroom_delay('iem-nexrad-n0b', reserve+1):
                    if self._radar_session is None or self._radar_provider != 'iem':
                        if self._radar_session is not None:
                            self._radar_session.close()
                        self._radar_session = RadarSession()
                        self._radar_provider = 'iem'
                    self._radar_session.begin_pass(pass_deadline)
                    self._radar_session.on_retry = lambda end, first_byte=False: self._radar_transport_retry(
                        'iem-nexrad-n0b', end, reserve=reserve, first_byte=first_byte)
                    try:
                        self._radar_site_listing(check, dict(site))
                    except _RadarBudget:
                        pass  # preserve unknown/last evidence until the next cadence
            if self._radar_refuse_dark_site(ctx):
                if not discovery:
                    return
                # Refusal must not stop the Region radar from refreshing.
                adapters = [pair for pair in adapters if pair[0] != 'iem-nexrad-n0b']
                ctx['source_fallback'] = 'site-not-reporting'
                ctx['sources'][1].update(available=False, reason='not reporting')
            same_mode = previous.available and previous.source_pref == ctx['source_pref'] and previous.source_fallback == ctx['source_fallback']
            if (same_mode and previous.source_id in dict(adapters) and previous.source_id != adapters[0][0]
                    and time.monotonic()-self._radar_source_since < 300
                    and self._radar_transport_failures.get(previous.source_id, 0) < 3):
                active = next(pair for pair in adapters if pair[0] == previous.source_id)
                adapters = [active]  # five-minute dwell includes transport recovery probes
            errors = []
            for source, adapter in adapters:
                ctx.pop('local_failure', None)
                ctx['local_failure_start'] = self._radar_health.local_failures
                ctx.pop('staging_source', None)
                ctx['switch_reason'] = 'user source/zoom selection' if not same_mode else 'initial source selection'
                if errors:
                    ctx['switch_reason'] = '; '.join(errors)
                if previous.frames and source != previous.source_id:
                    ctx['staging_source'] = source
                    ctx['switch_reason'] = '; '.join(errors) or 'preferred source recovered after 300s dwell'
                if self._radar_cooldowns.get(source, 0) > time.monotonic():
                    self._radar_retained_refresh('failed')
                    self._radar_budget_retry(source, 1)
                    return
                ctx['deadline'] = min(pass_deadline, time.monotonic()+RADAR_SOURCE_DEADLINE_SEC)
                ctx.pop('hedge_budget', None)
                ctx.pop('missing_tiles', None)
                ctx.pop('retained_failed', None)
                try:
                    probes = self._radar_health.probes(source)
                except CircuitOpen as error:
                    if source == 'iem-nexrad-n0b':
                        ctx['sources'][1].update(available=False, reason='scan unavailable')
                    errors.append(str(error))
                    if not self._radar_failed_pass(source, error, ctx):
                        return
                    continue
                zoom = max(RADAR_SITE_MIN_ZOOM if source == 'iem-nexrad-n0b' else RADAR_MIN_ZOOM, min(desired if desired is not None else auto_zoom,
                                               _RADAR_SOURCES[source]['max_zoom']))
                camera_zoom = desired if desired is not None else auto_zoom
                tiles, mpp, bounds, _ = _radar_viewport(lat, lon, zoom,
                    RADAR_VIEWPORT_W*2**(zoom-camera_zoom), RADAR_VIEWPORT_H*2**(zoom-camera_zoom))
                ctx['camera_zoom'] = camera_zoom
                bar, rings = _radar_scale(mpp, RADAR_VIEWPORT_PX, unit, max_fraction=.25)
                identity = (lat, lon, zoom, source)
                with self._radar_lock:
                    geometry = (identity, stamp)
                    if self._radar_view_geometry != geometry:
                        self._radar_view_geometry = geometry
                        self._radar_geometry_since = time.monotonic()
                ctx.update(zoom=zoom, tiles=tiles, mpp=mpp, bounds=bounds, bar=bar, rings=rings,
                           identity=identity, candidates=[])
                ctx['tiles'] = sorted(_radar_grid(ctx),key=lambda t: math.hypot(t[0]+.5-world_point(lat,lon,zoom)[0]/256,t[1]+.5-world_point(lat,lon,zoom)[1]/256))
                from lib.radar_basemap import version
                ctx['geo'] = dict(version=version(),base='radar/geo/',sites='radar/sites-'+_radar_sites_revision()+'.json')
                # Source/station knowledge can exist before the first tile. No camera acknowledgement.
                if not self._radar_result.available:
                    self._radar_result = _RADAR_NONE._replace(available=True,reason=None,
                        center=dict(lat=station_lat,lon=station_lon), zoom=zoom, nexrad=ctx['nexrad'],
                        source_id=source, **_RADAR_SOURCES[source], zoom_desired=desired,
                        zoom_auto_level=auto_zoom, geo=ctx['geo'], units=unit, rings=rings,
                        sources=tuple(ctx['sources']),source_pref=ctx['source_pref'],
                        source_fallback=ctx['source_fallback'])
                self._radar_publish_refresh(ctx,state='newest',frameIndex=0,frameTotal=1)
                provider = _RADAR_SOURCES[source]['provider']
                if self._radar_session is None or self._radar_provider != provider:
                    if self._radar_session is not None:
                        self._radar_session.close()
                    self._radar_session = RadarSession()
                    self._radar_provider = provider
                self._radar_session.on_retry = lambda end, first_byte=False, source=source: self._radar_transport_retry(source, end, first_byte=first_byte)
                self._radar_session.begin_pass(ctx['deadline'])
                needed = 1  # discover first; price actual missing visible layers below
                if self._radar_headroom_delay(source, 1 if discovery else needed):
                    self._radar_publish_refresh(ctx, state='idle')
                    self._radar_budget_retry(source, needed)
                    return
                started = time.monotonic()
                try:
                    for probe_url, is_metadata in probes:
                        raw = self._radar_request(source, probe_url, ctx['deadline'],
                            method='GET' if is_metadata else 'HEAD', metadata=True)
                        if is_metadata:
                            self._radar_probe_reuse[probe_url] = raw
                    ctx['tile_workers'] = RADAR_NEWEST_TILE_WORKERS
                    ctx['reuse_newest'] = False
                    try:
                        adapter(ctx)
                    except _RadarRevalidate:
                        self._radar_forget(source)
                        ctx.update(intent_triggered=False, reuse_newest=False)
                        ctx.pop('site_reasons', None)
                        adapter(ctx)  # same deadline, build count and rolling request gate
                    if source == 'iem-nexrad-n0b' and self._radar_refuse_dark_site(ctx):
                        return
                    if not ctx.get('retained_failed'):
                        self._radar_transport_failures.pop(source, None)
                        self._radar_local_failure_streak = 0
                    try:
                        self._radar_prune(previous)
                    except OSError as error:
                        Logger.warning(f'almanac_emit: radar cache prune failed - {error}')
                    if ctx.get('retained_failed'):
                        self._radar_forget(source)
                        self._radar_retained_refresh('failed')
                    else:
                        self._radar_publish_refresh(ctx, state='idle')
                    probe_delay = self._radar_probe_delay()
                    if probe_delay is not None:
                        self._schedule_retry('radar', self._check_radar, max(1, probe_delay))
                    return
                except _RadarUnchanged:
                    self._radar_transport_failures.pop(source, None)
                    self._radar_local_failure_streak = 0
                    self._radar_retained_refresh('idle')
                    return
                except _RadarSuperseded:
                    raise
                except _RadarBudget:
                    # Local rate capacity does not erase earlier service failures.
                    pass
                    self._radar_forget(source)
                    fresh = previous.available and previous.ts_frame is not None and 0 <= time.time()-previous.ts_frame < previous.stale_sec
                    if self._radar_result.ts_frame is None:
                        self._radar_result = previous
                    self._radar_retained_refresh('idle' if fresh else 'failed')
                    self._radar_budget_retry(source, needed)
                    return
                except Exception as error:
                    if source == 'iem-nexrad-n0b' and self._radar_refuse_dark_site(ctx):
                        return
                    self._radar_health.last_error = str(error) or type(error).__name__
                    self._radar_forget(source)
                    self._radar_checkpoint(ctx)
                    self._radar_session.close()
                    self._radar_session = None
                    errors.append(str(error))
                    Logger.warning(f'almanac_emit: radar {source} failed: {type(error).__name__}: {error}; '
                                   f'candidates={ctx["candidates"]}; elapsed={time.monotonic()-started:.3f}s')
                    if source == 'iem-nexrad-n0b':
                        ctx['sources'][1].update(available=False, reason=ctx.get('site_failure', 'scan unavailable'))
                    if not self._radar_failed_pass(source, error, ctx):
                        if same_mode and previous.source_id != source:
                            continue  # recovery failed; refresh the active fallback
                        return
                    errors[-1] = f'{source}: 3 consecutive failed passes ({type(error).__name__}: {error})'
            raise ValueError('; '.join(errors))
        except _RadarSuperseded:
            self._radar_restart = True
            # The worker's single-flight guard releases before its immediate wakeup.
            # The 100 ms watcher also sees the unserved preference stamp.
            with self._life_lock:
                retry = self._retries.pop('radar', None)
                if retry is not None:
                    retry.cancel()
                    if retry in self._events:
                        self._events.remove(retry)
        except Exception as error:
            if self._radar_result.ts_frame is None:
                self._radar_result=self._radar_result._replace(available=self._radar_result.center is not None,reason='no radar tiles')
            if 'ctx' in locals():
                if self._radar_result.available:
                    self._radar_retained_refresh('failed')
                else:
                    self._radar_publish_refresh(ctx, state='failed')
            self._radar_health.last_error = str(error) or type(error).__name__
            Logger.warning(f'almanac_emit: radar fetch failed - {error}')
            probe_delay = self._radar_probe_delay()
            self._schedule_retry('radar', self._check_radar,
                RADAR_RETRY_SEC if probe_delay is None else max(1, probe_delay))

        finally:
            if any(self._radar_pending.get(k) for k in ('newest','four','eight')) and not self._radar_restart and 'radar' not in self._retries:
                self._radar_budget_retry(self._radar_result.source_id, 1)
            self._radar_arm_discovery()
            self._radar_log_pass(pass_deadline-RADAR_BUILD_DEADLINE_SEC)
            if not self._running and self._radar_session is not None and 'radar' in self._inflight:
                self._radar_session.close()
                self._radar_session = None

    @staticmethod
    def _radar_payload(snap, now, tz, refresh=None):
        def local(ts):
            return datetime.fromtimestamp(ts,tz).strftime('%H:%M') if ts is not None else None
        complete = [f['ts'] for f in snap.frames if f['complete']]
        gaps = [b-a for a,b in zip(complete,complete[1:]) if b>a]
        age = int(now-snap.ts_frame) if snap.ts_frame is not None else None
        factor = 1609.344 if snap.units == 'mi' else 1000
        choices = [dict(meters=d*factor,label=f'{d} {snap.units}') for d in (5,10,20,25,50,100,150,200,250)]
        nearest = None
        if snap.nexrad:
            nearest = dict(reporting=None, newestTs=None, reason=None, checkedTs=None, nextCheckTs=None)
            nearest.update(snap.nexrad)
            nearest.update(ageSec=max(0, int(now-nearest['newestTs'])) if nearest['newestTs'] is not None else None,
                           checkedAt=local(nearest['checkedTs']), nextCheckAt=local(nearest['nextCheckTs']))
        tiles = dict(snap.tiles or {})
        tiles['frames'] = [{k:v for k,v in dict(f,at=local(f['ts'])).items() if k not in ('complete','publishable','acquiredSites')}
                           for f in tiles.get('frames',())]
        return dict(available=snap.available,reason=snap.reason,geo=snap.geo,tiles=tiles,
            intent=tiles.get('intent', {}), geometry=tiles.get('geometry'), camera=tiles.get('camera'),
            advertisedTs=max((f['ts'] for f in snap.frames), default=None), acquiredTs=snap.ts_frame,
            pending=(refresh or {}).get('pending', {}), nextRetry=(refresh or {}).get('nextRetry'),
            switchDeadlineSec=20,
            sourceMode=snap.source_mode,siteId=snap.site_id,sources=list(snap.sources),
            sites=[dict(s,ageSec=int(now-s['newestTs']) if s['newestTs'] is not None else None) for s in snap.sites],
            sitesConsidered=snap.sites_considered,sitesDrawn=len(snap.sites),
            refresh=refresh or dict(state='idle',frameIndex=0,frameTotal=0),
            sourcePref=snap.source_pref,sourceFallback=snap.source_fallback,
            sitePreferred=snap.source_pref=='site',siteResumeZoom=RADAR_SITE_MIN_ZOOM,
            scanningSlowly=snap.scanning_slowly,latestOnly=snap.source_mode=='site' and snap.scan_cadence_sec is None and len(snap.frames)==1,
            scanCadenceSec=snap.scan_cadence_sec,scanMode=snap.scan_mode,scanModeSource=snap.scan_mode_source,
            sourceId=snap.source_id,attribution=snap.attribution,attributionUrl=snap.attribution_url,
            provider=snap.provider,cadenceSec=snap.cadence,frameSpacingSec=median(gaps) if gaps else None,
            historyGaps=any(g!=snap.cadence for g in gaps),historySpanSec=complete[-1]-complete[0] if complete else 0,
            completeFrameCount=len(complete),partialCoverage=snap.partial_coverage,center=snap.center,
            smooth=(snap.tiles or {}).get('smooth',False),
            zoomAuto=snap.zoom_desired is None,zoomAutoLevel=snap.zoom_auto_level,
            zoomMin=RADAR_MIN_ZOOM,zoomMax=snap.max_zoom,
            zoomSource='MRMS' if snap.source_id=='iem-mrms-lcref' else 'NEXRAD' if snap.source_mode=='site' else 'RainViewer',
            zoomCapped=(snap.zoom_desired if snap.zoom_desired is not None else snap.zoom_auto_level)!=snap.zoom,
            zoomDesired=snap.zoom_desired,units=snap.units,scaleChoices=choices,
            rings=[dict(meters=float(r['label'].split()[0])*factor,label=r['label']) for r in snap.rings or ()],
            frameCount=len(snap.frames),observedAt=local(snap.ts_frame),observedTs=snap.ts_frame,
            ageSec=age,staleSec=snap.stale_sec,stale=age is not None and age>=snap.stale_sec,
            fetchedAt=snap.ts_fetch,updatedAt=local(snap.ts_fetch),nexrad=nearest,legend=dict(snap.legend))

    def _check_version(self, _dt=None):
        """ Kick off a non-blocking GitHub version check on a daemon thread so a
        slow/failed request never stalls the Kivy main loop or the emit tick. """
        self._spawn('version', self._do_version_check)

    def _do_version_check(self):
        """ Compare the installed version to the latest GitHub release tag and
        cache the result. Never raises. """
        try:
            from lib.request_api import github_api
            from packaging import version as _v
            config = getattr(self.app, 'config', None)
            current = _cfg(config, 'System', 'Version')
            resp = github_api.version(config)
            if not github_api.verify_response(resp, 'tag_name'):
                return
            latest = resp.json()['tag_name']
            available = bool(current and latest) and (
                _v.parse(str(latest).lstrip('vV')) > _v.parse(str(current).lstrip('vV')))
            self._ver_result = _VerResult(available, latest, current)
        except Exception:                                                 # noqa: BLE001
            pass

    def _check_forecast(self, _dt=None):
        """ Kick off a non-blocking 7-day forecast fetch on a daemon thread. """
        self._spawn('forecast', self._do_forecast)

    def _do_forecast(self):
        """ Fetch the 7-day daily outlook from Open-Meteo for the station's
        lat/lon: hi/lo, WMO weather code, max precipitation probability. The
        temperature unit follows the console's own Units/Temp setting so the
        strip always matches the observed readings. The SAME call also carries
        hourly temperature (forecast_hours, no extra round trip) for the hero
        curve's forward trajectory. Off-thread, never raises; on failure the
        previous outlook is kept. """
        try:
            import urllib.request
            config = getattr(self.app, 'config', {}) or {}
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            unit = 'fahrenheit' if (_cfg(config, 'Units', 'Temp') or 'c').lower() == 'f' else 'celsius'
            # Ask for precipitation in the unit the console already displays,
            # so the amount needs no conversion and cannot disagree with the
            # rainfall panel's rainUnit.
            precip_unit = 'inch' if (_cfg(config, 'Units', 'Precip') or 'mm').lower() == 'in' else 'mm'
            url = ('https://api.open-meteo.com/v1/forecast'
                   f'?latitude={lat}&longitude={lon}'
                   '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_gusts_10m_max'
                   # Hourly temperature for the hero curve. forecast_hours bounds
                   # the HOURLY block only (daily still spans forecast_days), and
                   # 48 h is the most the curve can want: at station-local
                   # midnight, "through the end of tomorrow" is exactly 48 hours.
                   '&hourly=temperature_2m&forecast_hours=48'
                   '&wind_speed_unit=kmh'
                   f'&precipitation_unit={precip_unit}'
                   f'&temperature_unit={unit}&forecast_days=7&timezone=auto')
            req = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            days = self._fc_daily_from(data.get('daily') or {})
            hours = self._fc_hourly_from(data.get('hourly') or {}, time.time(),
                                         self._station_tz(config))
            if days:
                self._fc_result = _FcResult(days, hours, time.time())
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: forecast fetch failed - {error}')
        finally:
            # Boot resilience: the hourly interval is far too slow to recover
            # from a failed FIRST fetch (the USB wifi is often still settling
            # when the t+50s attempt fires - the same failure AQI's delayed
            # start works around). Until one fetch has succeeded, retry every
            # 2 minutes; after that the hourly cadence is plenty. One chain
            # only - _schedule_retry drops the request if one is already armed.
            if self._fc_ts is None:
                self._schedule_retry('forecast', self._check_forecast, FORECAST_RETRY_SEC)

    @staticmethod
    def _fc_daily_from(daily):
        """ Shape Open-Meteo's parallel daily arrays into display-ready rows:
        [{day:'MON', hi:93, lo:56, code:3, pp:20, qpf:0.34}, ...]. Rows with a missing
        hi or lo are dropped (a partial bar lies on the shared scale). Pure;
        never raises. """
        times = daily.get('time') or []
        his   = daily.get('temperature_2m_max') or []
        los   = daily.get('temperature_2m_min') or []
        codes = daily.get('weather_code') or []
        pps   = daily.get('precipitation_probability_max') or []
        # Amount, already in the console's precipitation unit (see the
        # precipitation_unit above), so no conversion here.
        qpfs  = daily.get('precipitation_sum') or []
        gusts = daily.get('wind_gusts_10m_max') or []
        out = []
        for i, t in enumerate(times[:7]):
            hi = _num(his[i]) if i < len(his) else None
            lo = _num(los[i]) if i < len(los) else None
            if hi is None or lo is None:
                continue
            try:
                day = datetime.fromisoformat(t).strftime('%a').upper()
            except (ValueError, TypeError):
                continue
            code = _num(codes[i]) if i < len(codes) else None
            pp   = _num(pps[i])   if i < len(pps)   else None
            qpf  = _num(qpfs[i]) if i < len(qpfs) else None   # _num: never raises on junk
            gust = _num(gusts[i]) if i < len(gusts) else None
            out.append({'day':  day,
                        'date': t[:10],
                        'hi':   int(round(hi)),
                        'lo':   int(round(lo)),
                        'code': int(round(code)) if code is not None else None,
                        'pp':   int(round(pp)) if pp is not None else None,
                        # two decimals covers both units; the board hides
                        # anything below what the unit can print
                        'qpf':  round(qpf, 2) if qpf is not None else None,
                        'gust': int(round(gust)) if gust is not None else None})   # km/h, fixed unit
        return out

    @staticmethod
    def _fc_hourly_from(hourly, now, tz):
        """ Shape Open-Meteo's hourly temperature into the hero curve's forward
        trajectory: [[epoch_seconds, temp], ...] from the CURRENT station-local
        hour through the end of TOMORROW (48 points at most). Temperatures are
        already in the console's unit (see temperature_unit on the request), so
        no conversion here - only a round to 1 decimal, matching the observed
        readings the curve is drawn against.

        Under timezone=auto the times come back local-naive, so the station tz
        is what turns them into epochs; without a tz there is no honest epoch to
        publish and the list stays empty (the console then draws no forecast
        segment at all rather than one an hour out of place). Pure; never raises. """
        times = hourly.get('time') or []
        temps = hourly.get('temperature_2m') or []
        if tz is None:
            return []
        try:
            ref = datetime.fromtimestamp(now, tz).replace(tzinfo=None)    # station wall clock
        except (OverflowError, OSError, ValueError):
            return []
        # Window on the wall clock, not on epochs: DST cannot make "the end of
        # tomorrow" a fixed number of hours away.
        first = ref.replace(minute=0, second=0, microsecond=0)
        last  = ref.date() + timedelta(days=1)
        out = []
        for t, v in zip(times, temps):
            v = _num(v)                               # _num: never raises on junk
            if v is None:
                continue
            try:
                dt = datetime.fromisoformat(t)
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is not None:                 # not what timezone=auto sends; don't mix clocks
                dt = dt.astimezone(tz).replace(tzinfo=None)
            if dt < first or dt.date() > last:
                continue
            try:
                epoch = tz.localize(dt).timestamp()
            except Exception:                                             # noqa: BLE001
                continue                              # ambiguous/nonexistent local hour (DST edge)
            out.append([int(round(epoch)), round(v, 1)])
        return out[:48]

    WINDY_GUST_KMH = 45   # ~28 mph gusts: the day is a wind story

    @classmethod
    def _tomorrow_hint(cls, fc_rows):
        """ One quiet line about tomorrow, only when tomorrow is a story:
        "Thunderstorms tomorrow" / "Snow tomorrow" / "Rain tomorrow" /
        "Windy tomorrow" / "Fog tomorrow". Ordinary days say nothing -
        absence is information. Selects the tomorrow row by its station-local
        calendar date, never its position in a partially shaped array. Never raises. """
        try:
            today = next((row for row in fc_rows if row.get('today')), None)
            if today is None:
                return None
            today_date = today.get('date')
            if today_date:
                tomorrow_date = (datetime.fromisoformat(today_date).date() + timedelta(days=1)).isoformat()
                t = next((row for row in fc_rows if row.get('date') == tomorrow_date), None)
            else:
                # Compatibility for callers that predate the date field: select
                # by weekday label, not adjacent list position.
                names = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')
                try:
                    tomorrow_day = (names.index(today.get('day')) + 1) % len(names)
                except ValueError:
                    return None
                t = next((row for row in fc_rows if row.get('day') == names[tomorrow_day]), None)
            if t is None:
                return None
            code, gust = t.get('code'), t.get('gust')
            if code is not None and code >= 95:
                return 'Thunderstorms tomorrow'
            windy = gust is not None and gust >= cls.WINDY_GUST_KMH
            if code in (71, 73, 75, 77, 85, 86):
                return 'Blowing snow tomorrow' if windy else 'Snow tomorrow'
            if code in (56, 57, 66, 67):
                return 'Freezing rain tomorrow'   # the one rain worth distinguishing: it's a hazard
            if code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
                return 'Wind-driven rain tomorrow' if windy else 'Rain tomorrow'
            if gust is not None and gust >= cls.WINDY_GUST_KMH:
                return 'Windy tomorrow'
            if code in (45, 48):
                return 'Fog tomorrow'
        except Exception:                                                 # noqa: BLE001
            pass
        return None

    # The core's barometer outlook sentences are too long for the ledger row
    # ("Becoming clearer and cooler" wrapped, then overran). The vocabulary is
    # closed, so map to the compact editorial forms the design contract always
    # showed ("Unchanged"); unknown strings pass through untouched.
    _OUTLOOK_COMPACT = {
        'Conditions unchanged':        'Unchanged',
        'Fair conditions likely':      'Fair conditions',
        'Rainy conditions likely':     'Rain likely',
        'Stormy conditions likely':    'Storm likely',
        'Becoming clearer and cooler': 'Clearer, cooler',
        'Becoming cloudy and warmer':  'Cloudier, warmer',
    }

    @staticmethod
    def _snowify_status(status, temp, temp_unit, fc_rows):
        """ The Tempest's haptic rain sensor cannot register snowfall, so in
        freezing weather with snow in today's forecast, 'Currently Dry' is
        the sensor's truth but not the sky's. Rewrites ONLY the dry status -
        any measured rain always wins. Never raises. """
        if status != 'Currently Dry' or temp is None:
            return status
        try:
            freezing = float(temp) <= (34.0 if (temp_unit or '').endswith('F') else 1.0)
        except (TypeError, ValueError):
            return status
        if not freezing:
            return status
        today = next((r for r in (fc_rows or []) if r.get('today')), None)
        if today and today.get('code') in (71, 72, 73, 74, 75, 76, 77, 85, 86):
            return 'Snow Likely'   # forecast-derived; the haptic sensor cannot see snow
        return status

    @staticmethod
    def _rain_rate_display(Obs, config, eff_mm=None):
        """ Numeric rain rate in display units. Index [0] is the core's
        FORMATTED display value, which for a trace rate is the STRING
        '<0.01' (in/hr) / '<0.1' (mm/hr) - unparseable, so the overlay
        showed a dash and hid the water while it was actually drizzling.
        Fall back to converting the raw mm/hr at [3] by the configured
        precip unit. When the rolling window lifts the rate above the raw
        minute (eff_mm > raw), that effective rate is what gets converted,
        so the readout agrees with the gauge. Never raises. """
        raw_mm = _num(_idx(Obs.get('RainRate'), 3))
        bridged = eff_mm is not None and raw_mm is not None and eff_mm > raw_mm
        if not bridged:
            rate = _num(_idx(Obs.get('RainRate'), 0))
            if rate is not None:
                return rate
            if raw_mm is None:
                return None
        mm = eff_mm if bridged else raw_mm
        unit = (_cfg(config, 'Units', 'Precip') or 'mm').lower()
        per_mm = {'in': 1 / 25.4, 'cm': 0.1, 'mm': 1.0}.get(unit, 1.0)
        return round(mm * per_mm, 4)

    @staticmethod
    def _rain_status_for(eff_mm, core_status):
        """ The core's intensity word, except that a minute the sensor calls
        'Currently Dry' inside a drizzle (window rate > 0) gets the word for
        the windowed rate - the same bands derived_variables.rain_rate uses. """
        if core_status != 'Currently Dry' or eff_mm is None or eff_mm <= 0:
            return core_status
        if eff_mm < 0.25: return 'Very Light Rain'
        if eff_mm < 1.0:  return 'Light Rain'
        if eff_mm < 4.0:  return 'Moderate Rain'
        if eff_mm < 16.0: return 'Heavy Rain'
        if eff_mm < 50.0: return 'Very Heavy Rain'
        return 'Extreme Rain'

    @staticmethod
    def _feels_desc(text):
        """ The core's feels-like descriptors all begin "Feeling ..."; the hero
        line already says "Feels like 62°", so the prefix doubles up on glass
        ("Feels like 62° · Feeling warm"). Drop it and keep the sentence case. """
        if not text:
            return text
        stripped = re.sub(r'^\s*Feeling\s+', '', text)
        return stripped[:1].upper() + stripped[1:] if stripped else text

    @staticmethod
    def _unify_today(fc_rows, fc_low, fc_high):
        """ The hero's LOW/HIGH come from WeatherFlow while the outlook band's
        rows come from Open-Meteo, and the two providers disagree by a degree
        or two - which reads as a contradiction when both sit on one screen
        labelled "today". One provider owns today: the band's TODAY row takes
        the WeatherFlow figures whenever they are known. Other days untouched. """
        for r in fc_rows:
            if r.get('today'):
                if fc_low is not None:
                    r['lo'] = int(round(fc_low))
                if fc_high is not None:
                    r['hi'] = int(round(fc_high))
                lo, hi = r.get('lo'), r.get('hi')
                if lo is not None and hi is not None and lo > hi:
                    r['lo'], r['hi'] = hi, lo
        return fc_rows

    def _fc_daily_current(self, today_iso, fc_daily=None):
        """ The stored outlook with any already-past days dropped, so a stale
        forecast (wifi out for a day+) can never mislabel yesterday as TODAY.
        As rows age out the band naturally shrinks below the HTML's 3-day
        minimum and hides itself - no separate staleness flag needed.
        `fc_daily` defaults to the live snapshot; the emit tick passes the one
        it already read so the rows and the staleness flag agree. """
        rows = [dict(r) for r in (self._fc_daily if fc_daily is None else fc_daily)
                if not r.get('date') or r['date'] >= today_iso]
        for r in rows:
            r['today'] = (r.get('date') == today_iso)
        return rows

    def _check_aqi(self, _dt=None):
        """ Kick off a non-blocking air-quality fetch on a daemon thread. """
        self._spawn('aqi', self._do_aqi)

    @staticmethod
    def _aqi_cat(aqi):
        """ US EPA AQI category name. """
        if aqi <= 50:   return 'Good'
        if aqi <= 100:  return 'Moderate'
        if aqi <= 150:  return 'Sensitive'          # "Unhealthy for Sensitive Groups"
        if aqi <= 200:  return 'Unhealthy'
        if aqi <= 300:  return 'Very Unhealthy'
        return 'Hazardous'

    def _do_aqi(self):
        """ Fetch US AQI for the station's lat/lon.

        When [AirQuality] WaqiToken is set in wfpiconsole.ini, uses the WAQI
        API (aqicn.org) which aggregates the nearest EPA/AirNow monitoring
        station — the same reading shown on airnow.gov.

        Falls back to Open-Meteo (CAMS satellite model, no token required)
        when no token is configured.  Off-thread, never raises. """
        try:
            import urllib.request
            config = getattr(self.app, 'config', None)
            lat   = _cfg(config, 'Station', 'Latitude')
            lon   = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            token = (_cfg(config, 'AirQuality', 'WaqiToken') or '').strip()
            if token:
                # WAQI: nearest EPA/AirNow monitoring station
                url = f'https://api.waqi.info/feed/geo:{lat};{lon}/?token={token}'
                req = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                if data.get('status') != 'ok':
                    Logger.warning(f'almanac_emit: WAQI status={data.get("status")} '
                                   f'msg={data.get("data")}')
                    return
                d   = data.get('data') or {}
                aqi = _num(d.get('aqi'))
                if aqi is None:
                    return                  # station reports '-' when sensor is offline
                aqi = int(round(aqi))
                fc_pm25 = ((d.get('forecast') or {}).get('daily') or {}).get('pm25') or []
                series, peak, peak_time, trend, trend_text, fc_cat = \
                    self._waqi_trend(aqi, fc_pm25, self._station_today(config))
                # WAQI's iaqi.pm25.v is a pollutant AQI, not a concentration, so
                # there is no PM2.5 reading to publish from this provider.
                self._aqi_result = _AqiResult(aqi, self._aqi_cat(aqi), None, time.time(),
                                              series, peak, peak_time, fc_cat, trend, trend_text)
            else:
                # Open-Meteo fallback: CAMS model, no token required
                url = ('https://air-quality-api.open-meteo.com/v1/air-quality'
                       f'?latitude={lat}&longitude={lon}&current=us_aqi,pm2_5'
                       '&hourly=us_aqi,pm2_5&forecast_days=1&timezone=auto')
                req = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                cur = data.get('current') or {}
                aqi = _num(cur.get('us_aqi'))
                if aqi is None:
                    return
                aqi = int(round(aqi))
                series, peak, peak_time, trend, trend_text, fc_cat = \
                    self._aqi_forecast_summary(data.get('hourly') or {}, time.time(),
                                               self._station_tz(config), aqi)
                self._aqi_result = _AqiResult(aqi, self._aqi_cat(aqi), _num(cur.get('pm2_5')),
                                              time.time(), series, peak, peak_time,
                                              fc_cat, trend, trend_text)
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: air-quality fetch failed - {error}')

    @staticmethod
    def _hour_label(dt):
        """ "5 PM" / "5:30 PM" from a datetime. Portable (avoids the non-BSD %-I). """
        hour12 = dt.hour % 12 or 12
        ampm = 'AM' if dt.hour < 12 else 'PM'
        return f'{hour12} {ampm}' if dt.minute == 0 else f'{hour12}:{dt.minute:02d} {ampm}'

    @staticmethod
    def _aqi_forecast_summary(hourly, now, tz, aqi_now):
        """ From Open-Meteo hourly us_aqi (local-naive ISO times + the station tz),
        build the next-hours series, the 6 h peak, and a rising/falling/steady
        trend (5-AQI deadband, band-crossing required). Pure; never raises.
        Returns (series, peak, peak_time, trend, trend_text, peak_cat). """
        times = hourly.get('time') or []
        vals  = hourly.get('us_aqi') or []
        pts = []
        for t, v in zip(times, vals):
            v = _num(v)
            if v is None:
                continue
            try:
                dt = datetime.fromisoformat(t)
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None and tz is not None:
                try:
                    dt = tz.localize(dt)
                except Exception:                                         # noqa: BLE001
                    dt = dt.replace(tzinfo=timezone.utc)
            epoch = dt.timestamp() if dt.tzinfo else None
            pts.append((epoch, dt, int(round(v))))
        future = [(e, d, v) for (e, d, v) in pts if e is None or e >= now - 1800][:12]
        if not future:
            return [], None, None, None, None, None
        series = [[int(e), v] for (e, d, v) in future if e is not None]
        window = [(e, d, v) for (e, d, v) in future if e is None or e <= now + 6 * 3600] or future
        _, peak_dt, peak = max(window, key=lambda x: x[2])
        peak_cat  = AlmanacEmitter._aqi_cat(peak)
        peak_time = AlmanacEmitter._hour_label(peak_dt)
        base = aqi_now if aqi_now is not None else future[0][2]
        low  = min(v for (_, _, v) in future)
        if peak - base >= 5 and peak_cat != AlmanacEmitter._aqi_cat(base):
            trend, trend_text = 'rising', f'{peak_cat} by {peak_time}'
        elif base - low >= 5 and AlmanacEmitter._aqi_cat(low) != AlmanacEmitter._aqi_cat(base):
            trend, trend_text = 'falling', 'Improving'
        else:
            trend, trend_text = 'steady', None
        return series, peak, peak_time, trend, trend_text, peak_cat

    @staticmethod
    def _waqi_trend(aqi_now, fc_pm25_daily, today_iso):
        """ Derive rising/falling/steady from WAQI's dated daily PM2.5 forecast.
        Returns the same 6-tuple as _aqi_forecast_summary so callers are
        unchanged.  No hourly series, so aqiForecast sparkline is empty. """
        if not fc_pm25_daily or aqi_now is None or not today_iso:
            return [], None, None, None, None, None
        try:
            tomorrow_iso = (datetime.fromisoformat(today_iso).date() + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            return [], None, None, None, None, None
        today = next((row for row in fc_pm25_daily if row.get('day') == today_iso), {})
        tomorrow = next((row for row in fc_pm25_daily if row.get('day') == tomorrow_iso), {})
        peak_raw = _num(today.get('max'))
        if peak_raw is None:
            return [], None, None, None, None, None
        peak     = int(round(peak_raw))
        peak_cat = AlmanacEmitter._aqi_cat(peak)
        cur_cat  = AlmanacEmitter._aqi_cat(aqi_now)
        nxt_avg  = _num(tomorrow.get('avg'))
        if peak - aqi_now >= 5 and peak_cat != cur_cat:
            return [], peak, None, 'rising',  f'{peak_cat} today', peak_cat
        if nxt_avg is not None:
            nxt = int(round(nxt_avg))
            if aqi_now - nxt >= 5 and AlmanacEmitter._aqi_cat(nxt) != cur_cat:
                return [], peak, None, 'falling', 'Improving', peak_cat
        return [], peak, None, 'steady', None, peak_cat

    # --------------------------------------------------------------------
    # Weather alerts (NWS api.weather.gov, by station lat/lon)
    # --------------------------------------------------------------------
    def _check_alerts(self, _dt=None):
        """ Kick off a non-blocking NWS alerts fetch on a daemon thread. """
        self._spawn('alerts', self._do_alerts)

    def _do_alerts(self):
        """ Fetch active NWS alerts for the station's lat/lon. Off-thread, never
        raises; keeps the last-good list on a transient failure (staleness is
        flagged in the payload rather than silently shown as fresh).

        NWS (api.weather.gov) only covers the US and its territories. A point
        outside that coverage returns HTTP 400 "out of bounds" (400/404) — that
        is NOT a failure, it just means there are no NWS alerts here, so we clear
        the list and mark it freshly-fetched (no repeated warnings, never stale).
        Non-US stations therefore simply show no alert strip; the AQI block still
        works worldwide (Open-Meteo computes us_aqi globally). """
        try:
            import urllib.request
            import urllib.error
            config = getattr(self.app, 'config', None)
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if not lat or not lon:
                return
            contact = (_cfg(config, 'Station', 'Contact')
                       or os.environ.get('ALMANAC_CONTACT') or ALERTS_UA_FALLBACK)
            url = f'https://api.weather.gov/alerts/active?point={lat},{lon}'
            req = urllib.request.Request(url, headers={
                'User-Agent': contact, 'Accept': 'application/geo+json'})
            try:
                with urllib.request.urlopen(req, timeout=ALERTS_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as http_error:
                if http_error.code in (400, 404):
                    # outside NWS coverage (non-US) — benign: no alerts here
                    self._alerts_result = _AlertsResult([], [], time.time())
                    return
                raise
            feats = [f.get('properties') or {} for f in (data.get('features') or [])]
            now = time.time()
            self._alerts_result = _AlertsResult(
                feats, self._process_alerts(feats, now, self._station_tz(config)), now)
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: alerts fetch failed - {error}')

    @staticmethod
    def _alert_level(event):
        """ NWS product level from the last word of the event name. 'Alert' products
        bucket as advisory-tier; unknown products as statement-tier. Returns
        (level_int, level_str) with higher int = more urgent. """
        words = (event or '').strip().lower().split()
        last = words[-1] if words else ''
        if last in _ALERT_LEVEL:
            lvl = _ALERT_LEVEL[last]
        else:
            # products like "Small Craft Advisory for Hazardous Seas" or
            # "911 Telephone Outage Emergency" carry their tier mid-name;
            # take the most urgent tier word found anywhere, else statement
            found = [_ALERT_LEVEL[w] for w in words if w in _ALERT_LEVEL]
            lvl = max(found) if found else 1
        return lvl, _ALERT_LEVELNAME[lvl]

    @staticmethod
    def _alert_tone(event, level):
        """ Banner colour token, derived from the level with a 2-rule hazard override:
        (A) air-quality/smoke/red-flag/fire-weather pin to amber at any level;
        (B) routine water advisories (wind/flood/winter/fog/coastal/gale/surf,
        level <= advisory) cool to blue instead of shouting amber. """
        e = (event or '').lower()
        if any(k in e for k in ('air quality', 'smoke', 'red flag', 'fire weather')):
            return 'brass'                              # override A
        if level <= 2 and any(k in e for k in ('wind', 'flood', 'winter', 'fog', 'coastal', 'gale', 'surf')):
            return 'water'                              # override B
        if level >= 4:
            return 'accent'                             # warning
        if level >= 2:
            return 'brass'                              # watch / advisory / alert
        return 'verdigris'                              # statement / outlook / unknown

    @staticmethod
    def _event_class(event):
        """ Hazard family for the label/nuance; colour comes from the level. """
        e = (event or '').lower()
        for sub, cls in _EVENT_CLASS_MAP:
            if sub in e:
                return cls
        return 'default'

    @staticmethod
    def _to_epoch(iso):
        """ ISO-8601 (with or without offset) -> epoch seconds; None if unparsable. """
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _until_text(epoch, tz):
        """ Glanceable station-local end time, e.g. "Wed 5 PM". None if unknown. """
        if epoch is None or tz is None:
            return None
        try:
            dt = datetime.fromtimestamp(epoch, tz)
        except (ValueError, OSError, OverflowError):
            return None
        return f"{dt.strftime('%a')} {AlmanacEmitter._hour_label(dt)}"

    @staticmethod
    def _split_counties(area_desc):
        """ "King, WA; Kitsap, WA; …" -> ['King', 'Kitsap', …] (deduped, ordered). """
        out, seen = [], set()
        for seg in (area_desc or '').split(';'):
            name = seg.split(',')[0].strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @staticmethod
    def _areas_short(counties):
        """ ['King','Kitsap','Pierce','Snohomish','Thurston'] -> 'King, Kitsap, Pierce +2'. """
        if not counties:
            return None
        head = counties[:3]
        extra = len(counties) - len(head)
        return ', '.join(head) + (f' +{extra}' if extra > 0 else '')

    @staticmethod
    def _extract_reason(desc):
        """ Best-effort "…for wildfire smoke has been issued…" -> "Wildfire smoke".
        Returns None when not confidently extractable. """
        if not desc:
            return None
        match = re.search(r'\bfor ([a-z][a-z \-]{2,40}?) (?:has been|have been|is|are) '
                          r'(?:issued|in effect)', desc, re.I)
        if not match:
            return None
        reason = match.group(1).strip()
        return reason[:1].upper() + reason[1:]

    def _process_alerts(self, feats, now, tz):
        """ Raw NWS `properties` dicts -> the wx.json alert list: drop expired,
        classify by product level, COLLAPSE identical events (union of counties,
        soonest end), sort by level then soonest end, cap the count. Pure given its
        inputs (no network); never raises. """
        groups = {}
        for prop in feats:
            end = prop.get('ends') or prop.get('expires')
            expires = self._to_epoch(end)
            if expires is not None and expires < now:              # expired
                continue
            event = prop.get('event') or 'Weather Alert'
            level, level_name = self._alert_level(event)
            key = event.strip().lower()                            # collapse only IDENTICAL products
            cand = {
                'event': event, 'eventClass': self._event_class(event),
                'level': level_name, 'tone': self._alert_tone(event, level), 'priority': level,
                'short': self._extract_reason(prop.get('description')),
                'onset': self._to_epoch(prop.get('onset')),
                'until': expires, 'untilText': self._until_text(expires, tz),
                'headline': (prop.get('headline') or '')[:160],
                '_areaset': self._split_counties(prop.get('areaDesc') or ''),
            }
            group = groups.get(key)
            if group is None:
                groups[key] = cand
            else:
                for county in cand['_areaset']:
                    if county not in group['_areaset']:
                        group['_areaset'].append(county)
                group['short'] = group['short'] or cand['short']
                if cand['until'] is not None and (group['until'] is None or cand['until'] < group['until']):
                    group['until'] = cand['until']
                    group['untilText'] = cand['untilText']
        out = []
        for group in groups.values():
            group['areaShort'] = self._areas_short(group.pop('_areaset'))
            out.append(group)
        out.sort(key=lambda a: (-a['priority'], a['until'] if a['until'] is not None else 9e18))
        return out[:ALERT_MAX]

    # --------------------------------------------------------------------
    def _emit(self, dt):
        """ Clock callback. Never allowed to raise - a broken/late DictProperty
        must not crash the almanac timer loop. """
        with self._life_lock:
            if self._radar_emit_pending is not None:
                self._radar_emit_pending.cancel()
                if self._radar_emit_pending in self._events:
                    self._events.remove(self._radar_emit_pending)
                self._radar_emit_pending = None
        try:
            payload = self._build_payload()
            self._write_atomic(payload)
            self._warned = False                     # recovered: re-arm the failure log
        except Exception as error:                                       # noqa: BLE001
            if not self._warned:
                Logger.warning(f'almanac_emit: emit failed - {error}')
                self._warned = True

    def _write_atomic(self, payload):
        """ Write temp file + os.replace so the HTML reader never observes a
        partially written file. """
        directory = os.path.dirname(self.output_path) or '.'
        os.makedirs(directory, exist_ok=True)
        tmp_path = f'{self.output_path}.tmp.{os.getpid()}'
        with open(tmp_path, 'w') as tmp_file:
            json.dump(payload, tmp_file, allow_nan=False)
        os.replace(tmp_path, self.output_path)

    # --------------------------------------------------------------------
    _SLP_FROM_MB = {'mb': (1.0, 1), 'hpa': (1.0, 1), 'inhg': (0.0295301, 3), 'mmhg': (0.750063, 2)}

    def _baro_series(self):
        """ 24 h sea-level-pressure trace for the barograph, downsampled to
        ~48 points [[epoch_s, slp], ...] oldest->newest, in the station's
        configured pressure unit (the same unit as 'slp', so the trace's hi/lo
        numerals and the big reading can never be in two unit systems).

        Sourced from the core's cached WeatherFlow REST 24 h obs
        (app.obsParser.api_data[device]['24Hrs']) — the same payload the core
        already uses for SLPTrend/Max/Min, so no extra network calls. Fully
        self-contained (no upstream files touched) and guarded so a missing or
        malformed payload just yields [] (HTML then hides the barograph).
        Cached for BARO_SERIES_TTL s: the 24 h data changes slowly and the JSON
        is large, so we must not re-parse it every 2 s emit. """
        BARO_SERIES_TTL = 300.0
        now = time.time()
        if self._baro_series_cache and (now - self._baro_series_t) < BARO_SERIES_TTL:
            return self._baro_series_cache
        series = []
        try:
            from lib import derived_variables as derive
            config   = self.app.config
            parser   = getattr(self.app, 'obsParser', None)
            api_data = getattr(parser, 'api_data', None) or {}
            st       = config['Station']
            device, idx = None, None
            for dev, blob in api_data.items():
                if not isinstance(blob, dict) or not blob.get('24Hrs'):
                    continue                     # None = the REST call failed; ordinary, not an error
                if str(dev) in (st['OutAirID'], st['OutAirSN']):
                    device, idx = dev, 1                    # AIR pressure bucket
                    break
                if str(dev) in (st['TempestID'], st['TempestSN']):
                    device, idx = dev, 6                    # TEMPEST pressure bucket
                    break
            if device is not None:
                obs = (api_data[device]['24Hrs'].json() or {}).get('obs') or []
                raw = [(ob[0], ob[idx]) for ob in obs
                       if ob and ob[0] is not None and len(ob) > idx and ob[idx] is not None]
                raw.sort(key=lambda p: p[0])
                # derive.SLP always answers in mb; the core's observation_format
                # converts to the configured unit with these factors/precisions
                unit = (_cfg(config, 'Units', 'Pressure') or 'mb').lower()
                factor, places = self._SLP_FROM_MB.get(unit, (1.0, 1))
                for t, p in raw:
                    slp = derive.SLP([p, 'mb'], device, config)[0]
                    slp = _num(slp)
                    timestamp = _num(t)
                    if slp is not None and timestamp is not None:
                        value = _num(slp * factor)
                        if value is not None:
                            series.append([int(timestamp), round(value, places)])
                target = 48
                if len(series) > target:
                    step   = (len(series) - 1) / (target - 1)
                    series = [series[int(round(i * step))] for i in range(target)]
        except Exception as error:                                            # noqa: BLE001
            if not getattr(self, '_baro_warned', False):
                Logger.warning(f'almanac_emit: barograph series unavailable - {error}')
                self._baro_warned = True         # one line per outage, not one per refresh
            series = []
        if series:
            self._baro_warned = False
        self._baro_series_cache = series
        self._baro_series_t     = now
        return series

    # --------------------------------------------------------------------
    def _build_payload(self):
        Obs    = getattr(self.screen, 'Obs', {})    or {}
        Astro  = getattr(self.screen, 'Astro', {})  or {}
        Met    = getattr(self.screen, 'Met', {})    or {}
        Sager  = getattr(self.screen, 'Sager', {})  or {}
        config = getattr(self.app, 'config', {})    or {}

        tz = self._station_tz(config)
        now_local = datetime.now(pytz.utc).astimezone(tz) if tz else datetime.now()

        sunrise_txt = _text(_idx(Astro.get('Sunrise'), 1))
        sunset_txt  = _text(_idx(Astro.get('Sunset'), 1))
        sun_frac, daylight_txt, till_sunset_txt = self._sun_fraction(sunrise_txt, sunset_txt, now_local, tz)

        rapid_dir = Obs.get('rapidDir')
        wind_dir  = Obs.get('WindDir')
        wind_dir_deg = _num(_idx(rapid_dir, 0))
        if wind_dir_deg is None:
            wind_dir_deg = _num(_idx(wind_dir, 0))
        wind_cardinal = _text(_idx(rapid_dir, 2)) or _text(_idx(wind_dir, 2)) \
            or _cardinal_from_degrees(wind_dir_deg)

        met_wind_dir = Met.get('WindDir')
        fc_wind_spd  = _num(_idx(Met.get('WindSpd'), 0))
        fc_wind_unit = _text(_idx(Met.get('WindSpd'), 1))
        fc_wind_card = _text(_idx(met_wind_dir, 2)) or _cardinal_from_degrees(_num(_idx(met_wind_dir, 0)))
        fc_wind = None
        if fc_wind_spd is not None:
            parts = [f'{fc_wind_spd:g}']
            if fc_wind_unit:
                parts.append(fc_wind_unit)
            if fc_wind_card:
                parts.append(fc_wind_card)
            fc_wind = ' '.join(parts)

        now = time.time()
        # FRESHNESS. 'ts' below is only the engine heartbeat: it proves the emit
        # tick ran, nothing about the data it carried. An observation's own
        # epoch is the only evidence the station is still reporting, and a
        # strike's epoch the only evidence the lightning readout is current -
        # the formatted display values carry neither, so they are read here from
        # the raw epochs the parser publishes alongside them.
        obs_ts  = _num(Obs.get('obsTs'))
        # A station that has never reported since the engine started is as
        # silent as one that stopped: age it from engine start, so a restart
        # cannot reset a dead sensor to "healthy" (obsTs stays null).
        obs_age = _age_sec(obs_ts if obs_ts is not None else self._started_at, now)

        strike_delta_t = Obs.get('StrikeDeltaT')
        strike_ts = _num(Obs.get('strikeTs'))
        if strike_ts is not None:
            lightning_since_sec = _age_sec(strike_ts, now)
            lightning_last      = _ago_text(lightning_since_sec)
        else:
            # No epoch published (a parser reset, or a build without it): the
            # core's formatted delta is frozen at the moment it was calculated,
            # but with no epoch it is the only source there is.
            lightning_since_sec = _num(_idx(strike_delta_t, 4))
            lightning_last      = _since_ago_text(strike_delta_t)
        lightning_active = self._lightning_active(config, lightning_since_sec)

        temp_val  = _num(_idx(Obs.get('outTemp'), 0))
        temp_unit = _temp_unit(_idx(Obs.get('outTemp'), 1))
        rain_raw_mm = _num(_idx(Obs.get('RainRate'), 3))
        rain_eff_mm = self._rain_win.effective(now, rain_raw_mm)
        fc_low    = _num(_idx(Met.get('lowTemp'), 0))
        fc_high   = _num(_idx(Met.get('highTemp'), 0))
        # One read of each provider snapshot per tick: every field below comes
        # from the same fetch, so the payload can never mix old and new.
        aqi_snap    = self._aqi_result
        fc_snap     = self._fc_result
        alerts_snap = self._alerts_result
        with self._radar_lock:
            radar_snap = self._radar_result
            if radar_snap.nexrad:
                nearest = dict(radar_snap.nexrad)
                nearest.update(self._radar_site_status.get(nearest['id'], {}))
                nearest['nextCheckTs'] = self._radar_discovery.due
                radar_snap = radar_snap._replace(nexrad=nearest)
            radar_refresh = self._radar_refresh
        ver_snap    = self._ver_result
        fc_rows   = self._unify_today(
                        self._fc_daily_current(now_local.strftime('%Y-%m-%d'), fc_snap.daily),
                        fc_low, fc_high)

        # Alerts expire between fetches, so the last-good raw features are
        # re-filtered here rather than trusted as processed at fetch time.
        alerts = (alerts_snap.alerts if alerts_snap.features is None
                  else self._process_alerts(alerts_snap.features, now, tz))

        payload = {
            'radar': dict(self._radar_payload(radar_snap, now, tz, radar_refresh),
                          health=self._radar_health_payload()),
            'ts':      int(now),                     # engine heartbeat ONLY - see obsAgeSec
            'obsTs':     int(obs_ts) if obs_ts is not None else None,
            'obsAgeSec': obs_age,                    # age of the newest OUTDOOR observation
            'station': _text(_cfg(config, 'Station', 'Name')),
            'locationLine': self._location_line(config),
            'updateAvailable': ver_snap.available,
            'latestVersion':   ver_snap.latest,
            'currentVersion':  ver_snap.current,
            'date':    now_local.strftime('%a, %d %b %Y'),
            'time':    now_local.strftime('%H:%M'),
            # station-local midnight as an epoch: the hero curve maps hourly epochs
            # onto the day's axis with this, exact to the second (HH:MM cannot be)
            'dayStartTs': int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),

            # Temperature
            'temp':            temp_val,
            'tempUnit':        temp_unit,
            'feelsLike':       _num(_idx(Obs.get('FeelsLike'), 0)),
            'feelsDesc':       self._feels_desc(_text(_idx(Obs.get('FeelsLike'), 2))),
            'tempTrendPerHr':  _num(_idx(Obs.get('outTempTrend'), 0)),
            'temp24hDelta':    _num(_idx(Obs.get('outTempDiff'), 0)),
            'obsLow':          _num(_idx(Obs.get('outTempMin'), 0)),
            'obsLowTime':      _text(_idx(Obs.get('outTempMin'), 2)),
            'obsHigh':         _num(_idx(Obs.get('outTempMax'), 0)),
            'obsHighTime':     _text(_idx(Obs.get('outTempMax'), 2)),
            'fcLow':           fc_low,
            'fcHigh':          fc_high,
            'humidity':        _num(_idx(Obs.get('Humidity'), 0)),
            'dewPoint':        _num(_idx(Obs.get('DewPoint'), 0)),

            # Conditions / short-term forecast
            'conditions':      _text(Met.get('Conditions')),
            'conditionsNote':  self._tomorrow_hint(fc_rows),   # "Rain tomorrow" etc; None on quiet days
            'fcHour':          _text(Met.get('Valid')),
            'fcWind':          fc_wind,
            'fcPrecipPct':     _num(_idx(Met.get('PrecipPercnt'), 0)),
            'fcDailyPct':      _num(_idx(Met.get('PrecipDay'), 0)),
            'fcDaily':         fc_rows,   # outlook, past days dropped at emit time
            # Hourly trajectory for the hero curve. Same snapshot as fcDaily, so
            # the curve and the band can never come from different fetches. The
            # console drops points that are no longer in the future, which is
            # also what makes a stale list (wifi out) draw nothing at all.
            'fcHourly':        list(fc_snap.hourly or ()),
            'fcStale':         (fc_snap.ts is None) or (now - fc_snap.ts) > FC_STALE_SEC,
            'fcAgeSec':        _age_sec(fc_snap.ts, now),   # since the last SUCCESSFUL fetch

            # Wind
            'windSpd':      _num(_idx(Obs.get('WindSpd'), 0)),
            'windUnit':     _text(_idx(Obs.get('WindSpd'), 1)),
            'windAvg':      _num(_idx(Obs.get('AvgWind'), 0)),
            'windGust':     _num(_idx(Obs.get('WindGust'), 0)),
            'windMax':      _num(_idx(Obs.get('MaxGust'), 0)),
            'windDir':      wind_dir_deg,
            'windCardinal': wind_cardinal,
            'windStatus':   _wind_desc(_idx(Obs.get('WindSpd'), 4)),   # Beaufort description, not the force number at [2]

            # Barometer
            'slp':            _num(_idx(Obs.get('SLP'), 0)),
            'slpUnit':        _text(_idx(Obs.get('SLP'), 1)),
            'slpTrendPerHr':  _num(_idx(Obs.get('SLPTrend'), 0)),
            'slpTrendDesc':   _text(_idx(Obs.get('SLPTrend'), 2)),
            'slp24High':      _num(_idx(Obs.get('SLPMax'), 0)),
            'slp24HighTime':  _text(_idx(Obs.get('SLPMax'), 2)),
            'slp24Low':       _num(_idx(Obs.get('SLPMin'), 0)),
            'slp24LowTime':   _text(_idx(Obs.get('SLPMin'), 2)),
            'slpOutlook':      self._OUTLOOK_COMPACT.get(
                                   _text(_idx(Obs.get('SLPTrend'), 3)) or '',
                                   _text(_idx(Obs.get('SLPTrend'), 3))),
            'slpSeries':      self._baro_series(),   # 24h [[t,slp],...] for the barograph

            # Rainfall
            'rainToday':    _num(_idx(Obs.get('TodayRain'), 0)),
            'rainYest':     _num(_idx(Obs.get('YesterdayRain'), 0)),
            'rainMonth':    _num(_idx(Obs.get('MonthRain'), 0)),
            'rainYear':     _num(_idx(Obs.get('YearRain'), 0)),
            'rainUnit':     _text(_cfg(config, 'Units', 'Precip')),
            'rainRate':     self._rain_rate_display(Obs, config, rain_eff_mm),
            'rainRateMm':   rain_eff_mm,    # mm/hr, max(raw minute, 10-min mean) - drives the gauge
            'rainRateInstMm': rain_raw_mm,  # the sensor's raw minute, for the record
            'rainStatus':   self._snowify_status(
                                self._rain_status_for(rain_eff_mm, _text(_idx(Obs.get('RainRate'), 2))),
                                temp_val, temp_unit, fc_rows),
            'drySpellDays': None,   # not reliably sourced - see report
            'lastRainDate': None,   # not sourced - no last-rain date/amount is tracked
            'lastRainAmt':  None,   # not sourced

            # Sun & UV
            'uvIndex':   _num(_idx(Obs.get('UVIndex'), 0)),
            'uvDesc':    _text(_idx(Obs.get('UVIndex'), 2)),
            'radiation': _num(_idx(Obs.get('Radiation'), 0)),
            'radUnit':   _text(_idx(Obs.get('Radiation'), 1)),
            'sunrise':   sunrise_txt,
            'sunset':    sunset_txt,
            'sunFrac':   sun_frac,
            'daylight':  daylight_txt,
            'tillSunset': till_sunset_txt,
            'peakSun':   _num(_idx(Obs.get('peakSun'), 0)),

            # Air quality (US AQI from Open-Meteo, by station lat/lon; off-thread)
            'aqi':         aqi_snap.aqi,
            'aqiCategory': aqi_snap.category,
            'aqiPm25':     _num(aqi_snap.pm25),
            'aqiForecast':    aqi_snap.forecast,
            'aqiPeak':        aqi_snap.peak,
            'aqiPeakTime':    aqi_snap.peak_time,
            'aqiForecastCat': aqi_snap.fc_cat,
            'aqiTrend':       aqi_snap.trend,
            'aqiTrendText':   aqi_snap.trend_text,
            'aqiStale':       (aqi_snap.ts is None) or (now - aqi_snap.ts) > AQI_STALE_SEC,
            'aqiAgeSec':      _age_sec(aqi_snap.ts, now),

            # Weather alerts (NWS, by station lat/lon)
            'alerts':      alerts,
            'alertCount':  len(alerts),
            'alertsStale': (alerts_snap.ts is None) or (now - alerts_snap.ts) > ALERT_STALE_SEC,
            'alertsAgeSec': _age_sec(alerts_snap.ts, now),
            'alertsAsOf':  (datetime.fromtimestamp(alerts_snap.ts, tz).strftime('%H:%M')
                            if (alerts_snap.ts and tz) else None),

            # Moon
            'moonPhase':  _text(_idx(Astro.get('Phase'), 1)),
            'moonIllum':  _num(_idx(Astro.get('Phase'), 2)),
            'moonrise':   _text(_idx(Astro.get('Moonrise'), 1)),
            'moonset':    _text(_idx(Astro.get('Moonset'), 1)),
            'nextFull':   _text(_idx(Astro.get('FullMoon'), 0)),
            'nextNew':    _text(_idx(Astro.get('NewMoon'), 0)),

            # Lightning
            'lightningActive':   lightning_active,
            'lightningDist':     _text(_idx(Obs.get('StrikeDist'), 0)),   # the core's +/-3 km RANGE text, e.g. "13-17"
            'lightningDistNum':  _range_mid(_idx(Obs.get('StrikeDist'), 0)),  # its midpoint, for ring geometry / big-number readouts
            'lightningDistUnit': _text(_idx(Obs.get('StrikeDist'), 1)),
            'lightningSinceSec': lightning_since_sec,   # from the strike EPOCH, not the core's frozen delta
            'lightningTs':       int(strike_ts) if strike_ts is not None else None,
            # The core tracks strike FREQUENCY (/min) and a rolling 3-HOUR count -
            # there is no 3-min/30-min bucket anywhere in the data path, so the
            # panel reports what the station actually measures.
            'lightningRate':     _num(_idx(Obs.get('StrikeFreq'), 0)),
            'lightning3hr':      _num(_idx(Obs.get('Strikes3hr'), 0)),
            'lightningToday':    _num(_idx(Obs.get('StrikesToday'), 0)),
            'lightningLast':     lightning_last,

            # Sager (the Weathercaster forecast text + when it was issued; the
            # module exposes only these two, so the card's other slots stay null)
            'sagerCode':     None,   # not sourced - no single composite dial code is exposed
            'sagerText':     _text(Sager.get('Forecast')),
            'sagerIssued':   _text(Sager.get('Issued')),
            'sagerPressure': None,   # not sourced - no composed "<value> <trend>" string exists
            'sagerWind':     None,   # not sourced
            'sagerSky':      None,   # not sourced
        }
        return _json_safe(payload)

    # --------------------------------------------------------------------
    @staticmethod
    def _station_tz(config):
        try:
            tzname = _cfg(config, 'Station', 'Timezone')
            return pytz.timezone(tzname) if tzname else None
        except Exception:                                                 # noqa: BLE001
            return None

    @classmethod
    def _station_today(cls, config):
        tz = cls._station_tz(config)
        now = datetime.now(pytz.utc).astimezone(tz) if tz else datetime.now()
        return now.strftime('%Y-%m-%d')

    @staticmethod
    def _lightning_active(config, since_sec):
        """ True only while lightning is RECENT — mirrors the core console, which
        swaps Panel Six from Rainfall to Lightning on a strike and reverts after
        Display/lightning_timeout minutes (the core also flags the bolt icon for
        strikes < 360 s). This emitter is poll-based, so we show Lightning while
        the last strike is inside that window and Rainfall otherwise. Window =
        lightning_timeout if configured, else 30 min. Never raises. """
        if since_sec is None:
            return False
        try:
            timeout_min = int(_cfg(config, 'Display', 'lightning_timeout') or 0)
        except Exception:                                                 # noqa: BLE001
            timeout_min = 0
        window_sec = timeout_min * 60 if timeout_min > 0 else 1800
        return since_sec < window_sec

    @staticmethod
    def _location_line(config):
        """ Footer location for THIS station, e.g. "Seattle / 47.61d N / 122.33d W",
        built from the local station config. Returns '' when unknown so the HTML
        keeps its generic committed placeholder (no home location ever in git).
        Never raises (the emitter must not crash the app). """
        try:
            name = _cfg(config, 'Station', 'Name')
            lat = _cfg(config, 'Station', 'Latitude')
            lon = _cfg(config, 'Station', 'Longitude')
            if lat in (None, '') or lon in (None, ''):
                return name or ''
            latf, lonf = float(lat), float(lon)
            coords = u'%.2f° %s · %.2f° %s' % (
                abs(latf), 'N' if latf >= 0 else 'S',
                abs(lonf), 'E' if lonf >= 0 else 'W')
            return (u'%s · %s' % (name, coords)) if name else coords
        except Exception:                                                 # noqa: BLE001
            return ''

    @staticmethod
    def _parse_hhmm(text, today, tz):
        """ Parse a formatted sunrise/sunset label ("05:43", "8:12 PM", with an
        optional " (+1)"/" (-1)" day-offset suffix that this simplified
        calculation ignores) into a timezone-aware datetime on `today`.
        Returns None if the label is a placeholder or unparsable. """
        if not text:
            return None
        clean = re.sub(r'\s*\([+-]1\)\s*$', '', text).strip()
        for fmt in ('%H:%M', '%I:%M %p', '%#I:%M %p', '%-I:%M %p'):
            try:
                parsed = datetime.strptime(clean, fmt)
                return tz.localize(datetime.combine(today, parsed.time()))
            except ValueError:
                continue
        return None

    @classmethod
    def _sun_fraction(cls, sunrise_txt, sunset_txt, now_local, tz):
        """ Best-effort elapsed-fraction-of-daylight and daylight-length,
        computed from the already-formatted HH:MM sunrise/sunset labels (the
        Astro DictProperty does not expose raw epoch sun-transit times to the
        display layer). Returns (None, None) if either label is unavailable
        or unparsable. Returns (frac, daylight_txt, till_sunset_txt). """
        if tz is None:
            return None, None, None
        sunrise_dt = cls._parse_hhmm(sunrise_txt, now_local.date(), tz)
        sunset_dt  = cls._parse_hhmm(sunset_txt, now_local.date(), tz)
        if sunrise_dt is None or sunset_dt is None or sunset_dt <= sunrise_dt:
            return None, None, None
        span = (sunset_dt - sunrise_dt).total_seconds()
        elapsed = (now_local - sunrise_dt).total_seconds()
        frac = max(0.0, min(1.0, elapsed / span)) if span > 0 else None
        hours, remainder = divmod(int(span), 3600)
        minutes = remainder // 60
        daylight_txt = f'{hours}h {minutes}m'
        # time remaining until today's sunset (0h 0m once the sun has set)
        till_sec = max(0, int((sunset_dt - now_local).total_seconds()))
        till_h, till_rem = divmod(till_sec, 3600)
        till_sunset_txt = f'{till_h}h {till_rem // 60}m'
        return frac, daylight_txt, till_sunset_txt
