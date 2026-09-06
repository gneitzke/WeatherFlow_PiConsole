""" Regressions from the adversarial review of the audit fixes: a REST seed
lives one accepted message (an echo keeps it, the next real message replaces
it); a strike window must be covered at both ends; a dated statistics row
must carry a real number; a retry armed after stop() must not register. """

import math
from types import SimpleNamespace

from lib import properties
from tests.fixtures.config import make_config


def _obs_st(epoch):
    return {'type': 'obs_st', 'device_id': 111,
            'obs': [[epoch, 0.5, 1.0, 2.0, 180, 60, 1010.0, 18.0, 60, 20000, 2.0,
                     500, 0.0, 0, 0, 0, 2.6, 60, 0.0, 0.0, 0.0, 0]]}


def test_rest_seed_lives_one_accepted_message(make_parser, monkeypatch):
    from lib import observation_parser as op
    cfg = make_config(System={'nc_rain': '0', 'Timeout': '5', 'stats_endpoint': '0', 'rest_api': '1'},
                      Station={'SkyID': '', 'SkySN': '', 'InAirID': '', 'InAirSN': ''},
                      Keys={'WeatherFlow': 'test-token'})
    app = SimpleNamespace(config=cfg, CurrentConditions=SimpleNamespace(Obs=properties.Obs(), button_list=[]))
    parser = make_parser(app)
    parser.calc_derived_variables = lambda *a: None
    parser.update_display = lambda *a: None
    monkeypatch.setattr(op.weatherflow_api, 'last_6h', lambda *a: 'H6')
    monkeypatch.setattr(op.weatherflow_api, 'last_24h', lambda *a: 'H24')
    monkeypatch.setattr(op.weatherflow_api, 'today', lambda *a: 'TODAY')
    monkeypatch.setattr(op.weatherflow_api, 'yesterday', lambda *a: 'YEST')
    monkeypatch.setattr(op.weatherflow_api, 'month', lambda *a: 'MONTH')
    monkeypatch.setattr(op.weatherflow_api, 'year', lambda *a: 'YEAR')
    monkeypatch.setattr(op.weatherflow_api, 'statistics', lambda *a: 'STATS')
    parser.parse_obs_st(_obs_st(1_700_000_000), cfg)
    assert parser.api_data[111]['24Hrs'] == 'H24' and parser.api_data[111]['today'] == 'TODAY'
    parser.parse_obs_st(_obs_st(1_700_000_000), cfg)            # the echo
    assert parser.api_data[111]['today'] == 'TODAY'              # cache survives the echo
    parser.derive_obs['peakSun'] = [1.0, 'hrs']                  # everything seeded now
    parser.derive_obs['windAvg'] = [1.0, 'mps']; parser.derive_obs['gustMax'] = [1.0, 'mps']
    for k in ('SLPMin', 'SLPMax', 'outTempMin', 'outTempMax'):
        parser.derive_obs[k] = [1.0, 'x']
    parser.derive_obs['rainAccum']['today'] = [0.0, 'mm']; parser.derive_obs['rainAccum']['yesterday'] = [0.0, 'mm']
    parser.derive_obs['rainAccum']['month'] = [0.0, 'mm']; parser.derive_obs['strikeCount']['today'] = [0, 'count']
    parser.derive_obs['strikeCount']['month'] = [0, 'count']
    parser.parse_obs_st(_obs_st(1_700_000_060), cfg)            # next real minute
    assert 'today' not in parser.api_data[111]                   # a seed is not a cache
    assert parser.api_data[111]['24Hrs'] == 'H24'                # the 24h trace is refreshed every message


def test_strike_window_needs_coverage_at_both_ends():
    from lib.derived_variables import _strike_frequency_window as w
    rows = [[t, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0] for t in range(9040, 10001, 60)]   # minute buckets ending at 10000
    rows[-1][15] = 10                                            # ten strikes in the last minute
    assert abs(w(rows, 15, 10000, 600, 120) - 1.0) < 1e-9        # 10 strikes over 10 minutes
    lone = [[9460, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10]]
    assert w(lone, 15, 10000, 600, 120) is None                  # nothing known about the tail
    rows[-1][15] = float('nan')
    assert w(rows, 15, 10000, 600, 120) == 0.0                   # junk is not a strike


def test_statistics_row_requires_a_real_number():
    from lib.derived_variables import _statistics_row

    class R:
        ok = True
        def __init__(self, rows): self._rows = rows
        def json(self): return {'status': {'status_message': 'SUCCESS'}, 'stats_day': self._rows}

    api = {'111': {'statistics': R([['2026-09-05'] + [0] * 23 + [4], ['2026-09-06'] + [0] * 23 + ['junk']])}}
    assert _statistics_row(api, '111', 'stats_day', '2026-09-06', 24) is None
    assert _statistics_row(api, '111', 'stats_day', '2026-09-05', 24)[24] == 4
    api = {'111': {'statistics': R([['2026-09-06'] + [0] * 23 + [True]])}}
    assert _statistics_row(api, '111', 'stats_day', '2026-09-06', 24) is None


def test_retry_armed_after_stop_does_not_register(make_emitter):
    e = make_emitter()
    e.start()
    e.stop()
    e._schedule_retry('forecast', lambda dt: None, 120)          # worker lost the race with stop()
    assert e._retries == {} and e._events == []
    assert e._schedule(lambda dt: None, 5) is None
