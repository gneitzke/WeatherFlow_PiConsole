""" Canned display-layer state (Obs / Met / Astro / Sager) for emitter tests.

The emitter reads the FORMATTED dicts — the lists like ['64.0', '℉', ...]
that observation_format.format() produces — not raw websocket JSON. So we author
those directly. Each scenario starts from the real placeholder defaults in
lib/properties (so unmentioned keys look exactly like a fresh, dataless console)
and overrides only the fields the scenario is about.

Shapes that matter (from lib/almanac_emit._build_payload):
  outTemp      [value, unit-glyph]                 -> temp, tempUnit
  WindSpd      [spd, unit, force, force, descr]    -> windSpd, windStatus(descr, [4])
  WindDir      [deg, '', cardinal]                 -> windDir, windCardinal
  RainRate     [in/hr, unit, status, raw_mm]       -> rainRate([0]!), rainStatus([2]), rainRateMm([3])
  StrikeDist   [range_text, unit]                   -> lightningDist([0]), lightningDistNum(midpoint)
  StrikeDeltaT [n, unit, n2, unit2, epoch_sec]      -> lightningLast([0]+[1]), sinceSec([4])
  StrikesToday [count]                              -> lightningToday
"""

from lib import properties


def _base():
    return dict(Obs=properties.Obs(), Met=properties.Met(),
                Astro=properties.Astro(), Sager={'Forecast': 'Fair'})


def strike_active():
    """ A recent lightning strike, valid temp/wind. Drives the lightning block. """
    s = _base()
    s['Obs'].update({
        'outTemp':      ['64.0', '℉'],
        'WindSpd':      ['5.0', 'mph', '2', '2', 'Light Breeze'],
        'WindDir':      ['210', '', 'SSW'],
        # the core formats strike distance as a +/-3 km uncertainty RANGE,
        # never a bare number - see observation_format 'StrikeDistance'
        'StrikeDist':   ['2-8', 'miles'],
        'StrikeDeltaT': ['12', 'minutes', '', '', 720.0],   # [4]=720 s ago
        'StrikeFreq':   ['2.5', '/min'],
        'Strikes3hr':   ['11'],
        'StrikesToday': ['3'],
    })
    return s


def heavy_rain():
    """ Locks the rain-rate UNITS bug: rainRate must come from [0] (in/hr),
    NOT [3] (raw mm/hr). """
    s = _base()
    s['Obs'].update({
        'outTemp':   ['58.0', '℉'],
        'RainRate':  ['0.05', 'in/hr', 'Heavy Rain', 1.27],      # [0]=0.05 in/hr, [3]=1.27 mm/hr
        'TodayRain': ['1.2', 'in'],
    })
    return s


def calm_night():
    """ Dead calm — exercises the 'Calm Conditions' -> 'Calm' trim. """
    s = _base()
    s['Obs'].update({
        'outTemp': ['49.0', '℉'],
        'WindSpd': ['0.0', 'mph', '0', '0', 'Calm Conditions'],
        'WindDir': ['0', '', 'N'],
    })
    return s


def clear_day():
    """ Sunny midday: UV + solar radiation populated. """
    s = _base()
    s['Obs'].update({
        'outTemp':   ['72.0', '℉'],
        'Humidity':  ['41', '%'],
        'UVIndex':   ['6', '', 'High'],
        'Radiation': ['450', 'W/m2'],
        'WindSpd':   ['8.0', 'mph', '3', '3', 'Gentle Breeze'],
        'WindDir':   ['180', '', 'S'],
    })
    return s


def all_none():
    """ Fresh, dataless console: every Obs/Met/Astro value is a placeholder.
    Proves _build_payload never raises and emits all-null scalars. """
    return _base() | {'Sager': {}}
