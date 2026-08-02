""" One-time in-app notice, shown once in the classic GUI after the Almanac
layout becomes available (e.g. after an upgrade), offering to switch to it.
Gated by [Display] LayoutPrompt and only shown in classic layout — the headless
data engine and the almanac layout itself never call it.
Copyright (C) 2018-2025 Peter Davis. GNU GPL v3 (see COPYING).
"""

from kivy.uix.popup     import Popup
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label     import Label
from kivy.uix.button    import Button
from kivy.app           import App


def _dismiss_flag(app):
    """ Never show the notice again. """
    try:
        app.config.set('Display', 'LayoutPrompt', '0')
        app.config.write()
    except Exception:                                                     # noqa: BLE001
        pass


def maybe_show_almanac_prompt(*args):
    """ Show the notice once, only in classic layout. Safe to call always: it
    no-ops unless LayoutPrompt == '1' and LayoutStyle == 'classic'. """
    app = App.get_running_app()
    cfg = getattr(app, 'config', None)
    if cfg is None:
        return
    show = (cfg.has_option('Display', 'LayoutPrompt') and cfg.get('Display', 'LayoutPrompt') == '1')
    layout = cfg.get('Display', 'LayoutStyle') if cfg.has_option('Display', 'LayoutStyle') else 'classic'
    if not show:
        return
    if layout != 'classic':
        _dismiss_flag(app)
        return

    body = BoxLayout(orientation='vertical', spacing='12dp', padding='18dp')
    body.add_widget(Label(
        text=("A redesigned [b]Almanac[/b] layout is now available.\n\n"
              "Switch to it, or keep the classic six-panel view.\n"
              "You can change this any time via [Display] LayoutStyle in the config."),
        markup=True, halign='center', valign='middle'))
    row = BoxLayout(size_hint_y=None, height='46dp', spacing='12dp')
    popup = Popup(title='New Almanac layout available',
                  content=body, size_hint=(0.72, 0.5), auto_dismiss=False)

    def keep_classic(*_a):
        _dismiss_flag(app)
        popup.dismiss()

    def switch_almanac(*_a):
        try:
            app.config.set('Display', 'LayoutStyle', 'almanac')
            app.config.write()
        except Exception:                                                # noqa: BLE001
            pass
        _dismiss_flag(app)
        body.clear_widgets()
        body.add_widget(Label(
            text=("Almanac layout enabled.\n\n"
                  "[b]Restart the console[/b] to apply it\n"
                  "(wfpiconsole stop && wfpiconsole start)."),
            markup=True, halign='center', valign='middle'))
        ok = Button(text='OK', size_hint_y=None, height='46dp')
        ok.bind(on_release=popup.dismiss)
        body.add_widget(ok)

    btn_switch = Button(text='Switch to Almanac')
    btn_switch.bind(on_release=switch_almanac)
    btn_keep = Button(text='Keep Classic')
    btn_keep.bind(on_release=keep_classic)
    row.add_widget(btn_switch)
    row.add_widget(btn_keep)
    body.add_widget(row)
    popup.open()
