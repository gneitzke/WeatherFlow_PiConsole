""" Regression coverage for malformed packets and derived-value edge cases. """

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from lib import derived_variables as derive
from lib import observation_parser as parser_module
from lib import properties
from tests.fixtures.config import make_config


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _empty_rain():
    return {'today': [None, 'mm'], 'yesterday': [None, 'mm'],
            'month': [None, 'mm'], 'year': [None, 'mm']}


def _obs_st(epoch):
    return {'device_id': 111, 'obs': [[epoch, 0, 1, 2, 180, 60, 1010, 18,
            50, 1, 2, 300, 0, 0, 0, 0, 2, 60, 0, 0, 0, 0]]}


def _app(config):
    conditions = SimpleNamespace(Obs=properties.Obs(), switchPanel=lambda *a, **k: None,
                                 button_list=[])
    return SimpleNamespace(config=config, CurrentConditions=conditions)


def _station_config(**system):
    return make_config(
        System={'Connection': 'UDP', 'rest_api': '1', 'stats_endpoint': '1',
                'nc_rain': '0'} | system,
        Station={'SkyID': '', 'SkySN': '', 'InAirID': '', 'InAirSN': '',
                 'OutAirID': '', 'OutAirSN': '', 'StationID': 'station'})


def test_short_statistics_rows_and_websocket_packets_are_unavailable(make_parser,
                                                                       monkeypatch):
    config = _station_config()
    date = datetime.now(timezone.utc).astimezone(
        derive.pytz.timezone(config['Station']['Timezone'])).strftime('%Y-%m-%d')
    row = [date] + [0] * 28
    row[28] = 4.5
    response = _Response({'stats_day': [row]})
    monkeypatch.setattr(derive.weatherflow_api, 'verify_response', lambda *args: True)

    result = derive.rain_accumulation([0, 'mm'], [0, 'mm'], _empty_rain(), '111',
                                      {'111': {'statistics': response}}, config)
    assert result['today'][0] == 4.5
    assert result['yesterday'][0] is None

    app = _app(make_config(System={'nc_rain': '0'}))
    parser = make_parser(app)
    parser.calc_derived_variables = lambda *args: pytest.fail('short packet was parsed')
    parser.parse_obs_st({'device_id': 111, 'obs': [[1, 2, 3]]}, app.config)
    assert 'obs_st' not in parser.display_obs


@pytest.mark.parametrize(('method', 'kind', 'message'), [
    ('parse_obs_st', 'obs_st', _obs_st(1)),
    ('parse_obs_sky', 'obs_sky', {'device_id': 222,
     'obs': [[1, 0, 2, 0, 0, 1, 2, 180, 0, 0, 300, 0, 0, 0, 0, 0]]}),
    ('parse_obs_out_air', 'obs_out_air', {'device_id': 333,
     'obs': [[1, 1010, 18, 50, 0]]}),
    ('parse_obs_in_air', 'obs_in_air', {'device_id': 444, 'obs': [[1, 0, 20]]}),
])
def test_duplicate_observations_preserve_rest_cache(make_parser, method, kind, message):
    config = make_config(System={'rest_api': '1', 'nc_rain': '0',
                                 'stats_endpoint': '0'},
                         Station={'SkyID': '222', 'OutAirID': '333', 'InAirID': '444'})
    parser = make_parser(_app(config))
    parser.display_obs[kind] = deepcopy(message)
    parser.api_data[message['device_id']] = {'flagAPI': 0, '24Hrs': object(),
                                               'today': object(), 'series': [1]}

    getattr(parser, method)(message, config)

    assert parser.api_data[message['device_id']]['series'] == [1]
    assert '24Hrs' in parser.api_data[message['device_id']]


def test_statistics_retry_fetches_statistics_for_missing_daily_values(make_parser,
                                                                        monkeypatch):
    config = _station_config()
    parser = make_parser(_app(config))
    parser.calc_derived_variables = lambda *args: None
    for key in ('SLPMin', 'SLPMax', 'outTempMin', 'outTempMax', 'windAvg',
                'gustMax', 'peakSun'):
        parser.derive_obs[key][0] = 1
    for period in ('today', 'yesterday', 'month', 'year'):
        parser.derive_obs['rainAccum'][period][0] = 1
    for period in ('month', 'year'):
        parser.derive_obs['strikeCount'][period][0] = 1

    calls = []
    monkeypatch.setattr(parser_module.weatherflow_api, 'last_24h',
                        lambda *args: calls.append('24Hrs') or object())
    for name in ('today', 'yesterday', 'month', 'year', 'statistics'):
        monkeypatch.setattr(parser_module.weatherflow_api, name,
                            lambda *args, _name=name: calls.append(_name) or object())

    parser.parse_obs_st(_obs_st(100), config)

    assert calls == ['24Hrs', 'statistics']


def test_peak_sun_uses_utc_events_and_integrates_through_polar_day(monkeypatch):
    config = make_config(System={'rest_api': '0'},
                         Station={'Timezone': 'Europe/London', 'SkyID': '',
                                  'SkySN': ''})

    class EventObserver:
        pressure = lat = lon = horizon = date = None

        def next_rising(self, sun):
            return SimpleNamespace(datetime=lambda: datetime(2026, 7, 1, 3, 0))

        def next_setting(self, sun):
            return SimpleNamespace(datetime=lambda: datetime(2026, 7, 1, 21, 0))

    monkeypatch.setattr(derive.ephem, 'Observer', EventObserver)
    result = derive.peak_sun_hours([100, 'Wm2'], [None, 'hrs', '-'], '111', {}, config)
    assert result[4] == datetime(2026, 7, 1, 3, tzinfo=timezone.utc).timestamp()

    class PolarObserver(EventObserver):
        def next_rising(self, sun):
            raise derive.ephem.AlwaysUpError

    monkeypatch.setattr(derive.ephem, 'Observer', PolarObserver)
    polar = derive.peak_sun_hours([100, 'Wm2'], [None, 'hrs', '-'], '111', {}, config)
    assert polar[0] == pytest.approx(100 / 60 / 1000)
    assert polar[4:6] == [None, None]


def test_year_rain_midnight_uses_yesterdays_daily_accumulation():
    config = make_config(System={'Connection': 'Websocket', 'rest_api': '0',
                                 'nc_rain': '0'},
                         Station={'SkyID': '', 'SkySN': ''})
    now = derive.time.time()
    yesterday = now - 24 * 60 * 60
    rain = {'today': [5, 'mm', 5, yesterday], 'yesterday': [4, 'mm', 4, yesterday],
            'month': [105, 'mm', 100, yesterday], 'year': [105, 'mm', 100, yesterday]}

    result = derive.rain_accumulation([0, 'mm'], [0, 'mm'], rain, '111', {}, config)

    assert result['year'][0] == 105
    assert result['year'][2] == 105


def test_strike_frequency_includes_quiet_minutes_and_excludes_old_rows(monkeypatch):
    config = make_config(System={'rest_api': '1'})
    end = 10_000
    rows = []
    for offset in range(540, -1, -60):
        row = [end - offset] + [None] * 14 + [10 if offset == 60 else 0]
        rows.append(row)
    rows.append([end - 720] + [None] * 14 + [100])
    monkeypatch.setattr(derive.weatherflow_api, 'verify_response', lambda *args: True)

    frequency = derive.strike_frequency([end, 's'], '111',
                                        {'111': {'24Hrs': _Response({'obs': rows})}}, config)

    assert frequency[:2] == [pytest.approx(1.0), '/min']
    assert frequency[2] is None
