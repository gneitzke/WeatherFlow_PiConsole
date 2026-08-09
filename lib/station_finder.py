""" Nearby public-station picker for the WeatherFlow PiConsole.

Users who do not own WeatherFlow hardware can still run the console against a
nearby PUBLIC Tempest or Sky/Air station shared by another user. This module
implements an optional, interactive picker that is offered ONLY during the
initial configuration wizard (lib/config.create), only for the Websocket + REST
connection type (CONNECTION == 1), and only at the StationID prompt.

The picker is wired in via a single small hook in lib/config.write_config_key.
On success it writes the chosen StationID and device IDs into the configuration
object and injects the module globals that the unmodified upstream code paths in
lib/config depend on (STATION, idx, TEMPEST, INDOORAIR), so serial-number /
height / lat-lon / timezone / units resolution all flow through existing code.

This module NEVER imports lib.config at module load time (that would be a
circular import); it lazily imports it only inside the two functions that touch
its globals. The WeatherFlow Personal Access Token is read from the config
object, sent only to swd.weatherflow.com over HTTPS, and is NEVER written to a
log message or printed.

Copyright (C) 2018-2025 Peter Davis (classic console) / almanac add-on.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""

# Import required modules
from kivy.logger import Logger
import requests
import math
import sys

# Define required constants
DEFAULT_TIMEOUT      = 20
RADIUS_STEPS_KM      = (10, 25, 50, 100)
MAX_CANDIDATES       = 10
MAX_DETAIL_LOOKUPS   = 20
MAX_RETRIES          = 3
EARTH_RADIUS_KM      = 6371.0088
KM_PER_DEG_LAT       = 111.32
KM_TO_MI             = 0.621371
MAP_URL_TEMPLATE     = 'https://swd.weatherflow.com/swd/rest/map/stations?token={}&lat_min={}&lat_max={}&lon_min={}&lon_max={}'
DETAIL_URL_TEMPLATE  = 'https://swd.weatherflow.com/swd/rest/stations/{}?token={}'
OBS_URL_TEMPLATE     = 'https://swd.weatherflow.com/swd/rest/observations/station/{}?token={}'

# Define module state
_offered = False
_active  = False
_detail_cache = {}


def reset():

    """ Resets the module state. Used by the automated test suite to isolate
        each test; harmless in production (called once per process at most).
    """

    global _offered, _active, _detail_cache
    _offered = False
    _active  = False
    _detail_cache = {}


def intercept(config, key, connection):

    """ Single entry point called from lib.config.write_config_key. Returns True
        when the picker has handled (and thereby suppresses) the current prompt.

    INPUTS
        config              Station configuration object (configparser)
        key                 Name of the key currently being written
        connection          Selected connection type (1, 2, 3 or None)

    OUTPUT
        handled             True if the caller should return immediately
    """

    global _offered, _active
    try:

        # The picker is only ever offered for Websocket + REST (CONNECTION == 1).
        # UDP modes (2, 3) and the update() path (connection is None) never see it.
        if connection != 1:
            return False

        # Once a station has been picked, silently suppress the four device-ID
        # prompts (values are already written). Keep this branch trivial so it
        # cannot raise.
        if _active and key in ('TempestID', 'SkyID', 'OutAirID', 'InAirID'):
            return True

        # Lazy import to avoid a circular import at module load time
        from lib import config as config_module

        if key == 'StationID' and not _offered:
            _offered = True
            print('    If you do not own WeatherFlow hardware, the console can use a')
            print('    nearby PUBLIC Tempest or Sky/Air station shared by another user')
            if not config_module.query_user('Would you like to search for a nearby public station?*', 'no'):
                return False
            if run_picker(config):
                _active = True
                return True
            print('  Falling back to manual station entry')
            return False

        return False

    except Exception as error:
        Logger.error('station_finder: ' + str(error))
        return False


def run_picker(config):

    """ Orchestrates the picker: reads the token, resolves a search centre, then
        walks outward through the radius steps offering usable public stations.

    OUTPUT
        success             True if a station was selected and applied
    """

    # Lazy import to avoid a circular import at module load time
    from lib import config as config_module

    # Read the WeatherFlow Personal Access Token (never logged)
    token = config['Keys']['WeatherFlow']
    if not token:
        print('  No WeatherFlow Access Token found; cannot search for public stations')
        return False

    # Determine request timeout
    if config.has_option('System', 'Timeout'):
        timeout = int(config['System']['Timeout'])
    else:
        timeout = DEFAULT_TIMEOUT

    # Resolve the search centre (lat, lon)
    center = get_search_center(config)
    if center is None:
        return False
    lat, lon = center

    # Walk outward through the radius steps
    radius_idx = 0
    while True:
        radius_km    = RADIUS_STEPS_KM[radius_idx]
        at_max_radius = (radius_idx == len(RADIUS_STEPS_KM) - 1)

        # Fetch nearby stations, handling authorization and transient errors
        auth_retries = 0
        call_retries = 0
        stations     = None
        aborted      = False
        while True:
            stations, err = fetch_nearby_stations(token, lat, lon, radius_km, timeout)
            if err is None:
                break
            if err == 'unauthorized':
                if auth_retries >= MAX_RETRIES:
                    aborted = True
                    break
                token = input('    Access not authorized. Please re-enter your WeatherFlow Personal Access Token*: ')
                config.set('Keys', 'WeatherFlow', str(token))
                auth_retries += 1
                continue
            # err in ('network', 'invalid') -> retry the same call up to MAX_RETRIES
            call_retries += 1
            if call_retries >= MAX_RETRIES:
                print('  Unable to reach the WeatherFlow API. Please check your connection and try again')
                aborted = True
                break
            print('  Unable to reach the WeatherFlow API, retrying...')
            continue
        if aborted:
            return False

        # Build the eligible candidate list from the returned stations
        candidates = build_candidates(stations, token, timeout)

        # No usable candidates at this radius: widen or give up
        if not candidates:
            if not at_max_radius:
                next_km = RADIUS_STEPS_KM[radius_idx + 1]
                print('  No usable public stations within {} km. Widening search to {} km'.format(radius_km, next_km))
                radius_idx += 1
                continue
            print('  No public stations found within 100 km of your location')
            return False

        # Present the list and act on the user's choice
        widen = False
        while True:
            choice = present_and_choose(candidates, center, radius_km, at_max_radius)
            if choice is None:
                return False
            if choice == 'widen':
                radius_idx += 1
                widen = True
                break
            # A candidate was chosen; verify it is still sharing public data
            if preflight_station(choice, token, timeout):
                apply_selection(config, choice)
                return True
            print("  Station '{}' is not currently sharing public data. Please choose another station".format(choice['name']))
            candidates.remove(choice)
            if not candidates:
                if not at_max_radius:
                    next_km = RADIUS_STEPS_KM[radius_idx + 1]
                    print('  No usable public stations within {} km. Widening search to {} km'.format(radius_km, next_km))
                    radius_idx += 1
                    widen = True
                    break
                print('  No public stations found within 100 km of your location')
                return False
        if widen:
            continue


def get_search_center(config):

    """ Resolves the (lat, lon) centre for the search. Offers the configured
        station location if one is already present and valid, otherwise prompts.

    OUTPUT
        (lat, lon)          Tuple of floats, or None if the user cancels
    """

    # Lazy import to avoid a circular import at module load time
    from lib import config as config_module

    # Offer the configured station location if present and valid
    has_lat = config.has_option('Station', 'Latitude')  and config['Station']['Latitude']
    has_lon = config.has_option('Station', 'Longitude') and config['Station']['Longitude']
    if has_lat and has_lon:
        try:
            lat = float(config['Station']['Latitude'])
            lon = float(config['Station']['Longitude'])
            valid = (-90.0 <= lat <= 90.0) and (-180.0 <= lon <= 180.0)
        except ValueError:
            valid = False
        if valid:
            question = 'Use your configured station location ({}, {})?'.format(lat, lon)
            if config_module.query_user(question, 'yes'):
                return (lat, lon)

    # Otherwise prompt for latitude then longitude
    lat = _prompt_coordinate('Latitude', -90.0, 90.0)
    if lat is None:
        return None
    lon = _prompt_coordinate('Longitude', -180.0, 180.0)
    if lon is None:
        return None
    return (lat, lon)


def _prompt_coordinate(kind, low, high):

    """ Prompts for a single coordinate value. Returns a float in [low, high],
        or None if the user enters q/Q to cancel.
    """

    while True:
        value = input('  Please enter your {} (or q to cancel): '.format(kind.lower()))
        value = value.strip()
        if value.lower() == 'q':
            return None
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            print('    {} format is not valid. Please try again'.format(kind))
            continue
        if not (low <= number <= high):
            print('    {} must be between {} and {}. Please try again'.format(kind, int(low), int(high)))
            continue
        return number


def bounding_box(lat, lon, radius_km):

    """ Returns a (lat_min, lat_max, lon_min, lon_max) bounding box that
        comfortably contains a circle of radius_km around (lat, lon). Pure.
    """

    dlat = radius_km / KM_PER_DEG_LAT
    dlon = radius_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    lat_min = max(lat - dlat, -90.0)
    lat_max = min(lat + dlat, 90.0)
    lon_min = max(lon - dlon, -180.0)
    lon_max = min(lon + dlon, 180.0)
    return (lat_min, lat_max, lon_min, lon_max)


def haversine_km(lat1, lon1, lat2, lon2):

    """ Great-circle distance between two points in kilometres. Pure. """

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def fetch_nearby_stations(token, lat, lon, radius_km, timeout):

    """ Fetches public stations within the bounding box around (lat, lon) from
        the WeatherFlow map endpoint.

    OUTPUT
        (stations, error)   stations is a distance-sorted list of dicts on
                            success (possibly empty), or None on failure.
                            error is one of None, 'network', 'unauthorized',
                            'invalid'.
    """

    lat_min, lat_max, lon_min, lon_max = bounding_box(lat, lon, radius_km)
    url = MAP_URL_TEMPLATE.format(token, lat_min, lat_max, lon_min, lon_max)

    try:
        resp = requests.get(url, timeout=timeout)
    except Exception:
        return None, 'network'

    if resp.status_code == 401:
        return None, 'unauthorized'

    try:
        js = resp.json()
    except Exception:
        return None, 'invalid'
    if not resp.ok:
        return None, 'invalid'

    status = js.get('status', {})
    message = str(status.get('status_message', ''))
    if 'UNAUTHORIZED' in message:
        return None, 'unauthorized'
    if not (status.get('status_code') == 0 or 'SUCCESS' in message):
        return None, 'invalid'

    # Map GeoJSON features to plain dicts, defensively skipping malformed ones
    stations = []
    for feature in js.get('features', []):
        try:
            props = feature.get('properties', {})
            if props.get('station_status') != 1:
                continue
            geometry = feature.get('geometry')
            if not geometry or geometry.get('type') != 'Point':
                continue
            coordinates = geometry.get('coordinates')
            if not coordinates or len(coordinates) < 2:
                continue
            # GeoJSON coordinates are [lon, lat]
            station_lon = float(coordinates[0])
            station_lat = float(coordinates[1])
            devices = props.get('devices')
            if not devices:
                continue
            stations.append({
                'station_id':  props.get('station_id'),
                'name':        props.get('name') or props.get('station_name') or '',
                'latitude':    station_lat,
                'longitude':   station_lon,
                'device_ids':  devices,
                'distance_km': haversine_km(lat, lon, station_lat, station_lon),
            })
        except Exception:
            continue

    stations.sort(key=lambda s: s['distance_km'])
    return stations, None


def fetch_station_detail(station_id, token, timeout):

    """ Fetches the full /stations/{id} response for a single station, with a
        module-level cache (successes AND failures are cached).

    OUTPUT
        detail              The FULL parsed response dict (injected verbatim as
                            lib.config.STATION), or None on failure.
    """

    if station_id in _detail_cache:
        return _detail_cache[station_id]

    url = DETAIL_URL_TEMPLATE.format(station_id, token)
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception:
        _detail_cache[station_id] = None
        return None

    result = None
    try:
        if resp.ok:
            js = resp.json()
            status = js.get('status', {})
            # 'status'/'SUCCESS' is load-bearing: the observation loop in
            # lib/config checks 'status' in STATION, so the injected dict must
            # carry it.
            if (js.get('stations')
                    and 'status' in js
                    and 'SUCCESS' in str(status.get('status_message', ''))):
                result = js
    except Exception:
        result = None

    _detail_cache[station_id] = result
    return result


def classify_devices(station):

    """ Determines the hardware mode of a single station (the object at
        detail['stations'][0]). Pure.

    OUTPUT
        {'mode': 'tempest'|'sky_air'|None, 'devices': {'ST':.., 'SK':.., 'AR_out':..}}
    """

    st = None
    sk = None
    ars = []
    for device in station.get('devices', []):
        device_type = device.get('device_type')
        if not device_type:
            continue
        # Always ignore the hub
        if device_type == 'HB':
            continue
        if device_type == 'ST' and st is None:
            st = device
        elif device_type == 'SK' and sk is None:
            sk = device
        elif device_type == 'AR':
            ars.append(device)

    # Prefer the first outdoor AIR, else the first AIR
    out_air = None
    for air in ars:
        meta = air.get('device_meta', {}) or {}
        if meta.get('environment') == 'outdoor':
            out_air = air
            break
    if out_air is None and ars:
        out_air = ars[0]

    # ST wins even if SK/AR are also present
    if st is not None:
        mode = 'tempest'
    elif sk is not None and out_air is not None:
        mode = 'sky_air'
    else:
        mode = None

    return {'mode': mode, 'devices': {'ST': st, 'SK': sk, 'AR_out': out_air}}


def build_candidates(stations, token, timeout):

    """ Walks the distance-sorted station list, fetching detail and classifying
        each, keeping only eligible (Tempest or Sky+Air) stations.

    OUTPUT
        candidates          List of candidate dicts, at most MAX_CANDIDATES long
    """

    candidates = []
    fresh_lookups = 0
    print('  Checking nearby stations ', end='', flush=True)
    for station in stations:
        if len(candidates) >= MAX_CANDIDATES:
            break
        if fresh_lookups >= MAX_DETAIL_LOOKUPS:
            break

        station_id = station['station_id']
        was_cached = station_id in _detail_cache
        detail = fetch_station_detail(station_id, token, timeout)
        if not was_cached:
            fresh_lookups += 1
            print('.', end='', flush=True)

        if detail is None:
            Logger.warning('station_finder: station detail lookup failed; skipping')
            continue

        station_obj = detail['stations'][0]
        classification = classify_devices(station_obj)
        if classification['mode'] is None:
            continue

        name = (station.get('name')
                or station_obj.get('name')
                or station_obj.get('station_name')
                or 'Unknown')
        candidates.append({
            'station_id':  station_id,
            'name':        name,
            'distance_km': station['distance_km'],
            'distance_mi': station['distance_km'] * KM_TO_MI,
            'mode':        classification['mode'],
            'devices':     classification['devices'],
            'detail':      detail,
        })
    print('')
    return candidates


def preflight_station(candidate, token, timeout):

    """ Verifies, after the user picks a station but before applying it, that the
        station is currently sharing public observations.

    OUTPUT
        ok                  True if the station is usable
    """

    url = OBS_URL_TEMPLATE.format(candidate['station_id'], token)
    try:
        resp = requests.get(url, timeout=timeout)
    except Exception:
        return False
    try:
        if not resp.ok:
            return False
        js = resp.json()
    except Exception:
        return False
    status = js.get('status', {})
    if 'SUCCESS' not in str(status.get('status_message', '')):
        return False
    if 'station_units' not in js:
        return False
    return True


def present_and_choose(candidates, center, radius_km, at_max_radius):

    """ Prints the numbered candidate list and reads the user's selection.

    OUTPUT
        candidate           The chosen candidate dict, or the string 'widen',
                            or None if the user cancels
    """

    for index, candidate in enumerate(candidates, 1):
        name = candidate['name'][:28].ljust(28)
        distance = '{:5.1f} km / {:5.1f} mi'.format(candidate['distance_km'], candidate['distance_mi'])
        if candidate['mode'] == 'tempest':
            device = candidate['devices']['ST'] or {}
            summary = 'TEMPEST ({})'.format(device.get('serial_number', ''))
        else:
            summary = 'SKY + AIR'
        print('  {}) {}  {}  {}'.format(index, name, distance, summary))

    while True:
        choice = input('    > ').strip()
        lowered = choice.lower()
        if lowered == 'w':
            if at_max_radius:
                print('  Maximum search radius reached (100 km)')
                continue
            return 'widen'
        if lowered == 'q':
            return None
        try:
            number = int(choice)
            if 1 <= number <= len(candidates):
                return candidates[number - 1]
        except ValueError:
            pass
        print('  Selection not recognised. Please try again')


def apply_selection(config, candidate):

    """ Writes the chosen station and device IDs into the configuration object
        and injects the lib.config globals the downstream code paths depend on.

    This is the only function besides intercept() that touches lib.config.
    """

    # Lazy import to avoid a circular import at module load time
    from lib import config as config_module

    # Write StationID
    config.set('Station', 'StationID', str(candidate['station_id']))

    # Write device IDs, blanking the ones that do not apply
    mode = candidate['mode']
    devices = candidate['devices']
    if mode == 'tempest':
        config.set('Station', 'TempestID', str(devices['ST']['device_id']))
        config.set('Station', 'SkyID',    '')
        config.set('Station', 'OutAirID', '')
        config.set('Station', 'InAirID',  '')
    else:
        config.set('Station', 'SkyID',    str(devices['SK']['device_id']))
        config.set('Station', 'OutAirID', str(devices['AR_out']['device_id']))
        config.set('Station', 'TempestID', '')
        config.set('Station', 'InAirID',   '')

    # Inject the lib.config globals so upstream serial/height/lat-lon/units
    # resolution flows through unmodified code
    config_module.TEMPEST   = (mode == 'tempest')
    config_module.INDOORAIR = False
    config_module.STATION   = candidate['detail']
    config_module.idx       = 0

    # Print the applied summary and the public-station caveat
    print('  Selected station: {} (station ID {})'.format(candidate['name'], candidate['station_id']))
    print('  Adding station ID: {}'.format(candidate['station_id']))
    if mode == 'tempest':
        print('  Adding TEMPEST device ID: {}'.format(devices['ST']['device_id']))
    else:
        print('  Adding SKY device ID: {}'.format(devices['SK']['device_id']))
        print('  Adding outdoor AIR device ID: {}'.format(devices['AR_out']['device_id']))
    print('  Note: this station is owned by another user. The console will')
    print('  stop receiving data if the owner makes it private or offline')
