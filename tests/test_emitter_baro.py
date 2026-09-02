""" Tests for AlmanacEmitter._baro_series — the 24h barograph trace.

This is the emitter's one genuinely heavy seam: it walks the core's cached REST
24h payload and runs each pressure sample through the real derive.SLP. The
common case (no cached REST data yet) must yield [] so the HTML hides the graph;
the populated case must downsample to <=48 ordered [epoch, slp] points.
"""

from types import SimpleNamespace


class FakeResp:
    """ Stands in for the requests.Response the core caches under ['24Hrs']. """
    def __init__(self, obs):
        self._obs = obs

    def json(self):
        return {'obs': self._obs}


def test_baro_series_empty_when_no_api_data(make_emitter):
    emitter = make_emitter()                       # api_data defaults to {}
    assert emitter._baro_series() == []


def test_baro_series_downsamples_to_48(make_emitter):
    # 200 Tempest obs rows; pressure lives at index 6 for TEMPEST.
    rows = []
    for i in range(200):
        row = [0] * 8
        row[0] = 1_700_000_000 + i * 60          # epoch, +60s each, ascending
        row[6] = 1000.0 + (i % 10) * 0.3         # station pressure, mb
        rows.append(row)

    api_data = {'111': {'24Hrs': FakeResp(rows)}}   # device '111' == config TempestID
    emitter = make_emitter(api_data=api_data)
    series = emitter._baro_series()

    assert 0 < len(series) <= 48
    # each point is [int epoch, float slp]
    for epoch, slp in series:
        assert isinstance(epoch, int)
        assert isinstance(slp, float)
        assert 28 < slp < 31                     # sane sea-level pressure in the fixture's inHg
    # strictly time-ordered oldest -> newest
    times = [p[0] for p in series]
    assert times == sorted(times)


def test_baro_series_is_cached(make_emitter):
    api_data = {'111': {'24Hrs': FakeResp([[1_700_000_000, 0, 0, 0, 0, 0, 1005.0, 0]])}}
    emitter = make_emitter(api_data=api_data)
    first = emitter._baro_series()
    # mutate the source; cache (TTL 300s) should return the same object
    emitter.app.obsParser.api_data = {}
    assert emitter._baro_series() is first


def test_baro_series_follows_the_station_pressure_unit(make_emitter):
    # an inHg console must get an inHg trace - the big reading and the trace's
    # hi/lo numerals live in one tile and must share a unit system
    from tests.fixtures.config import make_config
    rows = [[1_700_000_000 + i * 60, 0, 0, 0, 0, 0, 1000.0 + i, 0] for i in range(5)]
    api_data = {'111': {'24Hrs': FakeResp(rows)}}
    config = make_config()
    config['Units']['Pressure'] = 'inhg'
    series = make_emitter(api_data=api_data, config=config)._baro_series()
    assert series and all(28.0 < slp < 31.0 for _, slp in series)
    assert all(round(slp, 3) == slp for _, slp in series)      # the core's inHg precision
    config_mb = make_config()
    config_mb['Units']['Pressure'] = 'mb'
    mb = make_emitter(api_data=api_data, config=config_mb)._baro_series()
    assert all(900 < slp < 1100 and round(slp, 1) == slp for _, slp in mb)
    assert abs(series[0][1] - mb[0][1] * 0.0295301) < 0.002    # same samples, converted
