""" Ingestion-side tests: a canned evt_strike websocket message driven through
the real obs_parser, offline. Covers the merge-critical headless invariant (the
empty button_list) AND an end-to-end proof that a strike reaches wx.json without
a real storm.

@mainthread is stubbed to identity (see conftest), so update_display — and its
evt_strike hook that reads CurrentConditions.button_list — runs synchronously.
"""

from types import SimpleNamespace

import pytest

from lib import properties
from tests.fixtures.config import make_config
from tests.fixtures.messages import evt_strike


def _fake_app(button_list=(), lightning_panel='1', with_lightning_panel_attr=False):
    """ Minimal app: config + a CurrentConditions holder. button_list=None omits
    the attribute entirely (to prove why the headless override must set it). """
    cc = SimpleNamespace(Obs=properties.Obs(), switchPanel=lambda *a, **k: None)
    if button_list is not None:
        cc.button_list = list(button_list)
    app = SimpleNamespace(
        config=make_config(Display={'LightningPanel': lightning_panel, 'lightning_timeout': '30'}),
        CurrentConditions=cc,
    )
    if with_lightning_panel_attr:
        app.LightningPanel = []
    return app, cc


def test_parse_populates_display_obs(make_parser):
    app, cc = _fake_app()
    parser = make_parser(app)
    parser.parse_evt_strike(evt_strike(distance_km=5), app.config)

    assert parser.display_obs['StrikeDist'] not in (None, '--', '-')
    # StrikeDeltaT formats to the [n, unit, ..., epoch_seconds] shape; [4] numeric
    assert isinstance(parser.display_obs['StrikeDeltaT'][4], (int, float))


def test_update_display_writes_currentconditions_obs(make_parser):
    app, cc = _fake_app()
    parser = make_parser(app)
    parser.parse_evt_strike(evt_strike(), app.config)
    # the strike fields propagated onto the shared Obs holder
    assert cc.Obs['StrikeDist'] == parser.display_obs['StrikeDist']
    assert cc.Obs['StrikeDeltaT'] == parser.display_obs['StrikeDeltaT']


def test_headless_empty_button_list_no_raise(make_parser):
    # The exact headless case: LightningPanel enabled, button_list == [].
    app, cc = _fake_app(button_list=[], lightning_panel='1')
    parser = make_parser(app)
    parser.parse_evt_strike(evt_strike(), app.config)   # must not raise


def test_missing_button_list_raises(make_parser):
    # Documents WHY HeadlessConditions.add_panels must assign button_list = [].
    app, cc = _fake_app(button_list=None, lightning_panel='1')
    parser = make_parser(app)
    with pytest.raises(AttributeError):
        parser.parse_evt_strike(evt_strike(), app.config)


def test_evt_strike_dedup(make_parser):
    app, cc = _fake_app()
    parser = make_parser(app)
    epoch = 1_700_000_500
    parser.parse_evt_strike(evt_strike(epoch=epoch), app.config)
    cc.Obs['StrikeDeltaT'] = 'SENTINEL'                 # mutate; a re-parse would overwrite
    parser.parse_evt_strike(evt_strike(epoch=epoch), app.config)   # same epoch -> dedup
    assert cc.Obs['StrikeDeltaT'] == 'SENTINEL'


def test_lightning_panel_disabled_is_safe(make_parser):
    # LightningPanel=0 skips the switch block; no app.LightningPanel attr either.
    app, cc = _fake_app(button_list=[], lightning_panel='0')
    parser = make_parser(app)
    parser.parse_evt_strike(evt_strike(distance_km=8), app.config)
    assert cc.Obs['StrikeDist'] not in (None, '--', '-')   # data still flowed


def test_evt_strike_reaches_wxjson(make_parser, make_emitter):
    # Crown test: websocket JSON -> parser -> Obs -> emitter -> wx.json, offline.
    app, cc = _fake_app(button_list=[])
    parser = make_parser(app)
    parser.parse_evt_strike(evt_strike(distance_km=5), app.config)   # strike "now"

    payload = make_emitter({'Obs': cc.Obs})._build_payload()
    assert payload['lightningActive'] is True
    assert payload['lightningDist'] not in (None, '')
    assert 0 <= payload['lightningSinceSec'] < 60
