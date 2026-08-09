""" Network-free unit tests for lib/station_finder.

Every test runs with the module state reset (via the autouse _isolate fixture)
and every lib.config global the picker injects is snapshotted and restored, so
the suite is order-independent. requests.get and builtins.input are always
mocked — no test touches the network.
"""

import configparser
import math

import pytest

from lib import station_finder as sf
from tests.fixtures import station_finder_api as api


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, json_data=None, status_code=200, raise_json=False):
        self._json = json_data
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError('no json')
        return self._json


def _resp(value):
    return value if isinstance(value, FakeResponse) else FakeResponse(value)


def make_dispatch(map_resp=None, detail_map=None, obs_map=None, default_obs=None, calls=None):
    """ A requests.get stand-in that routes by URL to canned map/detail/obs
        responses. `calls` (if given) records every URL for lookup-count asserts. """
    detail_map = detail_map or {}
    obs_map = obs_map or {}

    def _get(url, timeout=None):
        if calls is not None:
            calls.append(url)
        if 'map/stations' in url:
            return _resp(map_resp)
        if '/rest/stations/' in url:
            sid = int(url.split('/rest/stations/')[1].split('?')[0])
            return _resp(detail_map.get(sid))
        if 'observations/station/' in url:
            sid = int(url.split('observations/station/')[1].split('?')[0])
            return _resp(obs_map.get(sid, default_obs))
        raise AssertionError('unexpected url: ' + url)

    return _get


def feed_input(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr('builtins.input', lambda *a, **k: next(it))


def no_input(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError('input() must not be called')
    monkeypatch.setattr('builtins.input', _boom)


def make_cfg(token='TESTTOKEN', **station):
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.optionxform = str
    cfg.add_section('Keys')
    cfg.set('Keys', 'WeatherFlow', token)
    cfg.add_section('Station')
    for key, value in station.items():
        cfg.set('Station', key, str(value))
    cfg.add_section('System')
    return cfg


@pytest.fixture(autouse=True)
def _isolate():
    """ Reset module state before each test and restore any lib.config globals
        the picker injects. """
    sf.reset()
    import lib.config as cfgmod
    saved = (cfgmod.TEMPEST, cfgmod.INDOORAIR, cfgmod.STATION, cfgmod.idx, cfgmod.CONNECTION)
    yield
    (cfgmod.TEMPEST, cfgmod.INDOORAIR, cfgmod.STATION, cfgmod.idx, cfgmod.CONNECTION) = saved
    sf.reset()


# ---------------------------------------------------------------------------
# 1. bounding_box
# ---------------------------------------------------------------------------
def test_bounding_box_equal_at_equator():
    lat_min, lat_max, lon_min, lon_max = sf.bounding_box(0, 0, 10)
    assert abs((lat_max - 0) - 0.0898) < 0.001
    assert abs((lon_max - 0) - 0.0898) < 0.001
    assert abs((lat_max - 0) - (lon_max - 0)) < 1e-9


def test_bounding_box_lon_widens_with_latitude():
    _, lat_max, _, lon_max = sf.bounding_box(60, 0, 10)
    dlat = lat_max - 60
    dlon = lon_max - 0
    assert abs(dlon - 2 * dlat) < 0.01


def test_bounding_box_no_div_by_zero_near_pole():
    box = sf.bounding_box(89.9, 0, 10)
    assert all(math.isfinite(x) for x in box)


def test_bounding_box_clamped_at_pole_and_antimeridian():
    lat_min, lat_max, lon_min, lon_max = sf.bounding_box(89.9, 179.9, 100)
    assert lat_max == 90.0
    assert lat_min >= -90.0
    assert lon_max <= 180.0
    assert lon_min >= -180.0


# ---------------------------------------------------------------------------
# 2. haversine_km
# ---------------------------------------------------------------------------
def test_haversine_identical_is_zero():
    assert sf.haversine_km(47.6, -122.3, 47.6, -122.3) == 0


def test_haversine_known_pair_within_one_percent():
    # Seattle -> Portland is ~233 km great-circle
    d = sf.haversine_km(47.6062, -122.3321, 45.5152, -122.6784)
    assert abs(d - 233.0) / 233.0 < 0.01


def test_haversine_symmetric():
    a = sf.haversine_km(47.6, -122.3, 45.5, -122.7)
    b = sf.haversine_km(45.5, -122.7, 47.6, -122.3)
    assert a == b


# ---------------------------------------------------------------------------
# 3. fetch_nearby_stations sort/filter
# ---------------------------------------------------------------------------
def test_fetch_sorts_and_filters(monkeypatch):
    features = [
        api.map_feature(1, 'Far', 48.6, -122.3),                     # ~111 km
        api.map_feature(2, 'Near', 47.65, -122.3),                   # ~5.5 km
        api.map_feature(3, 'Mid', 47.9, -122.3),                     # ~33 km
        api.map_feature(4, 'NoStatus', 47.61, -122.3, station_status=0),
        {'type': 'Feature',                                          # no geometry
         'properties': {'station_id': 5, 'name': 'NoGeom', 'station_status': 1,
                        'devices': [{'device_id': 1, 'device_type': 'ST'}]}},
        api.map_feature(6, 'NoDevices', 47.62, -122.3, devices=[]),  # empty devices
    ]
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(api.map_response(features)))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 50, 20)
    assert err is None
    assert [s['station_id'] for s in stations] == [2, 3, 1]


def test_fetch_honors_lon_lat_order(monkeypatch):
    correct = api.map_feature(1, 'Correct', 47.62, -122.3)          # coords [-122.3, 47.62]
    swapped = {'type': 'Feature',                                   # coords put lat first
               'geometry': {'type': 'Point', 'coordinates': [47.62, -122.3]},
               'properties': {'station_id': 2, 'name': 'Swapped', 'station_status': 1,
                              'devices': [{'device_id': 1, 'device_type': 'ST'}]}}
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(api.map_response([swapped, correct])))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 50, 20)
    assert err is None
    assert stations[0]['station_id'] == 1          # correctly-ordered feature wins
    assert stations[0]['distance_km'] < 10


def test_fetch_empty_features_is_success(monkeypatch):
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(api.map_response([])))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 10, 20)
    assert stations == []
    assert err is None


# ---------------------------------------------------------------------------
# 4. fetch_nearby_stations error mapping
# ---------------------------------------------------------------------------
def test_fetch_network_error(monkeypatch):
    def boom(url, timeout=None):
        raise RuntimeError('connection reset')
    monkeypatch.setattr(sf.requests, 'get', boom)
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 10, 20)
    assert stations is None
    assert err == 'network'


def test_fetch_http_401_unauthorized(monkeypatch):
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(None, status_code=401))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 10, 20)
    assert stations is None
    assert err == 'unauthorized'


def test_fetch_unauthorized_status_message(monkeypatch):
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(api.map_unauthorized()))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 10, 20)
    assert stations is None
    assert err == 'unauthorized'


def test_fetch_non_json_invalid(monkeypatch):
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(raise_json=True))
    stations, err = sf.fetch_nearby_stations('tok', 47.6, -122.3, 10, 20)
    assert stations is None
    assert err == 'invalid'


# ---------------------------------------------------------------------------
# 5. classify_devices
# ---------------------------------------------------------------------------
def test_classify_tempest_only():
    station = api.detail_tempest()['stations'][0]
    assert sf.classify_devices(station)['mode'] == 'tempest'


def test_classify_tempest_wins_over_sky_air():
    station = api.detail_tempest_plus()['stations'][0]
    result = sf.classify_devices(station)
    assert result['mode'] == 'tempest'
    assert result['devices']['ST'] is not None


def test_classify_sky_air():
    station = api.detail_sky_air()['stations'][0]
    assert sf.classify_devices(station)['mode'] == 'sky_air'


def test_classify_sky_only_ineligible():
    station = api.detail_sky_only()['stations'][0]
    assert sf.classify_devices(station)['mode'] is None


def test_classify_air_only_ineligible():
    station = api.detail_air_only()['stations'][0]
    assert sf.classify_devices(station)['mode'] is None


def test_classify_hb_only_ineligible():
    station = api.detail_hb_only()['stations'][0]
    assert sf.classify_devices(station)['mode'] is None


def test_classify_two_airs_prefers_outdoor():
    station = api.detail_two_airs_second_outdoor()['stations'][0]
    result = sf.classify_devices(station)
    assert result['mode'] == 'sky_air'
    assert result['devices']['AR_out']['device_id'] == 6002
    assert result['devices']['AR_out']['device_meta']['environment'] == 'outdoor'


def test_classify_air_without_environment_key():
    station = api.detail_air_no_environment()['stations'][0]
    result = sf.classify_devices(station)
    assert result['mode'] == 'sky_air'
    assert result['devices']['AR_out'] is not None


# ---------------------------------------------------------------------------
# 6. build_candidates
# ---------------------------------------------------------------------------
def test_build_candidates_caps_at_ten(monkeypatch):
    stations = [{'station_id': i, 'name': 'S%d' % i, 'distance_km': float(i)}
                for i in range(1, 16)]                              # 15 eligible
    detail_map = {i: api.detail_tempest(i) for i in range(1, 16)}
    calls = []
    monkeypatch.setattr(sf.requests, 'get', make_dispatch(detail_map=detail_map, calls=calls))
    candidates = sf.build_candidates(stations, 'tok', 20)
    assert len(candidates) == 10
    assert sum('/rest/stations/' in c for c in calls) == 10


def test_build_candidates_lookup_capped_at_twenty(monkeypatch):
    stations = [{'station_id': i, 'name': 'S%d' % i, 'distance_km': float(i)}
                for i in range(1, 26)]                              # 25 HB-only
    detail_map = {i: api.detail_hb_only(i) for i in range(1, 26)}
    calls = []
    monkeypatch.setattr(sf.requests, 'get', make_dispatch(detail_map=detail_map, calls=calls))
    candidates = sf.build_candidates(stations, 'tok', 20)
    assert candidates == []
    assert sum('/rest/stations/' in c for c in calls) == 20


def test_build_candidates_skips_and_caches_failure(monkeypatch):
    stations = [{'station_id': 1, 'name': 'bad', 'distance_km': 1.0},
                {'station_id': 2, 'name': 'good', 'distance_km': 2.0}]
    detail_map = {1: FakeResponse(None, status_code=500), 2: api.detail_tempest(2)}

    calls = []
    monkeypatch.setattr(sf.requests, 'get', make_dispatch(detail_map=detail_map, calls=calls))
    candidates = sf.build_candidates(stations, 'tok', 20)
    assert [c['station_id'] for c in candidates] == [2]
    assert 1 in sf._detail_cache and sf._detail_cache[1] is None

    # Widen: a second pass over the same IDs must do zero fresh lookups
    calls2 = []
    monkeypatch.setattr(sf.requests, 'get', make_dispatch(detail_map=detail_map, calls=calls2))
    candidates2 = sf.build_candidates(stations, 'tok', 20)
    assert [c['station_id'] for c in candidates2] == [2]
    assert sum('/rest/stations/' in c for c in calls2) == 0


# ---------------------------------------------------------------------------
# 7. present_and_choose
# ---------------------------------------------------------------------------
def _sample_candidates():
    return [
        {'name': 'Alpha', 'distance_km': 1.0, 'distance_mi': 0.6, 'mode': 'sky_air', 'devices': {}},
        {'name': 'Bravo', 'distance_km': 2.0, 'distance_mi': 1.2, 'mode': 'tempest',
         'devices': {'ST': {'serial_number': 'ST-1'}}},
    ]


def test_present_reprompts_then_accepts(monkeypatch, capsys):
    cands = _sample_candidates()
    feed_input(monkeypatch, ['0', '99', 'x', '', '2'])
    result = sf.present_and_choose(cands, (0, 0), 10, False)
    assert result is cands[1]
    assert capsys.readouterr().out.count('Selection not recognised. Please try again') == 4


def test_present_quit_returns_none(monkeypatch):
    feed_input(monkeypatch, ['q'])
    assert sf.present_and_choose(_sample_candidates(), (0, 0), 10, False) is None


def test_present_widen_when_not_at_max(monkeypatch):
    feed_input(monkeypatch, ['w'])
    assert sf.present_and_choose(_sample_candidates(), (0, 0), 10, False) == 'widen'


def test_present_widen_at_max_reprompts_then_accepts(monkeypatch, capsys):
    cands = _sample_candidates()
    feed_input(monkeypatch, ['w', '1'])
    result = sf.present_and_choose(cands, (0, 0), 100, True)
    assert result is cands[0]
    assert 'Maximum search radius reached (100 km)' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 8. apply_selection
# ---------------------------------------------------------------------------
def test_apply_selection_tempest():
    import lib.config as cfgmod
    cfg = make_cfg()
    detail = api.detail_tempest(1001)
    candidate = {'station_id': 1001, 'name': 'Tempest', 'mode': 'tempest',
                 'devices': {'ST': {'device_id': 5001, 'serial_number': 'ST-1'},
                             'SK': None, 'AR_out': None},
                 'detail': detail}
    sf.apply_selection(cfg, candidate)
    assert cfg['Station']['StationID'] == '1001'
    assert cfg['Station']['TempestID'] == '5001'
    assert cfg['Station']['SkyID'] == ''
    assert cfg['Station']['OutAirID'] == ''
    assert cfg['Station']['InAirID'] == ''
    assert cfgmod.TEMPEST is True
    assert cfgmod.INDOORAIR is False
    assert cfgmod.STATION is detail
    assert cfgmod.idx == 0


def test_apply_selection_sky_air():
    import lib.config as cfgmod
    cfg = make_cfg()
    detail = api.detail_sky_air(1003)
    candidate = {'station_id': 1003, 'name': 'SkyAir', 'mode': 'sky_air',
                 'devices': {'ST': None, 'SK': {'device_id': 5002},
                             'AR_out': {'device_id': 5003}},
                 'detail': detail}
    sf.apply_selection(cfg, candidate)
    assert cfg['Station']['SkyID'] == '5002'
    assert cfg['Station']['OutAirID'] == '5003'
    assert cfg['Station']['TempestID'] == ''
    assert cfg['Station']['InAirID'] == ''
    assert cfgmod.TEMPEST is False
    assert cfgmod.INDOORAIR is False


# ---------------------------------------------------------------------------
# 9. intercept guards
# ---------------------------------------------------------------------------
def test_intercept_ignores_non_websocket(monkeypatch):
    no_input(monkeypatch)
    monkeypatch.setattr('lib.config.query_user',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('query_user called')))
    cfg = make_cfg()
    for connection in (None, 2, 3):
        assert sf.intercept(cfg, 'StationID', connection) is False


def test_intercept_declined_offer_latches(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: False)
    assert sf.intercept(cfg, 'StationID', 1) is False

    # Second StationID prompt must NOT re-offer
    calls = []
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: calls.append(1) or False)
    assert sf.intercept(cfg, 'StationID', 1) is False
    assert calls == []


def test_intercept_suppresses_device_ids_after_success(monkeypatch):
    cfg = make_cfg()
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: True)
    monkeypatch.setattr(sf, 'run_picker', lambda config: True)
    assert sf.intercept(cfg, 'StationID', 1) is True
    assert sf.intercept(cfg, 'TempestID', 1) is True
    assert sf.intercept(cfg, 'InAirID', 1) is True
    assert sf.intercept(cfg, 'TempestSN', 1) is False


# ---------------------------------------------------------------------------
# 10. run_picker paths
# ---------------------------------------------------------------------------
def test_run_picker_three_network_failures(monkeypatch):
    cfg = make_cfg(Latitude='47.6', Longitude='-122.3')
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: True)   # use configured location

    def boom(url, timeout=None):
        raise RuntimeError('net down')
    monkeypatch.setattr(sf.requests, 'get', boom)
    assert sf.run_picker(cfg) is False


def test_run_picker_unauthorized_then_recovers(monkeypatch):
    cfg = make_cfg(token='BADTOKEN', Latitude='47.6', Longitude='-122.3')
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: True)
    state = {'authed': False}
    detail = api.detail_tempest(1001, 'Nice')

    def dispatch(url, timeout=None):
        if 'map/stations' in url:
            if not state['authed']:
                return FakeResponse(api.map_unauthorized())
            return FakeResponse(api.map_response([api.map_feature(1001, 'Nice', 47.62, -122.3)]))
        if '/rest/stations/' in url:
            return FakeResponse(detail)
        if 'observations/station/' in url:
            return FakeResponse(api.obs_response())
        raise AssertionError('unexpected url: ' + url)
    monkeypatch.setattr(sf.requests, 'get', dispatch)

    inputs = iter(['GOODTOKEN', '1'])

    def fake_input(*a, **k):
        value = next(inputs)
        if value == 'GOODTOKEN':
            state['authed'] = True
        return value
    monkeypatch.setattr('builtins.input', fake_input)

    assert sf.run_picker(cfg) is True
    assert cfg['Keys']['WeatherFlow'] == 'GOODTOKEN'
    assert cfg['Station']['StationID'] == '1001'


def test_run_picker_empty_at_all_radii(monkeypatch, capsys):
    cfg = make_cfg(Latitude='47.6', Longitude='-122.3')
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: True)
    monkeypatch.setattr(sf.requests, 'get',
                        lambda url, timeout=None: FakeResponse(api.map_response([])))
    assert sf.run_picker(cfg) is False
    out = capsys.readouterr().out
    assert 'No usable public stations within 10 km. Widening search to 25 km' in out
    assert 'No public stations found within 100 km of your location' in out


def test_run_picker_preflight_failure_reselects(monkeypatch, capsys):
    cfg = make_cfg(Latitude='47.6', Longitude='-122.3')
    monkeypatch.setattr('lib.config.query_user', lambda *a, **k: True)
    d1 = api.detail_tempest(1001, 'First')
    d2 = api.detail_tempest(1002, 'Second')

    def dispatch(url, timeout=None):
        if 'map/stations' in url:
            return FakeResponse(api.map_response([
                api.map_feature(1001, 'First', 47.62, -122.3),
                api.map_feature(1002, 'Second', 47.63, -122.3)]))
        if '/rest/stations/1001' in url:
            return FakeResponse(d1)
        if '/rest/stations/1002' in url:
            return FakeResponse(d2)
        if 'observations/station/1001' in url:
            return FakeResponse(api.obs_private())     # fails preflight
        if 'observations/station/1002' in url:
            return FakeResponse(api.obs_response())
        raise AssertionError('unexpected url: ' + url)
    monkeypatch.setattr(sf.requests, 'get', dispatch)

    feed_input(monkeypatch, ['1', '1'])                # pick first (fails), then the remaining one
    assert sf.run_picker(cfg) is True
    assert cfg['Station']['StationID'] == '1002'
    assert "is not currently sharing public data" in capsys.readouterr().out
