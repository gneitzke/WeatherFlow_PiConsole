""" A stand-in for Kivy's ConfigParser.

The pipeline reads config two ways: subscript (config['Units']['Temp'], used by
the parser and by _baro_series' st['TempestID']) and the two-arg .get(section,
option) that the emitter's _cfg() helper requires. A plain dict only supports the
first, so FakeConfig adds the two-arg get(). Station carries the four device-id
keys _baro_series subscripts and the lat/lon/timezone the emitter reads.
"""


class FakeConfig(dict):
    def get(self, section, option, *default):          # noqa: A003 - mirrors ConfigParser.get
        try:
            return self[section][option]
        except KeyError:
            if default:
                return default[0]
            raise


def make_config(**overrides):
    cfg = FakeConfig({
        'Station': {
            'Name': 'Test Station',
            'Latitude': '47.61', 'Longitude': '-122.33',
            'Timezone': 'America/Los_Angeles',
            'TempestID': '111', 'TempestSN': 'ST-00000111',
            'OutAirID': '', 'OutAirSN': '',
            'Elevation': '100', 'TempestHeight': '2', 'OutAirHeight': '2',
        },
        'Units': {
            'Temp': 'f', 'Pressure': 'inhg', 'Precip': 'in', 'Wind': 'mph',
            'Distance': 'miles', 'Direction': 'cardinal', 'Other': 'metric',
        },
        'Display': {'LightningPanel': '1', 'lightning_timeout': '30'},
        'System': {'Connection': 'Websocket', 'Version': 'v1.0', 'rest_api': '0'},
    })
    for section, kv in overrides.items():
        cfg.setdefault(section, {}).update(kv)
    return cfg
