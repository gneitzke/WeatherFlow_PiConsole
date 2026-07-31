""" Defines the "almanac" alternate display layout for the Raspberry Pi Python
console for WeatherFlow Tempest and Smart Home Weather stations.

This is an additive, opt-in layout (enabled with [Display] LayoutStyle = almanac)
that FULLY REPLACES the classic six-panel CurrentConditions screen with a single
observatory-almanac composition: a Scotch-ruled masthead, a temperature hero, a
2x2 instrument grid (wind compass, aneroid barometer, sun & sky, rainfall) and a
footer tab bar.

Everything here lives in NEW files. The only edits to the upstream project are the
additive config flag (lib/config.py), the layout branch (main.py) and one #:include
line (wfpiconsole.kv). The classic path is untouched.

The instruments are drawn with Kivy canvas instructions and mirror the visual
target in design/almanac/console.html (paper theme). Colours are held in a named
theme table so a "night" theme can be added later without touching the widgets.

Copyright (C) 2018-2025 Peter Davis (classic console)
Almanac layout add-on.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
"""

# Load required Kivy modules
from kivy.uix.widget   import Widget
from kivy.core.text    import Label as CoreLabel
from kivy.factory      import Factory
from kivy.properties   import NumericProperty, StringProperty
from kivy.graphics     import (Color, Line, Ellipse, Rectangle, Quad,
                               PushMatrix, PopMatrix, Rotate)

import math
import os

# Additive JSON data-emitter for the HTML overlay (design/almanac/DATA_CONTRACT.md).
# NEW module; only used from this almanac-only screen, so the classic path
# never imports or runs it.
from lib.almanac_emit import AlmanacEmitter

# Import the classic screen we inherit all data plumbing from.
#
# main.py is the app entry point (launched as `python main.py`), so at runtime it
# lives in sys.modules as '__main__', NOT as an importable 'main' module. Pulling
# the base class from '__main__' references the already-running script's class
# object without re-executing the whole console. main.py performs its
# `from panels.almanac import ...` AFTER the CurrentConditions class is defined,
# so the name is guaranteed to be resolvable here.
from __main__ import CurrentConditions


# ==============================================================================
# THEME TABLE
# ==============================================================================
# One entry per theme. Only the "paper" theme is populated for this slice; the
# structure (named colour -> RGBA tuple) is what lets a "night" theme drop in
# later. Values mirror design/almanac/console.html.
def _hex(value, alpha=1.0):
    """ Convert an 'RRGGBB' hex string to an (r, g, b, a) float tuple. """
    value = value.lstrip('#')
    return (int(value[0:2], 16) / 255,
            int(value[2:4], 16) / 255,
            int(value[4:6], 16) / 255,
            alpha)


def safe_float(value, default=0.0):
    """ Parse a formatted-string Obs value (e.g. '206') into a float for the
    gauges, tolerating placeholders ('-', '--', '') and lists. Used by the KV
    bindings via `alm.safe_float(...)`. """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_text(value, joiner=''):
    """ Coerce ANY console data value into a display string for a Label's
    `text:`. The Obs / Met / Astro DictProperties hold a mix of plain strings
    (defaults and failure states, e.g. '--') and lists produced by
    observation.format() (e.g. ['0', 'mph'], and forecast temps/winds which
    arrive as numbers or None). A Kivy Label.text accepts ONLY str, so binding
    it to a raw list/number/None raises 'accept only str'. This normalises all
    of those:
        None            -> ''
        list / tuple    -> joiner.join(str(part) for each non-None part)
        anything else   -> str(value)
    Used by the KV bindings via `alm.as_text(...)`. """
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return joiner.join('' if part is None else str(part) for part in value)
    return str(value)


ALMANAC_THEMES = {
    'paper': {
        'paper':      _hex('F2EDE2'),
        'ink':        _hex('262218'),
        'ink_soft':   _hex('78705C'),
        'rule':       _hex('262218', 0.28),
        'rule_faint': _hex('262218', 0.14),
        'dot':        _hex('262218', 0.38),
        'accent':     _hex('AE3A27'),   # vermilion -> temperature / "now"
        'water':      _hex('3A5E77'),   # prussian  -> water & pressure
        'brass':      _hex('8F702A'),   # brass     -> sun & atmospheric energy
        'verdigris':  _hex('4C7263'),   # verdigris -> wind
        'accent_tint':    _hex('AE3A27', 0.10),
        'water_tint':     _hex('3A5E77', 0.07),
        'brass_tint':     _hex('8F702A', 0.10),
        'verdigris_tint': _hex('4C7263', 0.08),
    },
    # TODO(night-theme): populate from console.html :root[data-theme="night"].
}


# ==============================================================================
# FONT RESOLUTION
# ==============================================================================
# Reference a serif by absolute path (present on the Pi) with graceful fallback
# to Kivy's default font if the file is absent (e.g. a dev machine). Sans labels
# use the repo's bundled Inter.
def _first_existing(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


SERIF_BOLD = _first_existing([
    '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
    '/Library/Fonts/DejaVuSerif-Bold.ttf',
])
SERIF_REG = _first_existing([
    '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
    '/Library/Fonts/DejaVuSerif.ttf',
])
SANS_REG = _first_existing([
    'fonts/Inter-Regular.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
])
SANS_BOLD = _first_existing([
    'fonts/Inter-Bold.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
])


# ==============================================================================
# GAUGE BASE CLASS
# ==============================================================================
class AlmanacWidget(Widget):
    """ Base for the canvas instruments. Holds the active theme name and a
    colour lookup so subclasses draw against named pigments, not literals. """

    theme = StringProperty('paper')

    def color(self, name):
        return ALMANAC_THEMES.get(self.theme, ALMANAC_THEMES['paper'])[name]

    # 0 deg = up (north), increasing clockwise, in Kivy (y-up) screen space.
    @staticmethod
    def _pol(cx, cy, r, deg):
        a = math.radians(deg)
        return (cx + r * math.sin(a), cy + r * math.cos(a))

    def _text(self, text, cx, cy, font_size, color, font_name=SANS_REG,
              anchor='center'):
        """ Draw text centred (or edge-anchored) at (cx, cy). Text is rendered
        white/alpha by CoreLabel and tinted by the preceding Color(). """
        kwargs = {'text': str(text), 'font_size': font_size}
        if font_name:
            kwargs['font_name'] = font_name
        label = CoreLabel(**kwargs)
        label.refresh()
        tex = label.texture
        w, h = tex.size
        if anchor == 'center':
            x, y = cx - w / 2, cy - h / 2
        elif anchor == 'left':
            x, y = cx, cy - h / 2
        elif anchor == 'right':
            x, y = cx - w, cy - h / 2
        else:
            x, y = cx - w / 2, cy - h / 2
        Color(*color)
        Rectangle(texture=tex, pos=(x, y), size=(w, h))


# ==============================================================================
# WIND COMPASS
# ==============================================================================
class AlmanacCompass(AlmanacWidget):
    """ Compass rose with a verdigris needle pointing at the live wind bearing. """

    wind_dir_deg = NumericProperty(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw,
                  wind_dir_deg=self._draw, theme=self._draw)

    def _draw(self, *args):
        self.canvas.after.clear()
        cx, cy = self.center_x, self.center_y
        R = min(self.width, self.height) / 2 * 0.92
        if R <= 0:
            return
        with self.canvas.after:
            # Faint tinted face + ring
            Color(*self.color('verdigris_tint'))
            Ellipse(pos=(cx - R, cy - R), size=(2 * R, 2 * R))
            Color(*self.color('rule'))
            Line(circle=(cx, cy, R), width=1.0)
            # Tick ring
            for d in range(0, 360, 10):
                major = (d % 90 == 0)
                mid = (d % 45 == 0)
                r_in = R * (0.84 if major else 0.88 if mid else 0.92)
                Color(*(self.color('ink') if major else self.color('rule')))
                p1 = self._pol(cx, cy, r_in, d)
                p2 = self._pol(cx, cy, R, d)
                Line(points=[p1[0], p1[1], p2[0], p2[1]], width=1.0)
            # Cardinal letters
            for letter, deg in (('N', 0), ('E', 90), ('S', 180), ('W', 270)):
                lx, ly = self._pol(cx, cy, R * 0.68, deg)
                self._text(letter, lx, ly, R * 0.17, self.color('ink'),
                           font_name=SERIF_BOLD)
            # Needle (rest points up = north; rotate clockwise by bearing)
            PushMatrix()
            Rotate(angle=-self.wind_dir_deg, origin=(cx, cy), axis=(0, 0, 1))
            tip = R * 0.78
            tail = R * 0.22
            wq = R * 0.055
            Color(*self.color('verdigris'))
            Quad(points=[cx, cy + tip,
                         cx + wq, cy,
                         cx, cy - tail,
                         cx - wq, cy])
            PopMatrix()
            # Hub
            Color(*self.color('ink'))
            Ellipse(pos=(cx - R * 0.05, cy - R * 0.05),
                    size=(R * 0.1, R * 0.1))
            Color(*self.color('paper'))
            Ellipse(pos=(cx - R * 0.018, cy - R * 0.018),
                    size=(R * 0.036, R * 0.036))


# ==============================================================================
# ANEROID BAROMETER
# ==============================================================================
class AlmanacBarometer(AlmanacWidget):
    """ Aneroid dial, 980-1050 mb over 270 deg, prussian needle at live SLP. """

    slp = NumericProperty(1013)

    LOW = 980
    HIGH = 1050

    def _angle(self, value):
        # HTML convention: -135 deg at 980, sweeping +270 deg clockwise.
        value = max(self.LOW, min(self.HIGH, value))
        return -135 + (value - self.LOW) / (self.HIGH - self.LOW) * 270

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw,
                  slp=self._draw, theme=self._draw)

    def _draw(self, *args):
        self.canvas.after.clear()
        cx, cy = self.center_x, self.center_y
        R = min(self.width, self.height) / 2 * 0.92
        if R <= 0:
            return
        with self.canvas.after:
            Color(*self.color('water_tint'))
            Ellipse(pos=(cx - R, cy - R), size=(2 * R, 2 * R))
            Color(*self.color('rule'))
            Line(circle=(cx, cy, R), width=1.0)
            # Ticks + numbers every 10 mb, minor every 2 mb
            for v in range(self.LOW, self.HIGH + 1, 2):
                major = (v % 10 == 0)
                d = self._angle(v)
                r_in = R * (0.84 if major else 0.89)
                Color(*(self.color('ink') if major else self.color('rule')))
                p1 = self._pol(cx, cy, r_in, d)
                p2 = self._pol(cx, cy, R * 0.95, d)
                Line(points=[p1[0], p1[1], p2[0], p2[1]], width=1.0)
                if major:
                    nx, ny = self._pol(cx, cy, R * 0.70, d)
                    self._text(v, nx, ny, R * 0.11, self.color('ink_soft'))
            # Scale words
            for word, v in (('STORMY', 983), ('RAIN', 996), ('CHANGE', 1013),
                            ('FAIR', 1030), ('DRY', 1044)):
                wx, wy = self._pol(cx, cy, R * 0.48, self._angle(v))
                self._text(word, wx, wy, R * 0.085, self.color('ink_soft'))
            self._text('MILLIBARS', cx, cy - R * 0.75, R * 0.09,
                       self.color('ink_soft'))
            # Needle
            PushMatrix()
            Rotate(angle=-self._angle(self.slp), origin=(cx, cy), axis=(0, 0, 1))
            tip = R * 0.80
            tail = R * 0.20
            wq = R * 0.045
            Color(*self.color('water'))
            Quad(points=[cx, cy + tip,
                         cx + wq, cy,
                         cx, cy - tail,
                         cx - wq, cy])
            PopMatrix()
            Color(*self.color('ink'))
            Ellipse(pos=(cx - R * 0.05, cy - R * 0.05),
                    size=(R * 0.1, R * 0.1))
            Color(*self.color('paper'))
            Ellipse(pos=(cx - R * 0.018, cy - R * 0.018),
                    size=(R * 0.036, R * 0.036))


# ==============================================================================
# SUN ARC
# ==============================================================================
class AlmanacSunArc(AlmanacWidget):
    """ Horizon with the sun's daily arc. sun_frac (0=rise .. 1=set) places the
    brass sun dot. TODO(live): drive sun_frac from app.astro.sun_transit so the
    solid/dotted split tracks the real solar position. For now the arc is drawn
    solid with the sun near midday as a sensible static default. """

    sun_frac = NumericProperty(0.5)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw,
                  sun_frac=self._draw, theme=self._draw)

    def _draw(self, *args):
        self.canvas.after.clear()
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return
        pad = w * 0.06
        cx = self.x + w / 2
        base_y = self.y + h * 0.16          # horizon
        r = (w - 2 * pad) / 2
        r = min(r, h * 0.78)
        with self.canvas.after:
            # Arc (upper semicircle): Kivy ellipse angles clockwise from top.
            Color(*self.color('ink'))
            Line(ellipse=(cx - r, base_y - r, 2 * r, 2 * r, -90, 90),
                 width=1.2)
            # Horizon line
            Color(*self.color('rule'))
            Line(points=[self.x + pad * 0.5, base_y,
                         self.x + w - pad * 0.5, base_y], width=1.0)
            # Sun dot on the arc
            ang = -90 + self.sun_frac * 180
            sx, sy = self._pol(cx, base_y, r, ang)
            Color(*self.color('brass'))
            sr = max(3, w * 0.02)
            Ellipse(pos=(sx - sr, sy - sr), size=(2 * sr, 2 * sr))
            # Short rays
            for i in range(8):
                p1 = self._pol(sx, sy, sr * 1.5, i * 45)
                p2 = self._pol(sx, sy, sr * 2.3, i * 45)
                Line(points=[p1[0], p1[1], p2[0], p2[1]], width=1.0)


# ==============================================================================
# RAIN-RATE TUBE
# ==============================================================================
class AlmanacRainTube(AlmanacWidget):
    """ Vertical rain-rate tube, 0-1 in/hr, with a water-tint fill and a pointer
    at the live rate. """

    rain_rate = NumericProperty(0)
    MAX_RATE = 1.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw,
                  rain_rate=self._draw, theme=self._draw)

    def _draw(self, *args):
        self.canvas.after.clear()
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return
        tube_w = min(w * 0.34, 18)
        x1 = self.x + w * 0.12
        x2 = x1 + tube_w
        top = self.y + h * 0.90
        bot = self.y + h * 0.10
        span = top - bot
        with self.canvas.after:
            # Fill
            Color(*self.color('water_tint'))
            Rectangle(pos=(x1, bot), size=(tube_w, span))
            # Walls
            Color(*self.color('ink'))
            Line(points=[x1, top, x1, bot], width=1.0)
            Line(points=[x2, top, x2, bot], width=1.0)
            # Scale ticks 0..1 by 0.25
            labels = ['0', '1/4', '1/2', '3/4', '1']
            for i in range(5):
                y = bot + (i / 4) * span
                Color(*self.color('ink'))
                Line(points=[x2, y, x2 + tube_w * 0.35, y], width=1.0)
                self._text(labels[i], x2 + tube_w * 0.5, y, h * 0.055,
                           self.color('ink_soft'), anchor='left')
            # Pointer at current rate
            frac = max(0.0, min(1.0, self.rain_rate / self.MAX_RATE))
            py = bot + frac * span
            Color(*self.color('water'))
            Quad(points=[x1 - 2, py,
                         x1 - tube_w * 0.5, py + tube_w * 0.25,
                         x1 - tube_w * 0.55, py,
                         x1 - tube_w * 0.5, py - tube_w * 0.25])


# ==============================================================================
# TEMPERATURE SPARKLINE
# ==============================================================================
class AlmanacSparkline(AlmanacWidget):
    """ Temperature day-curve sparkline. TODO(live): bind to the day's observed
    temperature series (and forecast tail) once a history source is wired. For
    this slice a representative curve is drawn with a vermilion "now" dot at
    now_frac so the composition reads correctly. """

    now_frac = NumericProperty(0.4)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._draw, size=self._draw,
                  now_frac=self._draw, theme=self._draw)

    def _draw(self, *args):
        self.canvas.after.clear()
        w, h = self.width, self.height
        if w <= 0 or h <= 0:
            return
        base_y = self.y + h * 0.14
        with self.canvas.after:
            # Baseline + day ticks
            Color(*self.color('rule'))
            Line(points=[self.x, base_y, self.x + w, base_y], width=1.0)
            for i in range(5):
                tx = self.x + (i / 4) * w
                Line(points=[tx, base_y, tx, base_y - h * 0.06], width=1.0)
            # Representative diurnal curve (cool overnight -> warm afternoon)
            pts = []
            n = 48
            for i in range(n + 1):
                f = i / n
                # single-humped curve peaking mid-afternoon
                yv = 0.30 + 0.55 * math.sin(math.pi * min(1.0, f * 0.95 + 0.05))
                px = self.x + f * w
                py = base_y + yv * (h * 0.72)
                pts.extend([px, py])
            Color(*self.color('ink'))
            Line(points=pts, width=1.4)
            # Now dot
            idx = int(max(0, min(n, round(self.now_frac * n))))
            nx, ny = pts[idx * 2], pts[idx * 2 + 1]
            Color(*self.color('accent_tint'))
            hr = max(4, w * 0.02)
            Ellipse(pos=(nx - hr, ny - hr), size=(2 * hr, 2 * hr))
            Color(*self.color('accent'))
            dr = max(2.5, w * 0.011)
            Ellipse(pos=(nx - dr, ny - dr), size=(2 * dr, 2 * dr))


# ==============================================================================
# ALMANAC CONDITIONS SCREEN
# ==============================================================================
class AlmanacConditions(CurrentConditions):
    """ Full-screen almanac layout. Inherits ALL data plumbing from
    CurrentConditions (the inherited __init__ registers app.CurrentConditions,
    builds the Obs/Astro/Met/Sager/System DictProperties and schedules every
    update). Only add_panels is overridden so the classic six-panel grid is NOT
    built; the almanac widgets are declared in kvlang/almanac.kv instead. """

    def add_panels(self, *args):
        # No classic grid. Leave button_list empty so switchPanel / the lightning
        # auto-switch code (which iterates button_list) safely no-ops, and never
        # touch ids['row_layout'] / ids['panel_*'] which do not exist here.
        self.button_list = []

        # Build (or rebuild) the data-bound body. This runs from the inherited
        # __init__ AFTER app.CurrentConditions and the Obs/Astro/System
        # DictProperties are populated, so the body's KV bindings resolve
        # cleanly (the <AlmanacConditions> rule itself must stay data-free
        # because it is applied earlier, before those exist). Clearing first
        # keeps it idempotent if add_panels is re-invoked (e.g. PanelCount).
        if 'alm_container' in self.ids:
            self.ids['alm_container'].clear_widgets()
            self.ids['alm_container'].add_widget(Factory.AlmanacBody())

        # Start (or restart, if add_panels is re-invoked e.g. by a PanelCount
        # change) the JSON data-emitter for the HTML overlay. Additive/opt-in:
        # this only runs on the almanac screen, never on classic
        # CurrentConditions, since add_panels() is only overridden here.
        if not hasattr(self, 'almanac_emitter'):
            self.almanac_emitter = AlmanacEmitter(self)
        self.almanac_emitter.start()
