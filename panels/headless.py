""" Headless data core for the console.

Runs the full weather-data pipeline (station status, astronomy, forecast, Sager,
websocket/UDP ingestion -> obsParser -> the six DictProperties) with NO on-screen
panels, so the console can act as a pure data engine feeding the almanac HTML
overlay's wx.json without paying the software-GL (llvmpipe) cost of rendering the
GUI on a headless display.

It subclasses CurrentConditions and inherits its __init__ verbatim, so the data
core stays byte-identical to the classic UI and future upstream changes to that
constructor flow here automatically. Only add_panels is overridden -> the Screen
is never populated with panels and is never added to the window, so nothing is
drawn. Same pattern as panels/almanac.py.

Copyright (C) 2018-2025 Peter Davis. GNU GPL v3 (see COPYING).
"""

# CurrentConditions lives in the running app's __main__ module (like the other
# panel classes), so import it from there rather than from main.py.
from __main__ import CurrentConditions


class HeadlessConditions(CurrentConditions):

    """ CurrentConditions with the presentation removed: same data core, no
    panels. """

    def add_panels(self, *args):
        # The only presentation step in the inherited __init__. Skip building the
        # panels/gauges entirely. Keep button_list defined and empty -- the
        # lightning auto-switch in lib/observation_parser iterates it unguarded.
        self.button_list = []
