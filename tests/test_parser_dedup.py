""" WeatherFlow's websocket delivers every obs_st TWICE (same epoch, back to
back - measured live on the station). Anything the core integrates per message
(peak sun watt-hours, per-minute strike counts) doubles unless the duplicate
guard fires; upstream's guard compared the whole obs list to the epoch and so
never did. """

from types import SimpleNamespace

from lib import properties
from tests.fixtures.config import make_config


def _obs_st(epoch, radiation=500):
    # Tempest obs_st: [time, lull, avg, gust, dir, interval, pressure, temp, rh,
    #                  illum, uv, solar, rain, precip_type, strike_dist,
    #                  strike_count, battery, report_interval, local_day_rain,
    #                  rain_final, local_day_rain_final, precip_analysis]
    return {'type': 'obs_st', 'device_id': 111,
            'obs': [[epoch, 0.5, 1.0, 2.0, 180, 60, 1010.0, 18.0, 60, 20000, 2.0,
                     radiation, 0.0, 0, 0, 0, 2.6, 60, 0.0, 0.0, 0.0, 0]]}


def _app():
    cc = SimpleNamespace(Obs=properties.Obs(), switchPanel=lambda *a, **k: None, button_list=[])
    cfg = make_config(System={'nc_rain': '0', 'Timeout': '5', 'stats_endpoint': '0'},
                      Station={'SkyID': '', 'SkySN': '', 'InAirID': '', 'InAirSN': ''})
    return SimpleNamespace(config=cfg, CurrentConditions=cc), cc


def test_duplicate_obs_st_is_dropped(make_parser):
    app, cc = _app()
    parser = make_parser(app)
    calls = []
    parser.calc_derived_variables = lambda device, config, kind: calls.append(kind)
    parser.parse_obs_st(_obs_st(1_700_000_000), app.config)
    parser.parse_obs_st(_obs_st(1_700_000_000), app.config)      # the echo
    assert calls == ['obs_st']                                    # integrated once, not twice
    parser.parse_obs_st(_obs_st(1_700_000_060), app.config)      # a real new minute
    assert calls == ['obs_st', 'obs_st']
    assert parser.device_obs['obTime'][0] == 1_700_000_060
