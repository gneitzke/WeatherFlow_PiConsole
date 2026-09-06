""" The hero day-curve's forward segment (fcHourly).

The curve used to draw the daily high at a hard-coded 15:00 and the low at
midnight - two times nothing in the payload knew. It now follows real hourly
temperatures, so these tests pin the shape of that list: the window is station
LOCAL (current hour through the end of tomorrow), the unit is whatever the
request asked for (no conversion here), junk points are skipped rather than
fatal, and the list is published in the same snapshot as the daily rows.
"""

import json
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

from lib import almanac_emit as ae
from tests.fixtures import obs_scenarios as scn


TZ = pytz.timezone('America/Los_Angeles')


def _at(local_str):
    """ Epoch of a station-local wall-clock time, 'YYYY-MM-DD HH:MM'. """
    return TZ.localize(datetime.strptime(local_str, '%Y-%m-%d %H:%M')).timestamp()


def _hourly(start_local, count, temps=None):
    """ Open-Meteo's hourly block: local-naive ISO hours, as timezone=auto sends. """
    start = datetime.strptime(start_local, '%Y-%m-%d %H:%M')
    times = [(start + timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(count)]
    return {'time': times,
            'temperature_2m': temps if temps is not None else [60 + i for i in range(count)]}


# --------------------------------------------------------------- window
def test_window_runs_from_the_current_hour_through_the_end_of_tomorrow():
    now = _at('2026-08-29 14:37')
    hourly = _hourly('2026-08-29 12:00', 72)            # noon today -> +3 days
    pts = ae.AlmanacEmitter._fc_hourly_from(hourly, now, TZ)

    assert pts[0][0] == _at('2026-08-29 14:00')         # the hour we are IN, not 15:00
    assert pts[-1][0] == _at('2026-08-30 23:00')        # last hour of tomorrow
    assert len(pts) == 34
    # strictly increasing, one point per hour
    assert [b[0] - a[0] for a, b in zip(pts, pts[1:])] == [3600] * (len(pts) - 1)


def test_window_is_station_local_not_utc():
    """ 23:00 UTC on the 29th is 16:00 local; the window must follow the station,
    so the list runs to the end of the 30th local, never the 30th UTC. """
    now = _at('2026-08-29 16:00')
    pts = ae.AlmanacEmitter._fc_hourly_from(_hourly('2026-08-29 00:00', 72), now, TZ)
    last = datetime.fromtimestamp(pts[-1][0], TZ)
    assert (last.date(), last.hour) == (date(2026, 8, 30), 23)


def test_midnight_needs_forty_eight_hours_and_the_list_is_capped_there():
    now = _at('2026-08-29 00:05')
    pts = ae.AlmanacEmitter._fc_hourly_from(_hourly('2026-08-29 00:00', 96), now, TZ)
    assert len(pts) == 48
    assert pts[0][0] == _at('2026-08-29 00:00')


def test_past_hours_are_dropped():
    now = _at('2026-08-29 14:37')
    pts = ae.AlmanacEmitter._fc_hourly_from(_hourly('2026-08-29 00:00', 48), now, TZ)
    assert all(p[0] >= _at('2026-08-29 14:00') for p in pts)


# ----------------------------------------------------------------- values
def test_temperatures_pass_through_in_the_requested_unit_to_one_decimal():
    """ The request already asks Open-Meteo for the console's own unit, so the
    emitter must not convert - only round. """
    now = _at('2026-08-29 10:00')
    hourly = _hourly('2026-08-29 10:00', 3, temps=[71.24, 73.56, -0.06])
    assert [p[1] for p in ae.AlmanacEmitter._fc_hourly_from(hourly, now, TZ)] \
        == [71.2, 73.6, -0.1]


def test_junk_points_are_skipped_not_fatal():
    now = _at('2026-08-29 10:00')
    hourly = {'time': ['2026-08-29T10:00', 'not-a-time', '2026-08-29T12:00',
                       '2026-08-29T13:00', '2026-08-29T14:00'],
              'temperature_2m': [70, 71, None, '--', '72.5']}
    pts = ae.AlmanacEmitter._fc_hourly_from(hourly, now, TZ)
    assert pts == [[int(_at('2026-08-29 10:00')), 70.0],
                   [int(_at('2026-08-29 14:00')), 72.5]]


def test_missing_or_empty_hourly_block_yields_an_empty_list():
    now = _at('2026-08-29 10:00')
    for block in ({}, {'time': [], 'temperature_2m': []},
                  {'time': ['2026-08-29T10:00']},          # no temperatures
                  {'temperature_2m': [70]}):               # no times
        assert ae.AlmanacEmitter._fc_hourly_from(block, now, TZ) == []


def test_no_station_timezone_means_no_points():
    """ Local-naive times without a zone cannot be turned into honest epochs.
    Better an empty list (the console draws no future segment) than a curve an
    hour out of place. """
    now = _at('2026-08-29 10:00')
    assert ae.AlmanacEmitter._fc_hourly_from(_hourly('2026-08-29 10:00', 6), now, None) == []


def test_offset_aware_times_are_converted_not_mixed():
    """ timezone=auto sends naive times, but an offset-carrying response must
    not be read as if it were local. 18:00Z is 11:00 local. """
    now = _at('2026-08-29 10:00')
    hourly = {'time': ['2026-08-29T18:00+00:00'], 'temperature_2m': [70]}
    assert ae.AlmanacEmitter._fc_hourly_from(hourly, now, TZ) \
        == [[int(_at('2026-08-29 11:00')), 70.0]]


# ------------------------------------------------------------ payload / atomicity
class _InlineThread:
    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


def _response(payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return lambda *args, **kwargs: Response()


def _forecast_body():
    """ One Open-Meteo response carrying both blocks, dated around now so the
    emitter's live clock keeps them. """
    now_local = datetime.now(TZ)
    days = [(now_local.date() + timedelta(days=i)).isoformat() for i in range(3)]
    start = now_local.replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=i)).strftime('%Y-%m-%dT%H:%M') for i in range(6)]
    return {'daily': {'time': days, 'temperature_2m_max': [70, 71, 72],
                      'temperature_2m_min': [50, 51, 52], 'weather_code': [0, 0, 0],
                      'precipitation_probability_max': [0, 0, 0],
                      'precipitation_sum': [0, 0, 0], 'wind_gusts_10m_max': [10, 10, 10]},
            'hourly': {'time': times, 'temperature_2m': [61, 63, 65, 64, 62, 60]}}


def test_payload_carries_fchourly_and_defaults_to_empty(make_emitter, monkeypatch):
    emitter = make_emitter(scn.all_none())
    assert emitter._build_payload()['fcHourly'] == []       # nothing fetched yet

    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=_InlineThread))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', _response(_forecast_body()))
    emitter._do_forecast()

    pts = emitter._build_payload()['fcHourly']
    assert len(pts) == 6
    assert all(isinstance(p[0], int) and p[0] > 1e9 for p in pts)   # epoch SECONDS
    assert [p[1] for p in pts] == [61.0, 63.0, 65.0, 64.0, 62.0, 60.0]


def test_the_request_asks_for_hourly_temperature_in_the_same_call(make_emitter, monkeypatch):
    """ No second round trip: one URL carries the daily band and the curve. """
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=_InlineThread))
    import urllib.request
    seen = []

    def capture(req, timeout=None):
        seen.append(req.full_url)
        return _response(_forecast_body())()

    monkeypatch.setattr(urllib.request, 'urlopen', capture)
    make_emitter(scn.all_none())._do_forecast()

    assert len(seen) == 1
    assert 'hourly=temperature_2m' in seen[0]
    assert 'forecast_hours=48' in seen[0]
    assert 'temperature_unit=fahrenheit' in seen[0]      # the console's own unit


def test_hourly_and_rows_and_freshness_publish_together(make_emitter, monkeypatch):
    """ The snapshot is immutable: a tick landing mid-fetch sees the whole old
    forecast (no rows, no curve, flagged stale) or the whole new one. """
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=_InlineThread))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', _response(_forecast_body()))

    emitter = make_emitter(scn.all_none())
    mid_fetch = []
    shape = ae.AlmanacEmitter._fc_hourly_from

    def observe(hourly, now, tz):
        mid_fetch.append(emitter._build_payload())
        return shape(hourly, now, tz)

    monkeypatch.setattr(ae.AlmanacEmitter, '_fc_hourly_from', staticmethod(observe))
    emitter._do_forecast()

    during = mid_fetch[0]
    assert (during['fcHourly'], during['fcDaily'], during['fcStale']) == ([], [], True)
    after = emitter._build_payload()
    assert len(after['fcHourly']) == 6 and len(after['fcDaily']) == 3
    assert after['fcStale'] is False


def test_a_failed_fetch_keeps_the_previous_curve(make_emitter, monkeypatch):
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=_InlineThread))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', _response(_forecast_body()))
    emitter = make_emitter(scn.all_none())
    emitter._do_forecast()
    good = emitter._build_payload()['fcHourly']

    def boom(*args, **kwargs):
        raise OSError('wifi')

    monkeypatch.setattr(urllib.request, 'urlopen', boom)
    emitter._do_forecast()
    assert emitter._build_payload()['fcHourly'] == good


def test_the_curve_is_json_serializable_as_emitted(make_emitter, monkeypatch):
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=_InlineThread))
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', _response(_forecast_body()))
    emitter = make_emitter(scn.all_none())
    emitter._do_forecast()
    round_tripped = json.loads(json.dumps(emitter._build_payload(), allow_nan=False))
    assert round_tripped['fcHourly'] == emitter._build_payload()['fcHourly']
