"""Direct Python Playwright check; run from the repo root (no local server needed).

./venv-test/bin/python -m tests.verify_radar_headless [--browser /path/to/chromium]
Screenshots and recorded fetch URLs go to --output-dir (default /tmp/wfp-radar-headless).
Kept separate from pytest so the unit suite does not require a browser install.
"""
import argparse
import base64
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from playwright.sync_api import sync_playwright

from tests import conftest  # noqa: F401 - the same Kivy stubs used by pytest
from tests.fixtures.config import make_config
from lib import almanac_emit as ae


def payload():
    config = make_config()
    app = SimpleNamespace(config=config, obsParser=SimpleNamespace(api_data={}))
    screen = SimpleNamespace(app=app, Obs={}, Met={}, Astro={}, Sager={})
    emitter = ae.AlmanacEmitter(screen)
    now = int(time.time())
    zoom = ae._radar_zoom_for(47.61)
    _, mpp, bounds, _ = ae._radar_viewport(47.61, -122.33, zoom, 480)
    bar, rings = ae._radar_scale(mpp, 480, 'mi')
    png = io.BytesIO()
    Image.new('RGBA', (480, 480), (0, 163, 224, 100)).save(png, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(png.getvalue()).decode()
    frames = (dict(id=str(now - 600), ts=now - 600, complete=False),
              dict(id=str(now), ts=now, complete=True, url=url))
    emitter._radar_result = ae._RadarResult(True, None, frames, str(now), now,
        dict(lat=47.61, lon=-122.33), zoom, mpp, bounds, bar, rings,
        ae._radar_nexrad(47.61, -122.33, 'mi'), now)
    return emitter._build_payload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/wfp-radar-headless'))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    html = (Path(__file__).resolve().parents[1] / 'design/almanac/console_live.html').read_text()
    shim = """
        window.pollURLs = [];
        window.fetch = function(url) {
            window.pollURLs.push(String(url));
            var data = PAYLOAD;
            data.ts = Date.now() / 1000;
            return Promise.resolve({ok:true, headers:{get:()=>new Date().toUTCString()},
                                    json:()=>Promise.resolve(data)});
        };
    """.replace('PAYLOAD', json.dumps(payload()))
    report = {}
    with sync_playwright() as p:
        options = dict(headless=True)
        if args.browser:
            options['executable_path'] = args.browser
        browser = p.chromium.launch(**options)
        for theme in ('light', 'night'):
            context = browser.new_context(viewport={'width': 1024, 'height': 600}, device_scale_factor=2)
            context.add_init_script(shim)
            context.route('https://radar.test/**', lambda route: route.fulfill(body=html, content_type='text/html'))
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'https://radar.test/index.html?tabs=1&theme={theme}')
            page.wait_for_function('window.pollURLs.length >= 2')
            assert all('&view=radar' not in u for u in page.evaluate('pollURLs'))
            page.locator('.tab[data-screen="s-radar"]').click()
            count = page.evaluate('pollURLs.length')
            page.wait_for_function('n => pollURLs.length > n', arg=count)
            assert '&view=radar' in page.evaluate('pollURLs.at(-1)')
            assert '&r=1' in page.evaluate('pollURLs.at(-1)')
            page.wait_for_function('document.querySelector("#rad-plate").dataset.state === "live"')
            assert page.locator('#s-radar').is_visible()
            assert page.locator('#rad-echo').evaluate('(e) => e.complete && e.naturalWidth === 480 && !e.hidden')
            box = page.locator('#rad-plate').bounding_box()
            assert box['width'] == box['height'] == 480
            assert box['y'] >= 0 and box['y'] + box['height'] <= 568
            page.screenshot(path=str(args.output_dir / f'radar-{theme}.png'))
            for tab in page.locator('.tab[data-screen]:not([data-screen="s-radar"]):not(.dim)').all():
                if not tab.is_visible():
                    continue
                screen = tab.get_attribute('data-screen')
                tab.click()
                count = page.evaluate('pollURLs.length')
                page.wait_for_function('n => pollURLs.length > n', arg=count)
                assert '&view=radar' not in page.evaluate('pollURLs.at(-1)')
                assert '&r=1' in page.evaluate('pollURLs.at(-1)')
                assert page.locator('#' + screen).is_visible()
            report[theme] = page.evaluate('pollURLs')
            assert errors == [], errors
            context.close()
        context = browser.new_context(viewport={'width': 1024, 'height': 600})
        context.add_init_script(shim)
        context.route('https://radar.test/**', lambda route: route.fulfill(body=html, content_type='text/html'))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('https://radar.test/index.html')
        page.wait_for_function('window.pollURLs.length >= 2')
        assert page.locator('#s-obs').is_visible()
        assert not page.locator('.tabs').is_visible()
        assert all('&view=radar' not in u for u in page.evaluate('pollURLs'))
        assert '&r=1' in page.evaluate('pollURLs.at(-1)')
        assert errors == [], errors
        report['no-tabs'] = page.evaluate('pollURLs')
        context.close()
        browser.close()
    (args.output_dir / 'poll-urls.json').write_text(json.dumps(report, indent=2))
    print('PASS: latest radar light/dark, tab view signal on/off, render heartbeat, other tabs, no-tabs default; no page errors')


if __name__ == '__main__':
    main()
