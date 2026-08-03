""" Canned NWS alert `properties` dicts + an Open-Meteo hourly block, hand-authored
from the real api.weather.gov / air-quality payloads (the two Air Quality Alerts
that were live during development are reproduced verbatim in shape).

`expires` is set far in the future (2099) on active fixtures and far in the past
(2000) on the expired one, so _process_alerts' expiry filter is deterministic
regardless of when the suite runs.
"""

from datetime import timedelta


# --- the two real, simultaneous Air Quality Alerts (same event, different zones) ---
def air_quality_1():
    return {
        'event': 'Air Quality Alert',
        'severity': 'Unknown', 'urgency': 'Unknown', 'certainty': 'Unknown',
        'onset': '2026-08-03T13:43:00-07:00', 'ends': None,
        'expires': '2099-08-05T17:00:00-07:00',
        'areaDesc': 'King, WA; Kitsap, WA; Pierce, WA; Snohomish, WA; Thurston, WA',
        'senderName': 'NWS Seattle WA',
        'headline': 'Air Quality Alert issued August 3 at 1:43PM PDT by NWS Seattle WA',
        'description': 'An Air Quality Alert for wildfire smoke has been issued by the '
                       'following agencies: Puget Sound Clean Air Agency',
    }


def air_quality_2():
    return {
        'event': 'Air Quality Alert',
        'severity': 'Unknown', 'ends': None, 'expires': '2099-08-05T17:00:00-07:00',
        'onset': '2026-08-03T10:38:00-07:00',
        'areaDesc': 'Snohomish, WA; Skagit, WA',
        'senderName': 'NWS Seattle WA', 'headline': 'Air Quality Alert (earlier)',
        'description': 'Wildfire smoke.',
    }


def tornado_warning():
    return {'event': 'Tornado Warning', 'expires': '2099-01-01T00:00:00-07:00',
            'areaDesc': 'Pierce, WA', 'description': 'A Tornado Warning is in effect.'}


def flood_watch():
    return {'event': 'Flood Watch', 'expires': '2099-01-01T00:00:00-07:00',
            'areaDesc': 'King, WA', 'description': 'Flooding is possible.'}


def wind_advisory():
    return {'event': 'Wind Advisory', 'expires': '2099-01-01T00:00:00-07:00',
            'areaDesc': 'King, WA', 'description': 'Winds 30 mph.'}


def red_flag_warning():
    return {'event': 'Red Flag Warning', 'expires': '2099-01-01T00:00:00-07:00',
            'areaDesc': 'Kittitas, WA', 'description': 'Critical fire weather.'}


def special_statement():
    return {'event': 'Special Weather Statement', 'expires': '2099-01-01T00:00:00-07:00',
            'areaDesc': 'King, WA', 'description': 'Strong storms possible.'}


def expired_alert():
    return {'event': 'Flood Warning', 'expires': '2000-01-01T00:00:00-07:00',
            'areaDesc': 'King, WA', 'description': 'Old, already expired.'}


# --- Open-Meteo hourly blocks (naive local ISO times, as timezone=auto returns) ---
def _hourly(base_naive, aqis, pm):
    times = [(base_naive + timedelta(hours=i)).strftime('%Y-%m-%dT%H:00') for i in range(len(aqis))]
    return {'time': times, 'us_aqi': aqis, 'pm2_5': pm}


def hourly_rising(base_naive):
    """ 40 -> 55 over 6 h (Good climbing into Moderate) — today's real smoke curve. """
    return _hourly(base_naive, [40, 43, 46, 50, 52, 55], [13.8, 18.8, 24.1, 27.0, 26.9, 24.3])


def hourly_steady(base_naive):
    """ Flat in the low 40s — no band crossing, no trend. """
    return _hourly(base_naive, [41, 40, 42, 39, 41, 40], [10, 10, 11, 9, 10, 10])
