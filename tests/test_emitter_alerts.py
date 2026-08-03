""" Tests for the NWS-alerts + AQI-forecast pipeline in lib/almanac_emit.

Severity is derived from the NWS product level in the event name (Warning >
Watch > Advisory > Alert/Statement > Outlook), which stays reliable even though
the CAP severity/urgency fields are 'Unknown'. All hermetic: no network — raw
alert `properties` dicts and an Open-Meteo hourly block are fed directly.
"""

import json
import time
from datetime import datetime

import pytest
import pytz

from lib.almanac_emit import AlmanacEmitter as A
from tests.fixtures import nws_alerts as nws
from tests.fixtures import obs_scenarios as scn

TZ = pytz.timezone('America/Los_Angeles')


# ---------------------------------------------------------------- level + tone
@pytest.mark.parametrize('event, level_int, level_str', [
    ('Tornado Warning', 4, 'warning'),
    ('Winter Storm Watch', 3, 'watch'),
    ('Wind Advisory', 2, 'advisory'),
    ('Air Quality Alert', 2, 'advisory'),
    ('Special Weather Statement', 1, 'statement'),
    ('Hazardous Weather Outlook', 0, 'outlook'),
    ('Foo Bar', 1, 'statement'),          # unknown product -> statement-tier
])
def test_alert_level_table(event, level_int, level_str):
    assert A._alert_level(event) == (level_int, level_str)


@pytest.mark.parametrize('event, tone', [
    ('Tornado Warning', 'accent'),
    ('Flash Flood Warning', 'accent'),
    ('Air Quality Alert', 'brass'),
    ('Red Flag Warning', 'brass'),        # override A: fire/haze = amber even as a Warning
    ('Flood Watch', 'brass'),             # watch -> brass (no advisory-gate override)
    ('Wind Advisory', 'water'),           # override B: routine water advisory = cool blue
    ('Special Weather Statement', 'verdigris'),
])
def test_alert_tone_from_level(event, tone):
    level, _ = A._alert_level(event)
    assert A._alert_tone(event, level) == tone


# ------------------------------------------------------------- process_alerts
def _process(feats):
    return A._process_alerts.__get__(A.__new__(A))(feats, time.time(), TZ)


def test_process_single_air_quality():
    out = _process([nws.air_quality_1()])
    assert len(out) == 1
    a = out[0]
    assert a['eventClass'] == 'air' and a['tone'] == 'brass' and a['level'] == 'advisory'
    assert a['until'] is not None            # from `expires` since `ends` is null
    assert a['short'] == 'Wildfire smoke'


def test_process_dedup_same_event():
    out = _process([nws.air_quality_1(), nws.air_quality_2()])
    assert len(out) == 1                                  # collapsed
    assert out[0]['areaShort'] == 'King, Kitsap, Pierce +3'   # union of 6 counties


def test_process_expired_filtered():
    out = _process([nws.air_quality_1(), nws.expired_alert()])
    assert len(out) == 1 and out[0]['event'] == 'Air Quality Alert'


def test_level_orders_over_hazard():
    # a Flood Watch (3) must outrank a Wind Advisory (2) despite both being "water"
    out = _process([nws.wind_advisory(), nws.flood_watch()])
    assert out[0]['event'] == 'Flood Watch'
    # a Tornado Warning (4) must outrank an Air Quality Alert (2)
    out = _process([nws.air_quality_1(), nws.tornado_warning()])
    assert out[0]['eventClass'] == 'severe' and out[0]['tone'] == 'accent'
    aq = [a for a in out if a['eventClass'] == 'air'][0]
    assert aq['tone'] == 'brass'                          # air quality stays amber


def test_redflag_warning_amber_but_outranks():
    out = _process([nws.wind_advisory(), nws.red_flag_warning()])
    assert out[0]['event'] == 'Red Flag Warning'          # level 4 sorts first
    assert out[0]['tone'] == 'brass'                      # override A recolors, not reprioritizes


def test_process_cap_at_three():
    feats = [nws.tornado_warning(), nws.flood_watch(), nws.wind_advisory(),
             nws.special_statement(), nws.red_flag_warning()]
    out = _process(feats)
    assert len(out) == 3                                  # ALERT_MAX


# --------------------------------------------------------------- small helpers
def test_collapse_areas():
    assert A._areas_short(['King', 'Kitsap', 'Pierce', 'Snohomish', 'Thurston']) == 'King, Kitsap, Pierce +2'
    assert A._areas_short(['King']) == 'King'
    assert A._areas_short([]) is None


def test_extract_reason():
    assert A._extract_reason('An Air Quality Alert for wildfire smoke has been issued by …') == 'Wildfire smoke'
    assert A._extract_reason('Just some prose with no pattern.') is None


def test_split_counties_dedup():
    assert A._split_counties('King, WA; Kitsap, WA; King, WA') == ['King', 'Kitsap']


# ------------------------------------------------------------------- staleness
def test_alerts_stale_flag(make_emitter):
    e = make_emitter(scn.all_none(), _alerts=[{'event': 'x'}], _alerts_ts=time.time())
    assert e._build_payload()['alertsStale'] is False
    e._alerts_ts = time.time() - 7200
    assert e._build_payload()['alertsStale'] is True
    e._alerts_ts = None
    assert e._build_payload()['alertsStale'] is True


def test_alerts_default_empty(make_emitter):
    p = make_emitter(scn.all_none())._build_payload()
    assert p['alerts'] == [] and p['alertCount'] == 0


def test_alerts_json_serializable(make_emitter):
    out = _process([nws.air_quality_1(), nws.air_quality_2()])
    p = make_emitter(scn.all_none(), _alerts=out, _alerts_ts=time.time())._build_payload()
    json.dumps(p, allow_nan=False)
    assert p['alertCount'] == 1


# --------------------------------------------------------------- AQI forecast
def test_aqi_forecast_summary_rising():
    base = datetime(2026, 8, 3, 14, 0)
    now = TZ.localize(base).timestamp()
    series, peak, ptime, trend, ttext, pcat = A._aqi_forecast_summary(nws.hourly_rising(base), now, TZ, 40)
    assert peak == 55 and pcat == 'Moderate' and trend == 'rising'
    assert 'Moderate' in ttext
    assert series and all(isinstance(e, int) and isinstance(v, int) for e, v in series)


def test_aqi_forecast_summary_steady():
    base = datetime(2026, 8, 3, 14, 0)
    now = TZ.localize(base).timestamp()
    series, peak, ptime, trend, ttext, pcat = A._aqi_forecast_summary(nws.hourly_steady(base), now, TZ, 41)
    assert trend == 'steady' and ttext is None


def test_aqi_cat_thresholds():
    assert A._aqi_cat(50) == 'Good'
    assert A._aqi_cat(55) == 'Moderate'
    assert A._aqi_cat(175) == 'Unhealthy'
