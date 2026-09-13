"""Radar geometry, complete-frame cache, atomic publication and real palette fidelity."""
import builtins
import io
import json
import math
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from lib import almanac_emit as ae
from tests.fixtures.config import make_config


@pytest.fixture(autouse=True)
def radar_dir(tmp_path, monkeypatch):
    directory = tmp_path / 'radar'
    monkeypatch.setenv('WFP_RADAR_DIR', str(directory))
    monkeypatch.setattr(ae, 'RADAR_DIR', os.environ['WFP_RADAR_DIR'])
    return directory


@pytest.fixture
def radar_net(monkeypatch):
    tile = io.BytesIO()
    Image.new('RGBA', (256, 256), (146, 136, 113, 100)).save(tile, format='PNG')
    state = dict(times=[1800000000, 1800000600], calls=[], fail=None, tile=tile.getvalue())

    def fetch(req, timeout):
        assert timeout == 25
        assert req.get_header('User-agent') == 'WeatherFlow-PiConsole-almanac'
        url = req.full_url
        state['calls'].append(url)
        if state['fail']:
            state['fail'](url)
        if url == ae.RADAR_MANIFEST_URL:
            return io.BytesIO(json.dumps(dict(host='https://tiles.example', radar=dict(
                past=[dict(time=t, path=f'/v2/{t}') for t in reversed(state['times'])],
                nowcast=[dict(time=1999999999, path='/never')]))).encode())
        assert url.endswith('/2/1_1.png') and int(url.split('/256/')[1].split('/')[0]) in range(4, 8)
        return io.BytesIO(state['tile'])

    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    monkeypatch.setattr(ae.time, 'sleep', lambda _: None)
    return state


def tile_calls(state):
    return [u for u in state['calls'] if u != ae.RADAR_MANIFEST_URL]


@pytest.fixture
def radar_viewed(tmp_path):
    marker = tmp_path / 'radar_viewed'
    marker.write_text(str(ae.time.time()))
    return marker


@pytest.mark.parametrize('lat,expected', [(0, 7), (47.6, 7), (60, 7), (78, 6),
                                         (-78, 6), (85, 5), (90, 4), (-90, 4)])
def test_zoom_targets_coverage_with_clamps(lat, expected):
    zoom = ae._radar_zoom_for(lat)
    assert zoom == expected
    assert ae.RADAR_MIN_ZOOM <= zoom <= ae.RADAR_MAX_ZOOM
    raw = math.log2(156543.03392 * math.cos(math.radians(lat)) / (256000 / 480))
    if 4 <= round(raw) <= 7:
        coverage = ae._radar_viewport(lat, 0, zoom, 480)[1] * 480
        assert 256000 / math.sqrt(2) <= coverage <= 256000 * math.sqrt(2)
    assert ae._radar_zoom_for(0) > ae._radar_zoom_for(78)


def test_zoom_cached_until_latitude_changes(make_emitter, radar_net, monkeypatch):
    calls = []
    original = ae._radar_zoom_for
    def zoom_for(lat):
        calls.append(lat)
        return original(lat)
    monkeypatch.setattr(ae, '_radar_zoom_for', zoom_for)
    emitter = make_emitter()
    emitter._do_radar(); emitter._do_radar()
    assert calls == [47.61]
    emitter.app.config = make_config(Station={'Latitude': '78'})
    radar_net['times'] = [1800001200]
    radar_net['calls'].clear()
    emitter._do_radar()
    assert calls == [47.61, 78]
    assert emitter._build_payload()['radar']['zoom'] == 6
    assert all('/256/6/' in url for url in tile_calls(radar_net))


@pytest.mark.parametrize('marker', [None, 'old', 'boundary', 'garbage', '', 'nan', 'inf', '-inf', 'future', b'\xff'])
def test_unviewed_builds_only_latest(make_emitter, radar_net, radar_dir, tmp_path, marker):
    if marker is not None:
        marker = {'old': str(ae.time.time() - 901), 'boundary': str(ae.time.time() - 900),
                  'future': str(ae.time.time() + 3600)}.get(marker, marker)
        (tmp_path / 'radar_viewed').write_bytes(marker if isinstance(marker, bytes) else marker.encode())
    radar_net['times'] = [1800000000 + i * 600 for i in range(13)]
    emitter = make_emitter(); emitter._do_radar()
    frames = emitter._radar_frames
    assert [f['ts'] for f in frames] == radar_net['times']
    assert all(not f['complete'] and 'url' not in f for f in frames[:-1])
    assert frames[-1]['complete'] and emitter._radar_latest == frames[-1]['id']
    assert len(list(radar_dir.glob('*.png'))) == 1
    assert len(tile_calls(radar_net)) == 9
    assert len(radar_net['calls']) == 10


def test_viewing_warms_history_then_expiry_prunes_it(make_emitter, radar_net, radar_dir, tmp_path):
    radar_net['times'] = [1800000000 + i * 600 for i in range(13)]
    emitter = make_emitter(); emitter._do_radar()
    marker = tmp_path / 'radar_viewed'
    marker.write_text(str(ae.time.time()))
    radar_net['calls'].clear(); emitter._do_radar()
    assert len(tile_calls(radar_net)) == 12 * 9
    assert all(f['complete'] for f in emitter._radar_frames)
    assert len(list(radar_dir.glob('*.png'))) == 13
    marker.write_text(str(ae.time.time() - ae.RADAR_VIEW_TTL - 1))
    # Two new manifest frames: even an uncached intermediate frame is skipped.
    radar_net['times'] = radar_net['times'][2:] + [1800007800, 1800008400]
    radar_net['calls'].clear(); emitter._do_radar()
    assert len(tile_calls(radar_net)) == 9
    assert len(list(radar_dir.glob('*.png'))) == 1
    assert emitter._radar_latest == '1800008400'
    assert all(not f['complete'] for f in emitter._radar_frames[:-1])
    radar_net['calls'].clear(); emitter._do_radar()
    assert not tile_calls(radar_net)  # unchanged latest remains a cache hit


@pytest.mark.parametrize('lat,lon', [(47.61, -122.33), (0, 0), (-33.8, 151.2)])
def test_viewport_covers_crop_and_marker(lat, lon):
    tiles, mpp, bounds, marker = ae._radar_viewport(lat, lon, 7, 480)
    assert marker == pytest.approx((240, 240))
    assert mpp == pytest.approx(2 * math.pi * 6378137 * math.cos(math.radians(lat)) / 32768, rel=.001)
    assert bounds['s'] < lat < bounds['n'] and bounds['w'] < lon < bounds['e']
    mask = Image.new('1', (480, 480))
    for tx, ty, x, y in tiles:
        assert 0 <= tx < 128 and 0 <= ty < 128
        mask.paste(1, (x, y, x + 256, y + 256))
    assert mask.getextrema() == (1, 1)
    left = (lon + 180) / 360 * 32768 - 240
    top = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 32768 - 240
    for tx, ty, x, y in tiles:
        assert x == int(tx * 256 - left) and y == int(ty * 256 - top)


@pytest.mark.parametrize('lat,lon', [(0, 179.99), (0, -179.99), (90, 0), (-90, 0)])
def test_viewport_wraps_and_clamps(lat, lon):
    tiles, mpp, bounds, marker = ae._radar_viewport(lat, lon, 7, 480)
    assert all(0 <= x < 128 and 0 <= y < 128 for x, y, _, _ in tiles)
    assert math.isfinite(mpp) and mpp > 0
    if abs(lon) > 179:
        assert {0, 127} <= {t[0] for t in tiles}
        assert bounds['e'] < bounds['w']


@pytest.mark.parametrize('setting,unit,factor', [('mi', 'mi', 1609.344), ('miles', 'mi', 1609.344), ('km', 'km', 1000)])
def test_scale_and_rings(setting, unit, factor):
    mpp = ae._radar_viewport(47.61, -122.33, 7, 480)[1]
    assert ae._radar_distance_unit(make_config(Units={'Distance': setting})) == unit
    bar, rings = ae._radar_scale(mpp, 480, unit)
    dist = int(bar['distDisp'].split()[0])
    assert dist == max(d for d in [5, 10, 20, 25, 50, 100, 150, 200, 250] if d * factor / mpp <= 192)
    assert bar['pixels'] == pytest.approx(bar['meters'] / mpp)
    assert bar['meters'] == dist * factor and bar['unit'] == unit
    assert all(r['px'] <= 480 / math.sqrt(2) for r in rings)
    assert rings[0]['px'] == bar['pixels']


def test_nexrad_is_caption_only_and_uses_station_unit():
    assert len(ae._NEXRAD_SITES) == 160
    r = ae._radar_nexrad(47.61, -122.33, 'mi')
    assert r['id'] == 'KATX' and r['bearing'] == 'N' and r['distanceDisp'].endswith(' mi')
    assert ae._radar_nexrad(0, 0, 'km') is None
    assert ae._radar_nexrad(47.61, -122.33, 'km')['distanceDisp'].endswith(' km')


def test_composite_and_one_snapshot(make_emitter, radar_net, radar_dir, monkeypatch, radar_viewed):
    emitter = make_emitter()
    previous = emitter._radar_result
    publications = []
    original = ae.AlmanacEmitter.__setattr__
    def record(self, key, value):
        if key == '_radar_result':
            publications.append(value)
        original(self, key, value)
    monkeypatch.setattr(ae.AlmanacEmitter, '__setattr__', record)
    original_replace = os.replace
    def replace(src, dst):
        assert emitter._radar_result is previous
        assert str(src).endswith('.tmp.' + str(os.getpid()))
        with Image.open(src) as image:
            assert image.size == (480, 480)
        original_replace(src, dst)
    monkeypatch.setattr(os, 'replace', replace)
    emitter._do_radar()
    assert len(publications) == 1
    result = emitter._radar_result
    assert result.available and result.latest == '1800000600'
    assert [f['ts'] for f in result.frames] == radar_net['times']
    assert all(f['complete'] and f['url'] == 'radar/' + f['id'] + '.png' for f in result.frames)
    assert len(tile_calls(radar_net)) == 2 * len(ae._radar_viewport(47.61, -122.33, 7, 480)[0])
    with Image.open(radar_dir / (result.latest + '.png')) as image:
        assert image.getpixel((240, 240)) == (146, 136, 113, 100)  # alpha wasn't squared
    assert not list(radar_dir.glob('*.tmp.*'))
    payload = emitter._build_payload()['radar']
    assert payload['frameCount'] == 2 and payload['marker'] == dict(x=.5, y=.5)
    assert payload['observedAt'] == datetime.fromtimestamp(result.ts_frame, ae.AlmanacEmitter._station_tz(emitter.app.config)).strftime('%H:%M')


def test_cache_and_prune(make_emitter, radar_net, radar_dir, radar_viewed):
    emitter = make_emitter(); emitter._do_radar()
    radar_net['calls'].clear(); emitter._do_radar()
    assert not tile_calls(radar_net)
    assert radar_net['calls'] == [ae.RADAR_MANIFEST_URL]
    radar_net['times'] = [1800000600, 1800001200]
    emitter._do_radar()
    assert len(tile_calls(radar_net)) == len(ae._radar_viewport(47.61, -122.33, 7, 480)[0])
    assert sorted(p.stem for p in radar_dir.glob('*.png')) == ['1800000600', '1800001200']


@pytest.mark.parametrize('field', ['Latitude', 'Longitude'])
def test_missing_location(make_emitter, field):
    emitter = make_emitter(config=make_config(Station={field: ''})); emitter._do_radar()
    assert not emitter._radar_available and emitter._radar_reason == 'no location'


def test_zero_location_is_valid(make_emitter, radar_net):
    emitter = make_emitter(config=make_config(Station={'Latitude': '0', 'Longitude': '0'}))
    emitter._do_radar()
    assert emitter._radar_available and emitter._radar_nexrad is None


def test_pillow_absent(make_emitter, monkeypatch):
    real = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == 'PIL':
            raise ImportError('Pillow absent')
        return real(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', importing)
    emitter = make_emitter(); emitter._do_radar()
    assert not emitter._radar_available and emitter._radar_reason == 'compositor unavailable'


def test_stale_uses_frame_not_manifest(make_emitter, radar_net, monkeypatch):
    now = radar_net['times'][-1] + ae.RADAR_STALE_SEC
    monkeypatch.setattr(ae.time, 'time', lambda: now)
    emitter = make_emitter(); emitter._do_radar()
    r = emitter._build_payload()['radar']
    assert r['ageSec'] == 1200 and not r['stale'] and r['fetchedAt'] == now
    now += 1
    r = emitter._build_payload()['radar']
    assert r['stale'] and r['ageSec'] == 1201


@pytest.mark.parametrize('failure', ['manifest', 'tile', 'decode', 'save'])
def test_never_raises_keeps_last_good_and_warns_once(make_emitter, radar_net, monkeypatch, failure):
    emitter = make_emitter(); emitter._do_radar(); previous = emitter._radar_result
    radar_net['times'] = [1800001200]
    warnings, retries = [], []
    monkeypatch.setattr(ae.Logger, 'warning', warnings.append)
    monkeypatch.setattr(emitter, '_schedule_retry', lambda *a: retries.append(a))
    def fail(url):
        if failure == 'manifest' or (failure == 'tile' and url != ae.RADAR_MANIFEST_URL):
            raise urllib.error.URLError('offline')
    radar_net['fail'] = fail
    if failure == 'decode': radar_net['tile'] = b'bad PNG'
    if failure == 'save':
        monkeypatch.setattr(Image.Image, 'save', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    emitter._do_radar()
    assert emitter._radar_result is previous
    assert len(warnings) == len(retries) == 1
    assert retries[0] == ('radar', emitter._check_radar, 120)


def test_partial_latest_withheld_then_retried(make_emitter, radar_net, radar_dir):
    emitter = make_emitter(); emitter._do_radar()
    radar_net['times'].append(1800001200)
    failed = []
    def fail(url):
        if '/1800001200/' in url and not failed:
            failed.append(url); raise urllib.error.HTTPError(url, 404, 'not ready', {}, None)
    radar_net['fail'] = fail
    emitter._do_radar()
    assert emitter._radar_latest == '1800000600'
    assert not (radar_dir / '1800001200.png').exists()
    radar_net['fail'] = None; emitter._do_radar()
    assert emitter._radar_latest == '1800001200'


def test_radar_schedules_are_registered_and_cancelled(make_emitter, monkeypatch):
    from tests.test_emitter_lifecycle import FakeClock, HangingThread
    from types import SimpleNamespace
    clock = FakeClock(); monkeypatch.setattr(ae, 'Clock', clock)
    monkeypatch.setattr(ae, 'threading', SimpleNamespace(Thread=HangingThread))
    emitter = make_emitter(); emitter.start()
    assert sorted(e.timeout for e in clock.events if e.timeout in (60, 300)) == [60, 300]
    emitter._schedule_retry('radar', emitter._check_radar, 120)
    emitter._schedule_retry('radar', emitter._check_radar, 120)
    assert len(emitter._retries) == 1
    emitter._check_radar(); emitter._check_radar()
    assert emitter._inflight == {'radar'}
    emitter.stop(); assert not clock.events and not emitter._retries


def test_legend_fidelity_real_universal_blue_tile():
    """Online integration: exact RGBA stops must occur in ONE real fetched tile.

    A global 512px tile captures the whole intensity range more reliably than
    a dry station crop. Skip transport outages only, never a palette mismatch.
    """
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    def get(url):
        request = urllib.request.Request(url, headers={'User-Agent': 'WeatherFlow-PiConsole-almanac'})
        with urllib.request.urlopen(request, timeout=25, context=context) as response:
            return response.read()
    try:
        manifest = json.loads(get(ae.RADAR_MANIFEST_URL))
        path = manifest['radar']['past'][-1]['path']
        raw = get(f"{manifest['host']}{path}/512/0/0/0/2/1_1.png")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        pytest.skip(f'RainViewer offline: {error}')
    with Image.open(io.BytesIO(raw)) as tile:
        colors = {rgba for count, rgba in tile.convert('RGBA').getcolors(512 * 512)}
    for dbz, hexa, label in ae._RADAR_LEGEND['rain']:
        assert tuple(bytes.fromhex(hexa[1:])) in colors, f'{dbz} dBZ {hexa} absent from real tile'


def test_cold_start_rate_limit_covers_all_frames(make_emitter, radar_net, monkeypatch, radar_viewed, radar_dir):
    clock = [0.0]
    starts = []
    original_fetch = urllib.request.urlopen
    def fetch(*args, **kwargs):
        if args[0].full_url != ae.RADAR_MANIFEST_URL:
            starts.append(clock[0])
        return original_fetch(*args, **kwargs)
    monkeypatch.setattr(ae.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(ae.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    radar_net['times'] = [1800000000 + i * 600 for i in range(13)]
    emitter = make_emitter(); emitter._do_radar()
    assert len(emitter._radar_frames) == 13
    assert all(f['complete'] for f in emitter._radar_frames)
    assert len(list(radar_dir.glob('*.png'))) == 13
    assert len(radar_net['calls']) == 118
    assert len(starts) == 13 * len(ae._radar_viewport(47.61, -122.33, 7, 480)[0])
    assert all(sum(t <= v < t + 60 for v in starts) <= 90 for t in starts)
