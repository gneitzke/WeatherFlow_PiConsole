""" Canned WeatherFlow API payloads for the station-finder tests.

Three endpoints are reproduced in the shapes lib/station_finder consumes:

  * map/stations           -> a GeoJSON FeatureCollection (map_response)
  * stations/{id}          -> a per-station detail response (detail_* helpers)
  * observations/station/{id} -> a preflight response (obs_response / obs_private)

The detail helpers cover every device combination the classifier must
distinguish (ST, SK+AR, SK-only, AR-only, HB-only) plus the two-AR case where
the second is the outdoor one, and the error/unauthorized variants.

GeoJSON coordinates are [lon, lat] (note the order) exactly as the live API
returns them, so the tests can prove lib/station_finder honours it.
"""


# --- map/stations : GeoJSON features -----------------------------------------
def map_feature(station_id, name, lat, lon, devices=None, station_status=1):
    """ One GeoJSON Feature. `devices` is the properties.devices list; when None
        a single ST device stub is supplied so the feature is not skipped. """
    return {
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [lon, lat]},  # [lon, lat]!
        'properties': {
            'station_id':     station_id,
            'name':           name,
            'station_status': station_status,
            'devices':        [{'device_id': 1, 'device_type': 'ST'}] if devices is None else devices,
        },
    }


def map_response(features, status_code=0, status_message='SUCCESS'):
    return {
        'status': {'status_code': status_code, 'status_message': status_message},
        'type': 'FeatureCollection',
        'features': features,
    }


def map_unauthorized():
    return {'status': {'status_code': 401, 'status_message': 'UNAUTHORIZED'}}


# --- device stubs for the detail endpoint ------------------------------------
def _st(device_id=5001, serial='ST-00045678', agl=2.0):
    return {'device_id': device_id, 'device_type': 'ST', 'serial_number': serial,
            'device_meta': {'agl': agl, 'environment': 'outdoor'}}


def _sk(device_id=5002, serial='SK-00012345', agl=3.0):
    return {'device_id': device_id, 'device_type': 'SK', 'serial_number': serial,
            'device_meta': {'agl': agl, 'environment': 'outdoor'}}


def _ar(device_id=5003, serial='AR-00098765', agl=1.5, environment='outdoor'):
    meta = {'agl': agl}
    if environment is not None:
        meta['environment'] = environment
    return {'device_id': device_id, 'device_type': 'AR', 'serial_number': serial,
            'device_meta': meta}


def _hb(device_id=5009, serial='HB-00011122'):
    return {'device_id': device_id, 'device_type': 'HB', 'serial_number': serial,
            'device_meta': {}}


def _station_obj(station_id, name, devices, lat=47.61, lon=-122.33):
    return {
        'station_id':   station_id,
        'name':         name,
        'latitude':     lat,
        'longitude':    lon,
        'timezone':     'America/Los_Angeles',
        'station_meta': {'elevation': 100},
        'devices':      devices,
    }


def detail_response(station_obj, status_code=0, status_message='SUCCESS'):
    """ Full /stations/{id} response wrapping a single station object. """
    return {
        'status':   {'status_code': status_code, 'status_message': status_message},
        'stations': [station_obj],
    }


# Convenience detail builders keyed by the device combo under test
def detail_tempest(station_id=1001, name='Tempest Station'):
    return detail_response(_station_obj(station_id, name, [_st(), _hb()]))


def detail_tempest_plus(station_id=1002, name='Tempest + Sky + Air'):
    # ST present alongside SK and AR -> ST must still win
    return detail_response(_station_obj(station_id, name, [_hb(), _sk(), _ar(), _st()]))


def detail_sky_air(station_id=1003, name='Sky and Air Station'):
    return detail_response(_station_obj(station_id, name, [_sk(), _ar(), _hb()]))


def detail_sky_only(station_id=1004, name='Sky Only'):
    return detail_response(_station_obj(station_id, name, [_sk(), _hb()]))


def detail_air_only(station_id=1005, name='Air Only'):
    return detail_response(_station_obj(station_id, name, [_ar(), _hb()]))


def detail_hb_only(station_id=1006, name='Hub Only'):
    return detail_response(_station_obj(station_id, name, [_hb()]))


def detail_two_airs_second_outdoor(station_id=1007, name='Two Airs'):
    # First AR is indoor, second AR is outdoor -> the outdoor one must be chosen
    indoor  = _ar(device_id=6001, serial='AR-00000001', environment='indoor')
    outdoor = _ar(device_id=6002, serial='AR-00000002', environment='outdoor')
    return detail_response(_station_obj(station_id, name, [_sk(), indoor, outdoor]))


def detail_air_no_environment(station_id=1008, name='Air No Env'):
    # AR device_meta lacks the 'environment' key entirely
    return detail_response(_station_obj(station_id, name, [_sk(), _ar(environment=None)]))


def detail_sky_indoor_air_only(station_id=1009, name='Sky + Indoor Air'):
    # SK present but the only AR is explicitly indoor -> NOT sky_air eligible: an
    # indoor sensor must never be wired as the outdoor source.
    indoor = _ar(device_id=6003, serial='AR-00000003', environment='indoor')
    return detail_response(_station_obj(station_id, name, [_sk(), indoor, _hb()]))


def detail_sky_indoor_and_outdoor_air(station_id=1010, name='Sky + Indoor + Outdoor'):
    # SK with both an indoor and an outdoor AR -> the outdoor one must be chosen.
    indoor  = _ar(device_id=6004, serial='AR-00000004', environment='indoor')
    outdoor = _ar(device_id=6005, serial='AR-00000005', environment='outdoor')
    return detail_response(_station_obj(station_id, name, [_sk(), indoor, outdoor]))


# --- incomplete-detail variants (Fix 1: downstream-crash exposure) -----------
def detail_missing_timezone(station_id=1011, name='No Timezone'):
    """ Otherwise-valid Tempest station whose station object omits 'timezone'
        (which upstream dereferences unguarded). """
    station_obj = _station_obj(station_id, name, [_st(), _hb()])
    del station_obj['timezone']
    return detail_response(station_obj)


def detail_missing_elevation(station_id=1012, name='No Elevation'):
    """ Otherwise-valid Tempest station whose station_meta omits 'elevation'. """
    station_obj = _station_obj(station_id, name, [_st(), _hb()])
    station_obj['station_meta'] = {}
    return detail_response(station_obj)


def detail_device_missing_agl(station_id=1013, name='No AGL'):
    """ Otherwise-valid Tempest station whose selected ST device has a
        device_meta without 'agl' (the height lookup would KeyError). """
    st = {'device_id': 5001, 'device_type': 'ST', 'serial_number': 'ST-00045678',
          'device_meta': {'environment': 'outdoor'}}
    return detail_response(_station_obj(station_id, name, [st, _hb()]))


def detail_unauthorized():
    return {'status': {'status_code': 401, 'status_message': 'UNAUTHORIZED'}}


# --- observations/station/{id} : preflight ------------------------------------
def obs_response():
    return {
        'status': {'status_code': 0, 'status_message': 'SUCCESS'},
        'station_units': {
            'units_temp': 'c', 'units_wind': 'mps', 'units_precip': 'mm',
            'units_pressure': 'mb', 'units_distance': 'km',
            'units_direction': 'cardinal', 'units_other': 'metric',
        },
        'obs': [{}],
    }


def obs_private():
    # Station exists but is no longer sharing public observation data
    return {'status': {'status_code': 0, 'status_message': 'SUCCESS'}}
