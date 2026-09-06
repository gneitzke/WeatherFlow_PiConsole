""" Regression coverage for strict JSON, dated forecasts, and live alert expiry. """

import json
import math
import time

from lib import almanac_emit as ae
from lib.observation_format import format as format_observation
from tests.fixtures import obs_scenarios as scn
from tests.fixtures.config import make_config


def test_formatted_nonfinite_values_remain_strict_json(make_emitter):
    scenario = scn.all_none()
    scenario['Obs']['outTemp'] = format_observation([float('nan'), 'c'], 'Temp')
    scenario['Obs']['Humidity'] = ['inf', '%']
    emitter = make_emitter(scenario)
    emitter._baro_series_cache = [[1, math.inf]]
    emitter._baro_series_t = time.time()

    payload = emitter._build_payload()
    assert payload['temp'] is None
    assert payload['humidity'] is None
    assert payload['slpSeries'] == [[1, None]]
    json.dumps(payload, allow_nan=False)
    emitter._write_atomic(payload)
    assert json.loads(open(emitter.output_path).read())['temp'] is None


def test_emit_reexpires_raw_alerts_and_regroups_remaining_members(make_emitter, monkeypatch):
    now = 2_000_000_000
    def alert(area, ends):
        return {'event': 'Wind Advisory', 'areaDesc': area,
                'ends': time_iso(ends), 'description': 'Winds.'}

    emitter = make_emitter(scn.all_none())
    emitter._alert_features = [alert('King, WA', now + 10), alert('Kitsap, WA', now + 30)]
    emitter._alerts_ts = now
    monkeypatch.setattr(ae.time, 'time', lambda: now)
    assert emitter._build_payload()['alerts'][0]['areaShort'] == 'King, Kitsap'

    monkeypatch.setattr(ae.time, 'time', lambda: now + 15)
    payload = emitter._build_payload()
    assert payload['alertCount'] == 1
    assert payload['alerts'][0]['areaShort'] == 'Kitsap'
    assert payload['alerts'][0]['until'] == now + 30


def time_iso(epoch):
    return ae.datetime.fromtimestamp(epoch, ae.timezone.utc).isoformat()


def test_date_keyed_forecast_summaries_ignore_missing_and_historical_days():
    rows = [
        {'date': '2026-09-06', 'today': True, 'code': 0, 'gust': 0},
        {'date': '2026-09-08', 'today': False, 'code': 95, 'gust': 0},
    ]
    assert ae.AlmanacEmitter._tomorrow_hint(rows) is None
    rows.append({'date': '2026-09-07', 'today': False, 'code': 61, 'gust': 0})
    assert ae.AlmanacEmitter._tomorrow_hint(rows) == 'Rain tomorrow'

    history_and_forecast = [
        {'day': '2026-09-05', 'max': 160, 'avg': 130},
        {'day': '2026-09-06', 'max': 40, 'avg': 40},
        {'day': '2026-09-07', 'max': 35, 'avg': 30},
    ]
    _, peak, _, trend, text, category = ae.AlmanacEmitter._waqi_trend(
        40, history_and_forecast, '2026-09-06')
    assert (peak, trend, text, category) == (40, 'steady', None, 'Good')


def test_waqi_pollutant_aqi_is_not_emitted_as_pm25_concentration(make_emitter, monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({'status': 'ok', 'data': {
                'aqi': 42, 'iaqi': {'pm25': {'v': 40}},
                'forecast': {'daily': {'pm25': []}},
            }}).encode()

    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: Response())
    emitter = make_emitter(scn.all_none(), config=make_config(AirQuality={'WaqiToken': 'token'}))
    emitter._do_aqi()
    payload = emitter._build_payload()
    assert payload['aqi'] == 42
    assert payload['aqiPm25'] is None
