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

from datetime import datetime, timezone
import json
import math
import os
import re
import time
import pytz

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
        return default if isinstance(value, float) and math.isnan(value) else float(value)
    if isinstance(value, str):
        text = _clean_str(value)
        if text is None:
            return default
        if text.lower() == 'trace':
            return 0.0
        # Strike counts can render as e.g. "1.2 k" for >= 1000
        if text.lower().endswith('k'):
            try:
                return float(text[:-1].strip()) * 1000
            except ValueError:
                return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


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


def _since_ago_text(strike_delta_t):
    """ Build a "3 days ago" / "5 hours ago" / "12 minutes ago" style string
    from the ['d','days','h','hours', epoch] shape that
    observation_format.format(..., 'TimeDelta') produces for StrikeDeltaT.
    Returns None if the underlying value is a placeholder (no strikes seen). """
    n1, u1 = _text(_idx(strike_delta_t, 0)), _text(_idx(strike_delta_t, 1))
    if n1 is None or u1 is None:
        return None
    return f'{n1} {u1} ago'


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
        self._ver_event  = None
        self._warned     = False
        # update-check state (populated off-thread; read on the emit tick)
        self._update_available = False
        self._latest_version   = None
        self._current_version  = None
        # barograph 24h SLP series cache (refreshed every BARO_SERIES_TTL s so we
        # don't re-parse the 1440-point REST payload on every 2 s emit tick)
        self._baro_series_cache = []
        self._baro_series_t     = 0.0
        # rolling rain-rate window: the Tempest's haptic sensor reports drizzle
        # as an occasional 0.01 in minute with zeros between, so the raw
        # per-minute rate flickers 0 <-> trace and the gauge went dry mid-drizzle
        self._rain_win = _RainWindow(RAIN_WINDOW_SEC)
        # air-quality state (fetched off-thread from Open-Meteo by station lat/lon)
        self._aqi          = None
        self._aqi_category = None
        self._aqi_pm25     = None
        self._aqi_event    = None
        self._aqi_ts       = None    # epoch of last SUCCESSFUL aqi fetch (staleness guard)
        self._aqi_forecast = []      # [[epoch, us_aqi], ...] next hours, for the trend
        self._aqi_peak      = None   # max us_aqi over the next 6 h
        self._aqi_peak_time = None   # station-local hour label of that peak ("5 PM")
        self._aqi_fc_cat    = None   # AQI category at the peak
        self._aqi_trend     = None   # 'rising' | 'falling' | 'steady'
        self._aqi_trend_text = None  # "Moderate by 5 PM" | "Improving" | None
        # 7-day outlook state (fetched off-thread from Open-Meteo by lat/lon)
        self._fc_daily   = []        # [{day,hi,lo,code,pp}, ...] display-ready
        self._fc_ts      = None      # epoch of last SUCCESSFUL forecast fetch
        self._fc_event   = None
        # weather-alerts state (fetched off-thread from api.weather.gov by lat/lon)
        self._alerts       = []      # last-good, processed + collapsed + sorted
        self._alerts_ts    = None    # epoch of last SUCCESSFUL alerts fetch (staleness guard)
        self._alerts_event = None    # Clock handle

    def start(self):
        """ Schedule the periodic emit. Idempotent - calling twice (e.g. if
        add_panels() is re-invoked by a PanelCount change) cancels the
        previous schedule first rather than stacking a second timer. """
        if self._event is not None:
            self._event.cancel()
        try:
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        except OSError as error:
            Logger.warning(f'almanac_emit: could not create output directory - {error}')
        self._event = Clock.schedule_interval(self._emit, self.interval)
        # update check: soon after start, then periodically (off the main thread)
        Clock.schedule_once(self._check_version, 8)
        self._ver_event = Clock.schedule_interval(self._check_version, VERSION_CHECK_INTERVAL)
        # air quality: after the USB wifi has settled post-boot, then periodically
        Clock.schedule_once(self._check_aqi, 30)
        self._aqi_event = Clock.schedule_interval(self._check_aqi, AQI_CHECK_INTERVAL)
        # weather alerts: staggered a little after AQI, then periodically
        Clock.schedule_once(self._check_alerts, 40)
        self._alerts_event = Clock.schedule_interval(self._check_alerts, ALERTS_CHECK_INTERVAL)
        # 7-day outlook: staggered after alerts, then hourly
        Clock.schedule_once(self._check_forecast, 50)
        self._fc_event = Clock.schedule_interval(self._check_forecast, FORECAST_CHECK_INTERVAL)
        return self._event

    def stop(self):
        if self._event is not None:
            self._event.cancel()
            self._event = None
        if self._ver_event is not None:
            self._ver_event.cancel()
            self._ver_event = None
        if self._aqi_event is not None:
            self._aqi_event.cancel()
            self._aqi_event = None
        if self._alerts_event is not None:
            self._alerts_event.cancel()
            self._alerts_event = None

    def _check_version(self, _dt=None):
        """ Kick off a non-blocking GitHub version check on a daemon thread so a
        slow/failed request never stalls the Kivy main loop or the emit tick. """
        try:
            import threading
            threading.Thread(target=self._do_version_check, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            pass

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
            self._latest_version  = latest
            self._current_version = current
            if current and latest:
                self._update_available = (
                    _v.parse(str(latest).lstrip('vV')) > _v.parse(str(current).lstrip('vV')))
        except Exception:                                                 # noqa: BLE001
            pass

    def _check_forecast(self, _dt=None):
        """ Kick off a non-blocking 7-day forecast fetch on a daemon thread. """
        try:
            import threading
            threading.Thread(target=self._do_forecast, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            pass

    def _do_forecast(self):
        """ Fetch the 7-day daily outlook from Open-Meteo for the station's
        lat/lon: hi/lo, WMO weather code, max precipitation probability. The
        temperature unit follows the console's own Units/Temp setting so the
        strip always matches the observed readings. Off-thread, never raises;
        on failure the previous outlook is kept. """
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
                   '&wind_speed_unit=kmh'
                   f'&precipitation_unit={precip_unit}'
                   f'&temperature_unit={unit}&forecast_days=7&timezone=auto')
            req = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            days = self._fc_daily_from(data.get('daily') or {})
            if days:
                self._fc_daily = days
                self._fc_ts    = time.time()
        except Exception as error:                                        # noqa: BLE001
            Logger.warning(f'almanac_emit: forecast fetch failed - {error}')
        finally:
            # Boot resilience: the hourly interval is far too slow to recover
            # from a failed FIRST fetch (the USB wifi is often still settling
            # when the t+50s attempt fires - the same failure AQI's delayed
            # start works around). Until one fetch has succeeded, retry every
            # 2 minutes; after that the hourly cadence is plenty.
            if self._fc_ts is None:
                try:
                    Clock.schedule_once(self._check_forecast, 120)
                except Exception:                                         # noqa: BLE001
                    pass

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
            hi = his[i]  if i < len(his)  else None
            lo = los[i]  if i < len(los)  else None
            if hi is None or lo is None:
                continue
            try:
                day = datetime.fromisoformat(t).strftime('%a').upper()
            except (ValueError, TypeError):
                continue
            code = codes[i] if i < len(codes) else None
            pp   = pps[i]   if i < len(pps)   else None
            qpf  = qpfs[i]  if i < len(qpfs)  else None
            gust = gusts[i] if i < len(gusts) else None
            out.append({'day':  day,
                        'date': t[:10],
                        'hi':   int(round(hi)),
                        'lo':   int(round(lo)),
                        'code': int(code) if code is not None else None,
                        'pp':   int(round(pp)) if pp is not None else None,
                        # two decimals covers both units; the board hides
                        # anything below what the unit can print
                        'qpf':  round(float(qpf), 2) if qpf is not None else None,
                        'gust': int(round(gust)) if gust is not None else None})   # km/h, fixed unit
        return out

    WINDY_GUST_KMH = 45   # ~28 mph gusts: the day is a wind story

    @classmethod
    def _tomorrow_hint(cls, fc_rows):
        """ One quiet line about tomorrow, only when tomorrow is a story:
        "Thunderstorms tomorrow" / "Snow tomorrow" / "Rain tomorrow" /
        "Windy tomorrow" / "Fog tomorrow". Ordinary days say nothing -
        absence is information. Requires row 0 to be flagged today so
        row 1 is provably tomorrow. Never raises. """
        try:
            if len(fc_rows) < 2 or not fc_rows[0].get('today'):
                return None
            t = fc_rows[1]
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

    def _fc_daily_current(self, today_iso):
        """ The stored outlook with any already-past days dropped, so a stale
        forecast (wifi out for a day+) can never mislabel yesterday as TODAY.
        As rows age out the band naturally shrinks below the HTML's 3-day
        minimum and hides itself - no separate staleness flag needed. """
        rows = [dict(r) for r in self._fc_daily
                if not r.get('date') or r['date'] >= today_iso]
        for r in rows:
            r['today'] = (r.get('date') == today_iso)
        return rows

    def _check_aqi(self, _dt=None):
        """ Kick off a non-blocking air-quality fetch on a daemon thread. """
        try:
            import threading
            threading.Thread(target=self._do_aqi, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            pass

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
                aqi = d.get('aqi')
                if not isinstance(aqi, (int, float)):
                    return                  # station reports '-' when sensor is offline
                aqi = int(round(aqi))
                iaqi = d.get('iaqi') or {}
                self._aqi          = aqi
                self._aqi_category = self._aqi_cat(aqi)
                self._aqi_pm25     = (iaqi.get('pm25') or {}).get('v')   # PM2.5 µg/m³
                self._aqi_ts       = time.time()
                fc_pm25 = ((d.get('forecast') or {}).get('daily') or {}).get('pm25') or []
                (self._aqi_forecast, self._aqi_peak, self._aqi_peak_time,
                 self._aqi_trend, self._aqi_trend_text, self._aqi_fc_cat) = \
                    self._waqi_trend(aqi, fc_pm25)
            else:
                # Open-Meteo fallback: CAMS model, no token required
                url = ('https://air-quality-api.open-meteo.com/v1/air-quality'
                       f'?latitude={lat}&longitude={lon}&current=us_aqi,pm2_5'
                       '&hourly=us_aqi,pm2_5&forecast_days=1&timezone=auto')
                req = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                cur = data.get('current') or {}
                aqi = cur.get('us_aqi')
                if aqi is None:
                    return
                aqi = int(round(aqi))
                self._aqi          = aqi
                self._aqi_category = self._aqi_cat(aqi)
                self._aqi_pm25     = cur.get('pm2_5')
                self._aqi_ts       = time.time()
                (self._aqi_forecast, self._aqi_peak, self._aqi_peak_time,
                 self._aqi_trend, self._aqi_trend_text, self._aqi_fc_cat) = \
                    self._aqi_forecast_summary(data.get('hourly') or {}, time.time(),
                                               self._station_tz(config), aqi)
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
    def _waqi_trend(aqi_now, fc_pm25_daily):
        """ Derive rising/falling/steady from WAQI's daily PM2.5 forecast.
        Returns the same 6-tuple as _aqi_forecast_summary so callers are
        unchanged.  No hourly series, so aqiForecast sparkline is empty. """
        if not fc_pm25_daily or aqi_now is None:
            return [], None, None, None, None, None
        today    = fc_pm25_daily[0] if fc_pm25_daily else {}
        tomorrow = fc_pm25_daily[1] if len(fc_pm25_daily) > 1 else {}
        peak_raw = today.get('max')
        if peak_raw is None:
            return [], None, None, None, None, None
        peak     = int(round(peak_raw))
        peak_cat = AlmanacEmitter._aqi_cat(peak)
        cur_cat  = AlmanacEmitter._aqi_cat(aqi_now)
        nxt_avg  = tomorrow.get('avg')
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
        try:
            import threading
            threading.Thread(target=self._do_alerts, daemon=True).start()
        except Exception:                                                 # noqa: BLE001
            pass

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
                    self._alerts    = []
                    self._alerts_ts = time.time()
                    return
                raise
            feats = [f.get('properties') or {} for f in (data.get('features') or [])]
            self._alerts    = self._process_alerts(feats, time.time(), self._station_tz(config))
            self._alerts_ts = time.time()
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
                    if slp is not None and slp == slp:   # not None, not NaN (NaN breaks allow_nan=False)
                        series.append([int(t), round(slp * factor, places)])
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

        strike_delta_t = Obs.get('StrikeDeltaT')
        lightning_since_sec = _num(_idx(strike_delta_t, 4))
        lightning_active = self._lightning_active(config, lightning_since_sec)

        temp_val  = _num(_idx(Obs.get('outTemp'), 0))
        temp_unit = _temp_unit(_idx(Obs.get('outTemp'), 1))
        rain_raw_mm = _num(_idx(Obs.get('RainRate'), 3))
        rain_eff_mm = self._rain_win.effective(time.time(), rain_raw_mm)
        fc_low    = _num(_idx(Met.get('lowTemp'), 0))
        fc_high   = _num(_idx(Met.get('highTemp'), 0))
        fc_rows   = self._unify_today(
                        self._fc_daily_current(now_local.strftime('%Y-%m-%d')),
                        fc_low, fc_high)

        return {
            'ts':      int(time.time()),
            'station': _text(_cfg(config, 'Station', 'Name')),
            'locationLine': self._location_line(config),
            'updateAvailable': self._update_available,
            'latestVersion':   self._latest_version,
            'currentVersion':  self._current_version,
            'date':    now_local.strftime('%a, %d %b %Y'),
            'time':    now_local.strftime('%H:%M'),

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
            'fcStale':         (self._fc_ts is None) or (time.time() - self._fc_ts) > FC_STALE_SEC,

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
            'aqi':         self._aqi,
            'aqiCategory': self._aqi_category,
            'aqiPm25':     _num(self._aqi_pm25),
            'aqiForecast':    self._aqi_forecast,
            'aqiPeak':        self._aqi_peak,
            'aqiPeakTime':    self._aqi_peak_time,
            'aqiForecastCat': self._aqi_fc_cat,
            'aqiTrend':       self._aqi_trend,
            'aqiTrendText':   self._aqi_trend_text,
            'aqiStale':       (self._aqi_ts is None) or (time.time() - self._aqi_ts) > AQI_STALE_SEC,

            # Weather alerts (NWS, by station lat/lon)
            'alerts':      self._alerts,
            'alertCount':  len(self._alerts),
            'alertsStale': (self._alerts_ts is None) or (time.time() - self._alerts_ts) > ALERT_STALE_SEC,
            'alertsAsOf':  (datetime.fromtimestamp(self._alerts_ts, tz).strftime('%H:%M')
                            if (self._alerts_ts and tz) else None),

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
            'lightningSinceSec': lightning_since_sec,
            # The core tracks strike FREQUENCY (/min) and a rolling 3-HOUR count -
            # there is no 3-min/30-min bucket anywhere in the data path, so the
            # panel reports what the station actually measures.
            'lightningRate':     _num(_idx(Obs.get('StrikeFreq'), 0)),
            'lightning3hr':      _num(_idx(Obs.get('Strikes3hr'), 0)),
            'lightningToday':    _num(_idx(Obs.get('StrikesToday'), 0)),
            'lightningLast':     _since_ago_text(strike_delta_t),

            # Sager
            'sagerCode':     None,   # not sourced - no single composite dial code is exposed
            'sagerText':     _text(Sager.get('Forecast')),
            'sagerPressure': None,   # not sourced - no composed "<value> <trend>" string exists
            'sagerWind':     None,   # not sourced
            'sagerSky':      None,   # not sourced
        }

    # --------------------------------------------------------------------
    @staticmethod
    def _station_tz(config):
        try:
            tzname = _cfg(config, 'Station', 'Timezone')
            return pytz.timezone(tzname) if tzname else None
        except Exception:                                                 # noqa: BLE001
            return None

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
