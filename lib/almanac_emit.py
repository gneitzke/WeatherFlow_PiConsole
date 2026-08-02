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

from datetime import datetime
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
        return self._event

    def stop(self):
        if self._event is not None:
            self._event.cancel()
            self._event = None
        if self._ver_event is not None:
            self._ver_event.cancel()
            self._ver_event = None

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

    # --------------------------------------------------------------------
    def _emit(self, dt):
        """ Clock callback. Never allowed to raise - a broken/late DictProperty
        must not crash the almanac timer loop. """
        try:
            payload = self._build_payload()
            self._write_atomic(payload)
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
    def _baro_series(self):
        """ 24 h sea-level-pressure trace for the barograph, downsampled to
        ~48 points [[epoch_s, slp_mb], ...] oldest->newest.

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
                if not isinstance(blob, dict) or '24Hrs' not in blob:
                    continue
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
                for t, p in raw:
                    slp = derive.SLP([p, 'mb'], device, config)[0]
                    if slp is not None:
                        series.append([int(t), round(slp, 1)])
                target = 48
                if len(series) > target:
                    step   = (len(series) - 1) / (target - 1)
                    series = [series[int(round(i * step))] for i in range(target)]
        except Exception as error:                                            # noqa: BLE001
            Logger.warning(f'almanac_emit: barograph series unavailable - {error}')
            series = []
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
            'temp':            _num(_idx(Obs.get('outTemp'), 0)),
            'tempUnit':        _temp_unit(_idx(Obs.get('outTemp'), 1)),
            'feelsLike':       _num(_idx(Obs.get('FeelsLike'), 0)),
            'feelsDesc':       _text(_idx(Obs.get('FeelsLike'), 2)),
            'tempTrendPerHr':  _num(_idx(Obs.get('outTempTrend'), 0)),
            'temp24hDelta':    _num(_idx(Obs.get('outTempDiff'), 0)),
            'obsLow':          _num(_idx(Obs.get('outTempMin'), 0)),
            'obsLowTime':      _text(_idx(Obs.get('outTempMin'), 2)),
            'obsHigh':         _num(_idx(Obs.get('outTempMax'), 0)),
            'obsHighTime':     _text(_idx(Obs.get('outTempMax'), 2)),
            'fcLow':           _num(_idx(Met.get('lowTemp'), 0)),
            'fcHigh':          _num(_idx(Met.get('highTemp'), 0)),
            'humidity':        _num(_idx(Obs.get('Humidity'), 0)),
            'dewPoint':        _num(_idx(Obs.get('DewPoint'), 0)),

            # Conditions / short-term forecast
            'conditions':      _text(Met.get('Conditions')),
            'conditionsNote':  None,   # not sourced - no composed "note" field exists
            'fcHour':          _text(Met.get('Valid')),
            'fcWind':          fc_wind,
            'fcPrecipPct':     _num(_idx(Met.get('PrecipPercnt'), 0)),
            'fcDailyPct':      _num(_idx(Met.get('PrecipDay'), 0)),

            # Wind
            'windSpd':      _num(_idx(Obs.get('WindSpd'), 0)),
            'windUnit':     _text(_idx(Obs.get('WindSpd'), 1)),
            'windAvg':      _num(_idx(Obs.get('AvgWind'), 0)),
            'windGust':     _num(_idx(Obs.get('WindGust'), 0)),
            'windMax':      _num(_idx(Obs.get('MaxGust'), 0)),
            'windDir':      wind_dir_deg,
            'windCardinal': wind_cardinal,
            'windStatus':   _text(_idx(Obs.get('WindSpd'), 2)),

            # Barometer
            'slp':            _num(_idx(Obs.get('SLP'), 0)),
            'slpUnit':        _text(_idx(Obs.get('SLP'), 1)),
            'slpTrendPerHr':  _num(_idx(Obs.get('SLPTrend'), 0)),
            'slpTrendDesc':   _text(_idx(Obs.get('SLPTrend'), 2)),
            'slp24High':      _num(_idx(Obs.get('SLPMax'), 0)),
            'slp24HighTime':  _text(_idx(Obs.get('SLPMax'), 2)),
            'slp24Low':       _num(_idx(Obs.get('SLPMin'), 0)),
            'slp24LowTime':   _text(_idx(Obs.get('SLPMin'), 2)),
            'slpOutlook':     _text(_idx(Obs.get('SLPTrend'), 3)),
            'slpSeries':      self._baro_series(),   # 24h [[t,slp],...] for the barograph

            # Rainfall
            'rainToday':    _num(_idx(Obs.get('TodayRain'), 0)),
            'rainYest':     _num(_idx(Obs.get('YesterdayRain'), 0)),
            'rainMonth':    _num(_idx(Obs.get('MonthRain'), 0)),
            'rainYear':     _num(_idx(Obs.get('YearRain'), 0)),
            'rainUnit':     _text(_cfg(config, 'Units', 'Precip')),
            'rainRate':     _num(_idx(Obs.get('RainRate'), 3), default=_num(_idx(Obs.get('RainRate'), 0))),
            'rainStatus':   _text(_idx(Obs.get('RainRate'), 2)),
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

            # Moon
            'moonPhase':  _text(_idx(Astro.get('Phase'), 1)),
            'moonIllum':  _num(_idx(Astro.get('Phase'), 2)),
            'moonrise':   _text(_idx(Astro.get('Moonrise'), 1)),
            'moonset':    _text(_idx(Astro.get('Moonset'), 1)),
            'nextFull':   _text(_idx(Astro.get('FullMoon'), 0)),
            'nextNew':    _text(_idx(Astro.get('NewMoon'), 0)),

            # Lightning
            'lightningActive':   lightning_active,
            'lightningDist':     _text(_idx(Obs.get('StrikeDist'), 0)),
            'lightningSinceSec': lightning_since_sec,
            'lightning3min':     None,   # not sourced - only 3hr/day/month/year counts exist
            'lightning30min':    None,   # not sourced
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
