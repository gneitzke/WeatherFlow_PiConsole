""" The REST seeds (today's averages/maxima, yesterday's rain) were fetched
only on the first websocket message; a failed first attempt (boot-time wifi)
left dashes until the next restart. _seed_due keeps the attempts alive,
rate-limited, while values are still missing. """

from types import SimpleNamespace

from lib import properties
from tests.fixtures.config import make_config


def test_seed_due_first_then_rate_limited(make_parser, monkeypatch):
    from lib import observation_parser as op
    app = SimpleNamespace(config=make_config(), CurrentConditions=SimpleNamespace(Obs=properties.Obs(), button_list=[]))
    parser = make_parser(app)
    now = [1_000_000.0]
    monkeypatch.setattr(op.time, 'time', lambda: now[0])
    parser.api_data['111'] = {'flagAPI': 1}
    assert parser._seed_due('111') is True            # first message: always
    parser.api_data['111'] = {'flagAPI': 0}
    assert parser._seed_due('111') is True            # first renewed attempt
    now[0] += 60
    assert parser._seed_due('111') is False           # a minute later: hold
    now[0] += 300
    assert parser._seed_due('111') is True            # five minutes on: try again
    assert parser._seed_due('111') is False           # and not twice in a row
    parser.api_data['222'] = {'flagAPI': 0}
    assert parser._seed_due('222') is True            # limiter is per device
