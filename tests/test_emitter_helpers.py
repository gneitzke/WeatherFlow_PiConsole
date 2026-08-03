""" Unit tests for the pure module-level coercion helpers in lib/almanac_emit.

These turn the console's formatted display values ('64.0', '--', 'Trace',
'[color=..]Rising[/color]', ...) into the clean numbers/strings the wx.json
contract promises. They must never raise on junk input.
"""

from lib import almanac_emit as ae


def test_num_trace_is_zero():
    assert ae._num('Trace') == 0.0


def test_num_k_suffix_scales_thousands():
    assert ae._num('1.2 k') == 1200.0


def test_num_placeholder_is_none():
    assert ae._num('--') is None
    assert ae._num('-----') is None


def test_num_nan_is_none():
    # protects the writer's json.dump(allow_nan=False)
    assert ae._num(float('nan')) is None


def test_num_bool_is_rejected():
    assert ae._num(True) is None


def test_idx_scalar_and_out_of_range():
    assert ae._idx('--', 1) is None          # bare scalar, only index 0 applies
    assert ae._idx('--', 0) == '--'
    assert ae._idx(['a', 'b'], 5) is None     # too short
    assert ae._idx(None, 0) is None


def test_text_strips_colour_markup():
    assert ae._text('[color=ff8837ff]Rising[/color]') == 'Rising'


def test_text_blanks_placeholder():
    assert ae._text('---') is None


def test_wind_desc_trims_conditions():
    assert ae._wind_desc('Calm Conditions') == 'Calm'
    assert ae._wind_desc('Light Breeze') == 'Light Breeze'


def test_temp_unit_normalises_glyph():
    assert ae._temp_unit('℉') == '°F'
    assert ae._temp_unit('℃') == '°C'


def test_cardinal_from_degrees():
    assert ae._cardinal_from_degrees(0) == 'N'
    assert ae._cardinal_from_degrees(90) == 'E'
    assert ae._cardinal_from_degrees(180) == 'S'
    assert ae._cardinal_from_degrees(270) == 'W'
    assert ae._cardinal_from_degrees(None) is None


def test_since_ago_text():
    assert ae._since_ago_text(['3', 'days', '', '', 260000.0]) == '3 days ago'
    assert ae._since_ago_text(['-', '-', '', '', 0]) is None


def test_lightning_active_window():
    from tests.fixtures.config import make_config
    cfg = make_config()   # lightning_timeout = 30 min -> 1800 s window
    assert ae.AlmanacEmitter._lightning_active(cfg, 720) is True
    assert ae.AlmanacEmitter._lightning_active(cfg, 3600) is False
    assert ae.AlmanacEmitter._lightning_active(cfg, None) is False
