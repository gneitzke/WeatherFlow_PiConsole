""" /health used to answer "ok" for any wx.json the engine had touched recently,
which is exactly the state a dead sensor produces. These tests drive the real
server over loopback and pin the three distinct verdicts - engine stalled,
sensor silent, engine down - and the render counter the watchdog trusts. """

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVE = Path(__file__).parents[1] / 'design/almanac/kiosk/serve.py'


def _load_serve(monkeypatch, tmp_path, data, **env):
    """ Import serve.py fresh with WFP_* pointing at a temp tree. The module
    reads its config at import time, so each test gets its own instance. """
    data_path = tmp_path / 'wx.json'
    if data is not None:
        data_path.write_text(json.dumps(data))
    monkeypatch.setenv('WFP_DATA', str(data_path))
    monkeypatch.setenv('WFP_WEB', str(tmp_path))
    monkeypatch.setenv('WFP_PORT', '0')
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(f'serve_{id(tmp_path)}', SERVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def serve_at(monkeypatch, tmp_path):
    """ A running server on an ephemeral loopback port; yields (module, url). """
    started = []

    def _start(data, **env):
        module = _load_serve(monkeypatch, tmp_path, data, **env)
        server = module.Server(('127.0.0.1', 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return module, f'http://127.0.0.1:{server.server_address[1]}'

    yield _start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _payload(**overrides):
    body = {'ts': int(time.time()), 'obsAgeSec': 30, 'station': 'Test', 'temp': 64.0}
    body.update(overrides)
    return body


def test_fresh_engine_and_fresh_observation_is_ok(serve_at):
    _, url = serve_at(_payload())
    status, health = _get(url + '/health')
    assert (status, health['status']) == (200, 'ok')
    assert 'reason' not in health


def test_fresh_file_with_a_silent_sensor_is_degraded_not_ok(serve_at):
    """ The reproduced defect: an hour-old reading republished every 2 s. """
    _, url = serve_at(_payload(obsAgeSec=3600))
    status, health = _get(url + '/health')
    assert status == 503
    assert health['status'] == 'degraded'
    assert health['reason'] == 'sensor silent'
    assert health['obsAgeSec'] >= 3600
    assert health['dataAgeSec'] < 5                 # the engine itself is fine


def test_stalled_engine_reads_stale_not_degraded(serve_at):
    """ Order matters: a stale file makes its observation look old too, but the
    engine is the fault worth naming. """
    _, url = serve_at(_payload(ts=int(time.time()) - 600, obsAgeSec=3600))
    status, health = _get(url + '/health')
    assert (status, health['status'], health['reason']) == (503, 'stale', 'engine stalled')


def test_health_ages_the_observation_by_the_files_own_age(serve_at):
    """ obsAgeSec was measured when the file was written; on a file that has
    stopped moving it has to keep counting. """
    _, url = serve_at(_payload(ts=int(time.time()) - 15, obsAgeSec=30),
                      WFP_STALE_SEC='60')
    _, health = _get(url + '/health')
    assert 44 <= health['obsAgeSec'] <= 47


def test_missing_wx_json_is_an_error(serve_at, tmp_path):
    _, url = serve_at(None)
    status, health = _get(url + '/health')
    assert (status, health['status']) == (503, 'error')
    assert health['reason']


def test_payload_without_obs_age_still_reports_ok(serve_at):
    """ An engine that predates obsAgeSec must not be judged silent forever. """
    body = _payload()
    del body['obsAgeSec']
    _, url = serve_at(body)
    status, health = _get(url + '/health')
    assert (status, health['status'], health['obsAgeSec']) == (200, 'ok', None)


# ------------------------------------------------------------ render counter
def test_renders_count_confirmed_frames_not_requests(serve_at):
    module, url = serve_at(_payload())
    _get(url + '/wx.json')                     # a poll whose render has not happened yet
    _get(url + '/wx.json?_=1&r=1')             # the page confirming the previous frame
    _get(url + '/wx.json?_=2&r=1')
    _, health = _get(url + '/health')
    assert health['polls'] == 3
    assert health['renders'] == 2              # a request is not a render


def test_a_wedged_page_leaves_renders_flat_while_polls_climb(serve_at):
    """ The watchdog's whole premise: JS that fetches but never paints must not
    look healthy. """
    module, url = serve_at(_payload())
    before = _get(url + '/health')[1]['renders']
    for _ in range(5):
        _get(url + '/wx.json?_=x')             # no r=1: nothing reached the screen
    after = _get(url + '/health')[1]
    assert after['renders'] == before
    assert after['polls'] >= 5


def test_only_loopback_clients_can_credit_a_render(serve_at):
    module, _ = serve_at(_payload())
    assert '127.0.0.1' in module.LOOPBACK and '::1' in module.LOOPBACK
    # the credit is gated on the client address, not on the query string alone
    source = SERVE.read_text()
    assert 'self.client_address[0] in LOOPBACK' in source


def test_radar_view_marker_from_real_poll_preserves_counters(serve_at, tmp_path):
    module, url = serve_at(_payload())
    marker = tmp_path / 'radar_viewed'
    _get(url + '/wx.json?_=1&r=1')
    assert not marker.exists()
    before = time.time()
    assert _get(url + '/wx.json?_=2&r=1&view=radar')[0] == 200
    assert before <= float(marker.read_text()) <= time.time()
    assert not list(tmp_path.glob('radar_viewed.tmp.*'))
    previous = marker.read_text()
    _get(url + '/wx.json?_=3')
    assert marker.read_text() == previous
    assert (module._polls, module._renders) == (3, 2)


@pytest.mark.parametrize('address,query,viewed,renders', [
    ('127.0.0.1', 'r=1&view=radar', True, 1),
    ('::1', 'view=radar', True, 0),
    ('::ffff:127.0.0.1', 'r=1&view=radar', True, 1),
    ('192.168.1.2', 'r=1&view=radar', False, 0),
    ('127.0.0.1', 'r=1', False, 1),
    ('127.0.0.1', 'r=1&view=radar-extra', False, 1),
])
def test_view_marker_trusts_only_loopback(monkeypatch, tmp_path, address, query, viewed, renders):
    module = _load_serve(monkeypatch, tmp_path, _payload())
    # Exercise do_GET with a synthetic peer; the static response is orthogonal.
    served = []
    monkeypatch.setattr(module.http.server.SimpleHTTPRequestHandler, 'do_GET', lambda self: served.append(self.path))
    handler = object.__new__(module.Handler)
    handler.client_address = (address, 12345)
    handler.path = '/wx.json?_=1&' + query
    before = time.time()
    handler.do_GET()
    assert (tmp_path / 'radar_viewed').exists() == viewed
    if viewed:
        assert before <= float((tmp_path / 'radar_viewed').read_text()) <= time.time()
    assert (module._polls, module._renders) == (1, renders)
    assert served == [handler.path]


def test_marker_write_failure_does_not_break_polling(serve_at, tmp_path, monkeypatch):
    module, url = serve_at(_payload())
    def fail(*args):
        raise OSError('read-only marker')
    monkeypatch.setattr(module.os, 'replace', fail)
    assert _get(url + '/wx.json?r=1&view=radar')[0] == 200
    assert (module._polls, module._renders) == (1, 1)
    assert not list(tmp_path.glob('radar_viewed*'))
