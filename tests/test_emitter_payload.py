""" End-to-end tests for AlmanacEmitter._build_payload — the assembly half of
the pipeline (DictProperties -> wx.json dict). This is where the display keys
the HTML overlay consumes are actually produced, so it is the natural place to
assert the "any property -> wx.json" contract offline, no thunderstorm required.
"""

import json
from datetime import datetime

import pytz

from lib import almanac_emit as ae
from lib import properties
from tests.fixtures import obs_scenarios as scn
from tests.fixtures.config import make_config


def test_payload_is_json_serializable(make_emitter):
    payload = make_emitter(scn.strike_active())._build_payload()
    # allow_nan=False mirrors the real writer; a stray NaN would raise here
    json.dumps(payload, allow_nan=False)
    assert isinstance(payload, dict)
    assert payload['station'] == 'Test Station'
    assert payload['temp'] == 64.0


def test_rain_rate_uses_index0_not_index3(make_emitter):
    # The regression: rainRate must be the unit-converted in/hr at [0],
    # NOT the raw mm/hr at [3] (which was ~25x too high on the live console).
    payload = make_emitter(scn.heavy_rain())._build_payload()
    assert payload['rainRate'] == 0.05
    assert payload['rainRate'] != 1.27


def test_rain_rate_mm_is_the_raw_index3(make_emitter):
    # The intensity-banded gauge is defined in mm/hr (the units the console core
    # thresholds rain in), so the payload carries the raw [3] alongside the
    # display value. The two must NOT be confused - that was the 25x bug.
    payload = make_emitter(scn.heavy_rain())._build_payload()
    assert payload['rainRateMm'] == 1.27
    assert payload['rainRate'] == 0.05


def test_rain_status_and_unit(make_emitter):
    payload = make_emitter(scn.heavy_rain())._build_payload()
    assert payload['rainStatus'] == 'Heavy'
    assert payload['rainUnit'] == 'in'        # from config Units/Precip
    assert payload['rainToday'] == 1.2


def test_lightning_fields_from_obs(make_emitter):
    payload = make_emitter(scn.strike_active())._build_payload()
    assert payload['lightningActive'] is True
    assert payload['lightningDist'] == '2-8'
    assert payload['lightningSinceSec'] == 720.0
    assert payload['lightningToday'] == 3.0
    assert payload['lightningLast'] == '12 minutes ago'


def test_lightning_inactive_when_stale(make_emitter):
    s = scn.strike_active()
    s['Obs']['StrikeDeltaT'] = ['2', 'hours', '', '', 7200.0]   # outside 1800s window
    payload = make_emitter(s)._build_payload()
    assert payload['lightningActive'] is False


def test_all_none_never_raises(make_emitter):
    payload = make_emitter(scn.all_none())._build_payload()
    assert isinstance(payload, dict)
    assert payload['temp'] is None
    assert payload['windSpd'] is None
    assert payload['rainRate'] is None
    assert payload['rainRateMm'] is None
    assert payload['lightningActive'] is False
    assert payload['slpSeries'] == []            # no api_data -> empty series
    json.dumps(payload, allow_nan=False)


def test_temp_and_wind_status(make_emitter):
    payload = make_emitter(scn.calm_night())._build_payload()
    assert payload['temp'] == 49.0
    assert payload['tempUnit'] == '°F'           # glyph normalised
    assert payload['windSpd'] == 0.0
    assert payload['windStatus'] == 'Calm'       # 'Calm Conditions' trimmed


def test_wind_cardinal_fallback(make_emitter):
    # rapidDir absent (placeholder), so cardinal falls back to WindDir[2].
    payload = make_emitter(scn.strike_active())._build_payload()
    assert payload['windCardinal'] == 'SSW'
    assert payload['windDir'] == 210.0


def test_uv_and_radiation(make_emitter):
    payload = make_emitter(scn.clear_day())._build_payload()
    assert payload['uvIndex'] == 6.0
    assert payload['uvDesc'] == 'High'
    assert payload['radiation'] == 450.0


def test_location_line(make_emitter):
    payload = make_emitter(scn.all_none())._build_payload()
    assert payload['locationLine'] == 'Test Station · 47.61° N · 122.33° W'


def test_location_line_falls_back_to_name_without_coords(make_emitter):
    cfg = make_config(Station={'Latitude': '', 'Longitude': ''})
    payload = make_emitter(scn.all_none(), config=cfg)._build_payload()
    assert payload['locationLine'] == 'Test Station'


def test_aqi_passthrough(make_emitter):
    # AQI is fetched off-thread and stashed on the instance; the payload just
    # reads those attrs. Seed them directly (no network).
    emitter = make_emitter(scn.clear_day(), _aqi=42, _aqi_category='Good', _aqi_pm25='5.6')
    payload = emitter._build_payload()
    assert payload['aqi'] == 42
    assert payload['aqiCategory'] == 'Good'
    assert payload['aqiPm25'] == 5.6


def test_moon_and_sager_passthrough(make_emitter):
    s = scn.all_none()
    s['Astro']['Phase'] = ['', 'Waxing Gibbous', '63', 0]
    s['Sager'] = {'Forecast': 'Fair'}
    payload = make_emitter(s)._build_payload()
    assert payload['moonPhase'] == 'Waxing Gibbous'
    assert payload['moonIllum'] == 63.0
    assert payload['sagerText'] == 'Fair'


def test_write_atomic_roundtrips(make_emitter, tmp_path):
    emitter = make_emitter(scn.strike_active())
    payload = emitter._build_payload()
    emitter._write_atomic(payload)
    written = json.loads((tmp_path / 'wx.json').read_text())
    assert written == payload
    assert not list(tmp_path.glob('wx.json.tmp*'))       # no leftover temp files


# --- sun fraction: tested directly with an injected `now` for determinism ---
# (_build_payload uses datetime.now(), which would make day/night wall-clock flaky)

def test_sun_fraction_midday():
    tz = pytz.timezone('America/Los_Angeles')
    now = tz.localize(datetime(2026, 6, 21, 13, 0))       # 1pm, between 06:00 and 20:00
    frac, daylight, till = ae.AlmanacEmitter._sun_fraction('06:00', '20:00', now, tz)
    assert 0.0 < frac < 1.0
    assert daylight == '14h 0m'
    assert till == '7h 0m'


def test_sun_fraction_after_sunset_clamps():
    tz = pytz.timezone('America/Los_Angeles')
    now = tz.localize(datetime(2026, 6, 21, 21, 30))      # after 20:00 sunset
    frac, daylight, till = ae.AlmanacEmitter._sun_fraction('06:00', '20:00', now, tz)
    assert frac == 1.0
    assert till == '0h 0m'


def test_sun_fraction_no_tz_is_none():
    frac, daylight, till = ae.AlmanacEmitter._sun_fraction('06:00', '20:00', datetime.now(), None)
    assert (frac, daylight, till) == (None, None, None)


def test_lightning_distance_range_yields_a_number(make_emitter):
    # The core only ever renders strike distance as a +/-3 km RANGE ("2-8"), so
    # the payload has to carry a numeric midpoint too - without it every numeric
    # consumer in the overlay (ring radius, big-number readout) silently fell
    # back to a dash whenever lightning was actually detected.
    payload = make_emitter(scn.strike_active())._build_payload()
    assert payload['lightningDistNum'] == 5.0
    assert payload['lightningDistUnit'] == 'miles'


def test_range_mid_handles_the_shapes_the_core_emits():
    from lib.almanac_emit import _range_mid
    assert _range_mid('13-17') == 15.0
    assert _range_mid('0-3') == 1.5
    assert _range_mid('15') == 15.0
    assert _range_mid(u'13\u201317') == 15.0     # en dash
    assert _range_mid('-') is None               # placeholder
    assert _range_mid(None) is None


def test_lightning_counts_report_what_the_core_measures(make_emitter):
    # There is no 3-min/30-min strike bucket in the console data path, so those
    # two rows were permanently dashed. The panel now carries the frequency and
    # the rolling 3-hour count, which the core does track.
    payload = make_emitter(scn.strike_active())._build_payload()
    assert payload['lightningRate'] == 2.5
    assert payload['lightning3hr'] == 11
    assert payload['lightningToday'] == 3


def test_fc_daily_shapes_open_meteo_arrays():
    from lib.almanac_emit import AlmanacEmitter
    daily = {
        'time': ['2026-08-29', '2026-08-30', '2026-08-31'],
        'temperature_2m_max': [63.6, 71.4, None],
        'temperature_2m_min': [53.5, 52.8, 55.0],
        'weather_code': [95, 61, 2],
        'precipitation_probability_max': [95, 60, None],
    }
    rows = AlmanacEmitter._fc_daily_from(daily)
    # the None-hi day is dropped: a partial bar lies on the shared scale
    assert len(rows) == 2
    assert rows[0] == {'day': 'SAT', 'date': '2026-08-29', 'hi': 64, 'lo': 54, 'code': 95, 'pp': 95}
    assert rows[1]['day'] == 'SUN' and rows[1]['hi'] == 71 and rows[1]['pp'] == 60


def test_fc_daily_empty_and_garbage_never_raise():
    from lib.almanac_emit import AlmanacEmitter
    assert AlmanacEmitter._fc_daily_from({}) == []
    assert AlmanacEmitter._fc_daily_from({'time': ['not-a-date'],
                                          'temperature_2m_max': [70],
                                          'temperature_2m_min': [50]}) == []


def test_trace_rain_rate_string_still_yields_a_number(make_emitter):
    # The core formats a trace rate as the STRING '<0.01' (in/hr). That made
    # rainRate null, which read as a dash AND hid the gauge water while it
    # was actually drizzling. The emitter now falls back to converting the
    # raw mm/hr by the configured precip unit.
    s = scn.heavy_rain()
    s['Obs']['RainRate'] = ['<0.01', 'in/hr', 'Very Light Rain', 0.2]
    payload = make_emitter(s)._build_payload()
    assert payload['rainRate'] == round(0.2 / 25.4, 4)
    assert payload['rainRateMm'] == 0.2


def test_stale_forecast_drops_past_days():
    from lib.almanac_emit import AlmanacEmitter
    e = AlmanacEmitter.__new__(AlmanacEmitter)
    e._fc_daily = [
        {'day': 'FRI', 'date': '2026-08-28', 'hi': 70, 'lo': 50, 'code': 0, 'pp': 0},
        {'day': 'SAT', 'date': '2026-08-29', 'hi': 64, 'lo': 54, 'code': 95, 'pp': 93},
        {'day': 'SUN', 'date': '2026-08-30', 'hi': 68, 'lo': 52, 'code': 61, 'pp': 60},
    ]
    rows = e._fc_daily_current('2026-08-29')
    assert [r['day'] for r in rows] == ['SAT', 'SUN']   # yesterday can never be TODAY
    assert [r['today'] for r in rows] == [True, False]  # and only the real today is flagged
    # a row without a date (older cached shape) is kept, never crashed on
    e._fc_daily.append({'day': 'MON', 'hi': 70, 'lo': 50, 'code': 0, 'pp': 0})
    assert e._fc_daily_current('2026-08-29')[-1]['day'] == 'MON'
