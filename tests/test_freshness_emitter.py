""" A fresh wx.json used to be taken as proof of a live station. It never was:
the emit tick stamps `ts` whether or not the sensor said anything, and every
display value it copies is FORMATTED, so the epoch it was measured at is gone.
These tests pin the epochs that carry the truth instead - the observation's own
time, the strike event's time, and the age of each provider fetch. """

import time
from types import SimpleNamespace

from lib import properties
from tests.fixtures.config import make_config
from tests.fixtures import obs_scenarios as scn


def _obs_st(epoch, radiation=500):
    return {'type': 'obs_st', 'device_id': 111,
            'obs': [[epoch, 0.5, 1.0, 2.0, 180, 60, 1010.0, 18.0, 60, 20000, 2.0,
                     radiation, 0.0, 0, 0, 0, 2.6, 60, 0.0, 0.0, 0.0, 0]]}


def _obs_in_air(epoch):
    # obs_air: [time, pressure, temp, rh, strike_count, strike_dist, battery, interval]
    return {'type': 'obs_air', 'device_id': 222,
            'obs': [[epoch, 1010.0, 21.0, 45, 0, 0, 2.6, 60]]}


def _app():
    cc = SimpleNamespace(Obs=properties.Obs(), switchPanel=lambda *a, **k: None, button_list=[])
    # Connection=UDP silences derive.strike_delta_t's "no strike yet" log line,
    # which would otherwise reach system().log_time() and its live Kivy app.
    cfg = make_config(System={'nc_rain': '0', 'Timeout': '5', 'stats_endpoint': '0',
                              'Connection': 'UDP'},
                      Station={'SkyID': '', 'SkySN': '', 'InAirID': '', 'InAirSN': ''},
                      Display={'LightningPanel': '1', 'lightning_timeout': '30',
                               'TimeFormat': '24 hr'},
                      FeelsLike={'ExtremelyCold': '-5', 'FreezingCold': '0', 'VeryCold': '5',
                                 'Cold': '10', 'Mild': '15', 'Warm': '20', 'Hot': '25',
                                 'VeryHot': '30'})
    return SimpleNamespace(config=cfg, CurrentConditions=cc), cc


# ---------------------------------------------------------------- observation
def test_obs_age_is_measured_from_the_observation_not_the_emit_tick(make_emitter):
    """ The reproduced defect: an hour-old reading, republished every 2 s. """
    scenario = scn.strike_active()
    scenario['Obs']['obsTs'] = time.time() - 3600
    payload = make_emitter(scenario)._build_payload()

    assert abs(payload['ts'] - time.time()) < 5          # heartbeat is fresh ...
    assert 3595 <= payload['obsAgeSec'] <= 3605          # ... the observation is not
    assert payload['obsTs'] == int(scenario['Obs']['obsTs'])


def test_never_observed_ages_from_engine_start(make_emitter):
    # a station that has never reported is as silent as one that stopped: the
    # age counts from engine start (so a restart cannot reset a dead sensor to
    # healthy), while obsTs stays null to say "never"
    e = make_emitter(scn.strike_active())
    payload = e._build_payload()
    assert payload['obsTs'] is None
    assert 0 <= payload['obsAgeSec'] <= 2
    e._started_at -= 600
    assert e._build_payload()['obsAgeSec'] >= 600


def test_obs_age_never_goes_negative_on_a_fast_sensor_clock(make_emitter):
    scenario = scn.strike_active()
    scenario['Obs']['obsTs'] = time.time() + 120         # station clock ahead of ours
    assert make_emitter(scenario)._build_payload()['obsAgeSec'] == 0


# ------------------------------------------------------------------ lightning
def test_lightning_age_comes_from_the_strike_epoch_not_the_frozen_delta(make_emitter):
    """ StrikeDeltaT is computed once, when the strike is parsed, and then never
    again - so a payload built an hour later still claimed the bolt was seconds
    old and kept the masthead flag lit. """
    scenario = scn.strike_active()
    scenario['Obs']['StrikeDeltaT'] = ['0', 'minutes', '', '', 0.0]   # frozen at parse time
    scenario['Obs']['strikeTs'] = time.time() - 3600
    payload = make_emitter(scenario)._build_payload()

    assert 3595 <= payload['lightningSinceSec'] <= 3605
    assert payload['lightningActive'] is False           # outside the 30 min window
    assert payload['lightningLast'] == '1 hour ago'
    assert payload['lightningTs'] == int(scenario['Obs']['strikeTs'])


def test_recent_strike_epoch_keeps_the_lightning_panel_active(make_emitter):
    scenario = scn.strike_active()
    scenario['Obs']['strikeTs'] = time.time() - 300
    payload = make_emitter(scenario)._build_payload()
    assert payload['lightningActive'] is True
    assert payload['lightningLast'] == '5 minutes ago'


def test_lightning_falls_back_to_the_formatted_delta_without_an_epoch(make_emitter):
    scenario = scn.strike_active()                           # no strikeTs published
    payload = make_emitter(scenario)._build_payload()
    assert payload['lightningSinceSec'] == 720.0          # StrikeDeltaT[4]
    assert payload['lightningLast'] == '12 minutes ago'
    assert payload['lightningTs'] is None


# ------------------------------------------------------------------ providers
def test_provider_ages_report_the_last_successful_fetch(make_emitter):
    now = time.time()
    emitter = make_emitter(scn.strike_active())
    emitter._fc_ts     = now - 1800
    emitter._aqi_ts    = now - 420
    emitter._alerts_ts = now - 60
    payload = emitter._build_payload()

    assert 1795 <= payload['fcAgeSec'] <= 1805
    assert 415  <= payload['aqiAgeSec'] <= 425
    assert 55   <= payload['alertsAgeSec'] <= 65
    # A recent download of an old provider reading is still a recent download;
    # the stale flags stay off and the age is what disambiguates them.
    assert payload['aqiStale'] is False
    assert payload['alertsStale'] is False


def test_provider_ages_are_null_before_the_first_successful_fetch(make_emitter):
    payload = make_emitter(scn.strike_active())._build_payload()
    assert payload['fcAgeSec'] is None
    assert payload['aqiAgeSec'] is None
    assert payload['alertsAgeSec'] is None
    assert payload['aqiStale'] is True


# --------------------------------------------------------------- parser seam
def test_parser_publishes_the_observation_epoch_to_the_display_layer(make_parser):
    app, cc = _app()
    parser = make_parser(app)
    parser.parse_obs_st(_obs_st(1_700_000_060), app.config)
    assert cc.Obs['obsTs'] == 1_700_000_060


def test_indoor_air_does_not_refresh_the_outdoor_observation_epoch(make_parser):
    """ A live indoor Air must never make a dead Tempest look alive. """
    app, cc = _app()
    parser = make_parser(app)
    parser.parse_obs_st(_obs_st(1_700_000_060), app.config)
    parser.parse_obs_in_air(_obs_in_air(1_700_003_660), app.config)   # an hour later
    assert cc.Obs['obsTs'] == 1_700_000_060


def test_obs_st_reaches_wxjson_with_a_truthful_age(make_parser, make_emitter):
    """ End to end, offline: websocket -> parser -> Obs -> emitter -> payload. """
    app, cc = _app()
    parser = make_parser(app)
    parser.parse_obs_st(_obs_st(int(time.time()) - 900), app.config)

    payload = make_emitter({'Obs': cc.Obs}, config=app.config)._build_payload()
    assert 895 <= payload['obsAgeSec'] <= 910
