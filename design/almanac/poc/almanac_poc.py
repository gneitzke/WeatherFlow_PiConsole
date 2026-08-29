#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
almanac_poc.py — throwaway Kivy fidelity test.

Reproduces the three hardest visual elements of the "paper" almanac console
mockup (design/almanac/console.html, ?theme=paper) purely on the Kivy canvas,
so we can eyeball whether Kivy can match the HTML's beauty. NOT integration.

  1. Serif temperature hero          (left third)
  2. Temperature day-curve sparkline (under the hero)
  3. Aneroid barometer dial          (right side)

Runs headless-ish on the Pi's live display (DISPLAY :0), grabs one screenshot,
then always terminates so it can never hang the console.

CRITICAL coordinate note: Kivy canvas is y-up / origin bottom-left, the OPPOSITE
of the SVG mockup (y-down / origin top-left). All of the mockup's pol()/ang()
math is re-derived for y-up here — see polk() and rot_cw() below.
"""

import os
import sys
import traceback

# ---- Config must be set BEFORE the window is imported -----------------------
import kivy
from kivy.config import Config
Config.set('graphics', 'width', '1024')
Config.set('graphics', 'height', '600')
Config.set('graphics', 'resizable', '0')
Config.set('graphics', 'borderless', '1')
Config.set('graphics', 'position', 'custom')
Config.set('graphics', 'left', '0')
Config.set('graphics', 'top', '0')
Config.set('input', 'mouse', 'mouse,disable_multitouch')

from math import radians, sin, cos, hypot

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.core.text import Label as CoreLabel
from kivy.graphics import Color, Rectangle, Line, Ellipse, Mesh
from kivy.graphics.texture import Texture
from kivy.uix.widget import Widget

# =============================================================================
# PAPER-THEME PIGMENTS  (from console.html :root tokens)
# =============================================================================
def hx(s, a=1.0):
    return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0,
            int(s[4:6], 16) / 255.0, a)

BG        = hx('F2EDE2')
INK       = hx('262218')
INK_SOFT  = hx('78705C')
RULE      = hx('262218', 0.28)
RULE_FT   = hx('262218', 0.14)
DOT       = hx('262218', 0.38)
VERMILION = hx('AE3A27')          # temperature / "now"
PRUSSIAN  = hx('3A5E77')          # water & pressure
BRASS     = hx('8F702A')          # sun

# Fonts available on the Pi (primary absolute paths). A fallback list keeps the
# POC from crashing if a face is missing (also lets it run off-Pi for review).
def _font(primary, *fallbacks):
    for p in (primary,) + fallbacks:
        if p and os.path.exists(p):
            return p
    return primary                     # let Kivy raise a clear error if truly absent

F_SERIF_B = _font('/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
                  '/System/Library/Fonts/Supplemental/Georgia Bold.ttf',
                  '/Library/Fonts/Georgia Bold.ttf')
F_SERIF   = _font('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
                  '/System/Library/Fonts/Supplemental/Georgia.ttf',
                  '/Library/Fonts/Georgia.ttf')
F_SANS    = _font('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                  os.path.join(os.path.dirname(kivy.__file__),
                               'data', 'fonts', 'DejaVuSans.ttf'))

W, H = 1024, 600
def T(yt):
    """Convert a top-down y (like the mockup) to Kivy's y-up coordinate."""
    return H - yt


# =============================================================================
# TEXT HELPERS  (CoreLabel -> white glyph texture, tinted at draw time)
# =============================================================================
_texcache = {}
def _tex(s, font, size):
    key = (s, font, round(size, 2))
    tex = _texcache.get(key)
    if tex is None:
        lbl = CoreLabel(text=s, font_name=font, font_size=size)
        lbl.refresh()
        tex = lbl.texture
        _texcache[key] = tex
    return tex

def text(canvas, s, x, y, font, size, color, anchor='left', valign='center'):
    """Draw text; x/y are Kivy coords. Returns (w, h)."""
    tex = _tex(s, font, size)
    w, h = tex.size
    px = x - w / 2 if anchor == 'center' else (x - w if anchor == 'right' else x)
    py = y - h / 2 if valign == 'center' else (y - h if valign == 'top' else y)
    with canvas:
        Color(1, 1, 1, 1)
        Rectangle(texture=tex, pos=(round(px), round(py)), size=(w, h))
        Color(*color)
        Rectangle(texture=tex, pos=(round(px), round(py)), size=(w, h))
    return w, h

def rich(canvas, segments, x, y, font, size, valign='center'):
    """Draw coloured runs left-to-right on one baseline.
    segments = [(str, color)] or [(str, color, font)] to override the face
    (arrow glyphs must use the sans face — the serif faces lack U+2191)."""
    cx = x
    for seg in segments:
        s, col = seg[0], seg[1]
        fnt = seg[2] if len(seg) > 2 else font
        w, _ = text(canvas, s, cx, y, fnt, size, col, anchor='left', valign=valign)
        cx += w
    return cx

def track(s, gap=' '):
    """Fake letter-spacing (DejaVu has no tracking) for masthead/dial words."""
    return gap.join(list(s))


# =============================================================================
# GEOMETRY HELPERS  (y-up conversions of the mockup's pol()/ang())
# =============================================================================
def polk(cx, cy, r, deg):
    """Kivy y-up polar: deg 0 = straight up, positive = clockwise on screen.
    Mockup SVG used [cx + r*sin, cy - r*cos]; y-up flips the cos term."""
    a = radians(deg)
    return (cx + r * sin(a), cy + r * cos(a))

def rot_cw(lx, ly, th):
    """Rotate a local point clockwise (screen sense) by th radians. Matches polk."""
    c, s = cos(th), sin(th)
    return (lx * c + ly * s, -lx * s + ly * c)

def cubic(p0, p1, p2, p3, n):
    out = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1]
        out.append((x, y))
    return out

def hline(canvas, x1, x2, y, color, width=1.0):
    with canvas:
        Color(*color)
        Rectangle(pos=(x1, y - width / 2.0), size=(x2 - x1, width))

def vline(canvas, x, y1, y2, color, width=1.0):
    with canvas:
        Color(*color)
        Rectangle(pos=(x - width / 2.0, y1), size=(width, y2 - y1))

def dots_between(canvas, x1, x2, y, color, step=4.0, r=0.7):
    x = x1
    with canvas:
        Color(*color)
        while x <= x2:
            Ellipse(pos=(x - r, y - r), size=(2 * r, 2 * r))
            x += step


# =============================================================================
# 1 + 2 : HERO + SPARKLINE  (left third)
# =============================================================================
HERO_X = 34
SPX = 34                       # sparkline left  (native 350 wide → fits 34..384)
SP_BASE = T(432)               # baseline (svg y=62) in Kivy
SVY = 1.0                      # vertical scale of the day-curve (native)

def sy2k(sy):
    return SP_BASE + (62 - sy) * SVY

def sxy2k(p):
    return (SPX + p[0], sy2k(p[1]))

def draw_sun_icon(canvas, cx, cy, r=8.2):
    with canvas:
        Color(*INK)
        Line(circle=(cx, cy, r), width=1.15)
        for i in range(12):
            deg = i * 30
            lg = (i % 2 == 0)
            p1 = polk(cx, cy, 11.5, deg)
            p2 = polk(cx, cy, 18.5 if lg else 15.5, deg)
            Line(points=[p1[0], p1[1], p2[0], p2[1]], width=1.1)

def draw_hero(canvas):
    # condition line
    draw_sun_icon(canvas, HERO_X + 18, T(103))
    text(canvas, 'Clear & Sunny', HERO_X + 42, T(97), F_SERIF, 23, INK)
    text(canvas, 'Clear until 02:00 tomorrow', HERO_X + 42, T(116),
         F_SANS, 11.5, INK_SOFT)

    # big serif temperature
    nw, nh = text(canvas, '64.0', HERO_X - 4, T(178), F_SERIF_B, 112, INK,
                  valign='center')
    text(canvas, '°F', HERO_X - 4 + nw + 6, T(178) + 34, F_SERIF, 30, INK)

    # feels-like
    rich(canvas, [('Feels like ', INK_SOFT), ('64°', INK),
                  ('  ·  Feeling warm', INK_SOFT)],
         HERO_X, T(252), F_SANS, 12.5)

    # rising trend (vermilion arrow + number)
    rich(canvas, [('↑ ', VERMILION, F_SANS), ('Rising ', INK), ('4.6°', VERMILION),
                  (' per hour', INK)],
         HERO_X, T(284), F_SERIF, 15)
    text(canvas, '4.9° warmer than this time yesterday',
         HERO_X, T(305), F_SANS, 11.5, INK_SOFT)

    # range labels row
    ry = T(346)
    rich(canvas, [(track('LOW') + '  ', INK_SOFT)], HERO_X, ry, F_SANS, 9.5)
    lw, _ = _tex(track('LOW') + '  ', F_SANS, 9.5).size
    text(canvas, '50°', HERO_X + lw, ry, F_SERIF, 15, INK)
    text(canvas, track('FORECAST TODAY'), SPX + 175, ry, F_SANS, 9.5,
         INK_SOFT, anchor='center')
    text(canvas, '82°', SPX + 350, ry, F_SERIF, 15, INK, anchor='right')
    hiw, _ = _tex('82°', F_SERIF, 15).size
    text(canvas, track('HIGH') + '  ', SPX + 350 - hiw - 4, ry, F_SANS, 9.5,
         INK_SOFT, anchor='right')

    draw_sparkline(canvas)

def draw_sparkline(canvas):
    # --- observed curve (three cubic segments) ---
    obs = (cubic((0, 52.7), (32, 54.5), (64, 58.2), (91.1, 59.6), 26) +
           cubic((91.1, 59.6), (99, 60), (107, 44.5), (117.7, 39.6), 16)[1:] +
           cubic((117.7, 39.6), (122, 37.8), (128, 40.3), (132.7, 40.7), 10)[1:])
    obs_k = [sxy2k(p) for p in obs]

    # --- soft vermilion gradient fill under observed portion ---
    top_span = (62 - 39.6) * SVY            # svg-bbox top of the fill
    grad = _grad_tex()
    verts, idx = [], []
    for i, (kx, ky) in enumerate(obs_k):
        v = (ky - SP_BASE) / top_span
        verts += [kx, ky, 0.5, max(0.0, min(1.0, v))]
        verts += [kx, SP_BASE, 0.5, 0.0]
        idx += [2 * i, 2 * i + 1]
    with canvas:
        Color(1, 1, 1, 1)
        Mesh(vertices=verts, indices=idx, mode='triangle_strip', texture=grad)

    # --- baseline hairline + 00/06/12/18/24h ticks ---
    hline(canvas, SPX, SPX + 350, SP_BASE, RULE, 1.0)
    for sx in (0.5, 87.5, 175, 262.5, 349.5):
        vline(canvas, SPX + sx, SP_BASE - 3.5, SP_BASE, RULE, 1.0)

    # --- "now" drop line ---
    nx = SPX + 132.7
    vline(canvas, nx, sy2k(62), sy2k(46), RULE_FT, 1.0)

    # --- dotted forecast curve ---
    fx = (cubic((132.7, 40.7), (152, 35), (205, 17.5), (240.6, 16.7), 90) +
          cubic((240.6, 16.7), (275, 17), (322, 29), (350, 36.7), 90)[1:])
    fx_k = [sxy2k(p) for p in fx]
    _dotted_path(canvas, fx_k, INK_SOFT, step=4.2, r=0.85)

    # --- observed ink curve (on top of fill) ---
    pts = []
    for (kx, ky) in obs_k:
        pts += [kx, ky]
    with canvas:
        Color(*INK)
        Line(points=pts, width=1.35, cap='round', joint='round')

    # --- open-circle markers at 49.8° / 64.8° ---
    for (sx, sy) in ((91.1, 59.6), (117.7, 39.6)):
        kx, ky = sxy2k((sx, sy))
        with canvas:
            Color(*BG)
            Ellipse(pos=(kx - 2.4, ky - 2.4), size=(4.8, 4.8))
            Color(*INK)
            Line(circle=(kx, ky, 2.4), width=1.15)

    # --- "now" halo + vermilion dot ---
    nxk, nyk = sxy2k((132.7, 40.7))
    with canvas:
        Color(VERMILION[0], VERMILION[1], VERMILION[2], 0.18)
        Ellipse(pos=(nxk - 6.5, nyk - 6.5), size=(13, 13))
        Color(*VERMILION)
        Ellipse(pos=(nxk - 3.2, nyk - 3.2), size=(6.4, 6.4))

    # --- tiny labels ---
    text(canvas, '49.8°  ·  06:15', SPX + 91.1, sy2k(72.5), F_SANS, 8.5,
         INK_SOFT, anchor='center')
    text(canvas, '64.8°  ·  08:04', SPX + 112, sy2k(30.5), F_SANS, 8.5,
         INK_SOFT, anchor='right')
    text(canvas, '82°', SPX + 240.6, sy2k(10.5), F_SANS, 8.5,
         INK_SOFT, anchor='center')

def _dotted_path(canvas, pts, color, step=4.0, r=0.8):
    acc = 0.0
    with canvas:
        Color(*color)
        Ellipse(pos=(pts[0][0] - r, pts[0][1] - r), size=(2 * r, 2 * r))
        for i in range(1, len(pts)):
            x0, y0 = pts[i - 1]
            x1, y1 = pts[i]
            d = hypot(x1 - x0, y1 - y0)
            acc += d
            while acc >= step and d > 0:
                acc -= step
                t = 1 - (acc / d) if d else 1
                # place at fractional position along this segment
                px = x0 + (x1 - x0) * min(1.0, max(0.0, t))
                py = y0 + (y1 - y0) * min(1.0, max(0.0, t))
                Ellipse(pos=(px - r, py - r), size=(2 * r, 2 * r))

_grad = None
def _grad_tex():
    global _grad
    if _grad is None:
        n = 64
        tex = Texture.create(size=(1, n), colorfmt='rgba')
        buf = bytearray()
        for i in range(n):                       # row 0 = v0 = baseline
            a = int(round(0.20 * 255 * (i / (n - 1))))
            buf += bytes((int(VERMILION[0] * 255), int(VERMILION[1] * 255),
                          int(VERMILION[2] * 255), a))
        tex.blit_buffer(bytes(buf), colorfmt='rgba', bufferfmt='ubyte')
        tex.wrap = 'clamp_to_edge'
        _grad = tex
    return _grad


# =============================================================================
# 3 : ANEROID BAROMETER DIAL  (right side)
#     mockup: cx=75 cy=78 r=66,  ang(v) = -135 + (v-980)/70*270,  0=up CW
# =============================================================================
BCX, BCY = 618, 300            # dial centre (Kivy)
S = 2.0                        # svg-internal unit -> Kivy px scale

def ang(v):
    return -135 + (v - 980) / 70.0 * 270.0

def bpol(r, deg):
    return polk(BCX, BCY, r * S, deg)

def draw_barometer(canvas):
    # panel header + hairline
    text(canvas, track('BAROMETER'), 448, T(96), F_SANS, 11, INK)
    text(canvas, track('RISING'), 984, T(96), F_SANS, 10, INK_SOFT, anchor='right')
    hline(canvas, 448, 984, T(110), RULE, 1.0)

    # faint tinted face + outer ring
    with canvas:
        Color(PRUSSIAN[0], PRUSSIAN[1], PRUSSIAN[2], 0.07)
        Ellipse(pos=(BCX - 66 * S, BCY - 66 * S), size=(132 * S, 132 * S))
        Color(*RULE)
        Line(circle=(BCX, BCY, 66 * S), width=1.3)

    # ticks every 2 mb (minor) / 10 mb (major) + numbers
    v = 980
    while v <= 1050:
        major = (v % 10 == 0)
        d = ang(v)
        p1 = bpol(56 if major else 59, d)
        p2 = bpol(63, d)
        with canvas:
            Color(*(INK if major else RULE))
            Line(points=[p1[0], p1[1], p2[0], p2[1]],
                 width=1.15 if major else 0.9)
        if major:
            nx, ny = bpol(47, d)
            text(canvas, str(v), nx, ny, F_SANS, 7 * S, INK_SOFT, anchor='center')
        v += 2

    # classic scale words
    for word, wv in (('STORMY', 983), ('RAIN', 996), ('CHANGE', 1013),
                     ('FAIR', 1030), ('DRY', 1044)):
        wx, wy = bpol(33, ang(wv))
        text(canvas, track(word, ' '), wx, wy, F_SANS, 6 * S,
             INK_SOFT, anchor='center')

    # MILLIBARS caption (bottom gap: svg y=132, cy=78 -> 54 below centre)
    text(canvas, track('MILLIBARS'), BCX, BCY - 54 * S, F_SANS, 6.5 * S,
         INK_SOFT, anchor='center')

    # faint set-hand parked at 24h low (1020.2)
    sx, sy = bpol(50, ang(1020.2))
    with canvas:
        Color(*INK_SOFT)
        Line(points=[BCX, BCY, sx, sy], width=1.4)

    # main prussian needle at 1022.1
    th = radians(ang(1022.1))
    local = [(0, 55), (2.6, 10), (1.2, -12), (-1.2, -12), (-2.6, 10)]
    verts, idx = [], []
    for i, (lx, ly) in enumerate(local):
        rx, ry = rot_cw(lx * S, ly * S, th)
        verts += [BCX + rx, BCY + ry, 0, 0]
        idx.append(i)
    with canvas:
        Color(*PRUSSIAN)
        Mesh(vertices=verts, indices=idx, mode='triangle_fan')

    # hub
    with canvas:
        Color(*INK)
        Ellipse(pos=(BCX - 4 * S, BCY - 4 * S), size=(8 * S, 8 * S))
        Color(*BG)
        Ellipse(pos=(BCX - 1.4 * S, BCY - 1.4 * S), size=(2.8 * S, 2.8 * S))

    # ---- headline + ledger beside the dial ----
    vx = 800
    nw, _ = text(canvas, '1022.1', vx, T(206), F_SERIF, 34, INK)
    text(canvas, 'mb', vx + nw + 8, T(206) - 4, F_SANS, 13, INK_SOFT)
    rich(canvas, [('↑ ', PRUSSIAN), ('Rising 0.4 mb/hr', INK)],
         vx, T(244), F_SANS, 13.5)

    hline(canvas, vx, 980, T(268), RULE_FT, 1.0)
    ledger_row(canvas, vx, 980, T(292), 'Outlook', 'Unchanged')
    ledger_row(canvas, vx, 980, T(316), '24h high', '1022.1  08:20')
    ledger_row(canvas, vx, 980, T(340), '24h low',  '1020.2  00:00')

def ledger_row(canvas, xL, xR, y, key, val):
    kw, _ = text(canvas, track(key.upper()), xL, y, F_SANS, 9.5, INK_SOFT)
    vw, _ = text(canvas, val, xR, y, F_SERIF, 13, INK, anchor='right')
    dots_between(canvas, xL + kw + 6, xR - vw - 6, y - 3, DOT, step=4.0, r=0.7)


# =============================================================================
# CHROME  (masthead scotch rule + hairlines + vertical rule)
# =============================================================================
def draw_chrome(canvas):
    hline(canvas, HERO_X, 990, T(13), INK, 3.0)          # scotch (thick)
    hline(canvas, HERO_X, 990, T(20), INK, 1.0)          # scotch (thin)
    text(canvas, track('FRI, 31 JUL 2026'), HERO_X, T(42), F_SERIF, 15, INK)
    text(canvas, track('SEATTLE', ' '), W / 2, T(41), F_SERIF, 20, INK,
         anchor='center')
    text(canvas, '09:06', 990, T(41), F_SERIF, 20, INK, anchor='right')
    hline(canvas, HERO_X, 990, T(60), RULE, 1.0)
    vline(canvas, 410, T(468), T(84), RULE, 1.0)         # hero divider


# =============================================================================
# APP
# =============================================================================
class AlmanacPOC(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.size = (W, H)
        self.size_hint = (None, None)
        # Paper ground: the Pi's VC4 sdl2/EGL backend does NOT reliably honour
        # Window.clearcolor, so draw an explicit full-window paper Rectangle in
        # canvas.before and keep it pinned to pos/size (never rely on clear).
        with self.canvas.before:
            Color(*BG)
            self._bg = Rectangle(pos=(0, 0), size=(W, H))
        self.bind(pos=self._sync_bg, size=self._sync_bg)
        draw_chrome(self.canvas)
        draw_hero(self.canvas)
        draw_barometer(self.canvas)

    def _sync_bg(self, *a):
        # Cover the whole window regardless of how the root widget is sized/placed
        self._bg.pos = (0, 0)
        self._bg.size = (max(W, self.width), max(H, self.height))


class AlmanacApp(App):
    def build(self):
        Window.size = (W, H)
        Window.clearcolor = BG
        return AlmanacPOC()

    def on_start(self):
        Clock.schedule_once(self._cap, 1.5)

    def _cap(self, *a):
        try:
            # Kivy may write /tmp/almanac_poc0001.png — print the ACTUAL path.
            path = Window.screenshot(name='/tmp/almanac_poc.png')
            print('SCREENSHOT WRITTEN:', path)
        except Exception:
            traceback.print_exc()
        sys.stdout.flush()                 # os._exit() skips buffer flushing
        sys.stderr.flush()
        Clock.schedule_once(lambda *_: os._exit(0), 9.0)  # stay up for external scrot capture


if __name__ == '__main__':
    try:
        AlmanacApp().run()
    except Exception:
        traceback.print_exc()
    finally:
        os._exit(0)
