""" Canned WeatherFlow websocket messages, hand-authored from the exact fields
the parser reads (no sample data ships in the repo).

evt_strike: parse_evt_strike reads message['evt'] ([0]=epoch, [1]=distance km)
and message['device_id']; it dedups on evt[0]. That's all it needs.
"""

import time


def evt_strike(epoch=None, distance_km=5, energy=4200, device_id=111):
    return {
        'type': 'evt_strike',
        'device_id': device_id,
        'evt': [int(epoch if epoch is not None else time.time()), distance_km, energy],
    }
