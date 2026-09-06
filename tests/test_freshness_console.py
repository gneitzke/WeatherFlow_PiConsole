""" The overlay page is the last link in the chain and the only one a person
looks at, so its freshness rules are pinned here structurally. The behaviour
itself was verified by headless render (chromium, 1024x600, fetch shimmed):
a hung fetch flips the mark in ~11 s with 3 requests outstanding instead of 45
with no mark at all, and a payload carrying obsAgeSec 900 reads SILENT.

These assertions are the guard rail that keeps the four properties that made
that possible from being edited away: one bounded request at a time, a
freshness timer that does not depend on any fetch settling, payload validation
before render, and success bookkeeping after render returns. """

from pathlib import Path

import pytest

PAGE = Path(__file__).parents[1] / 'design/almanac/console_live.html'


@pytest.fixture(scope='module')
def page():
    return PAGE.read_text()


def test_poll_is_bounded_and_single_flight(page):
    # No deadline and no overlap guard is how 45 unsettled promises piled up.
    assert 'var FETCH_MS' in page
    assert 'new AbortController()' in page
    assert 'ctl.abort()' in page
    assert 'if (started - pollStart < FETCH_MS) return;' in page   # never overlap
    assert 'if (gen !== pollGen) return;' in page                  # abandoned reply ignored


def test_an_unanswered_request_counts_as_a_miss(page):
    # A promise that neither resolves nor aborts must not read as success.
    body = page.split('function poll()', 1)[1]
    abandon = body.split('var gen = ++pollGen;', 1)[0]
    assert 'failCount++;' in abandon


def test_freshness_timer_runs_independently_of_any_fetch(page):
    assert 'setInterval(updateFreshness, 1000)' in page
    # and it is driven by the clock since the last RENDER, not by a settle
    assert 'Date.now() - (lastRenderMs || loadMs) > STALE_SEC * 1000' in page


def test_payload_is_validated_before_render(page):
    assert 'function validPayload(d)' in page
    assert 'if (!validPayload(d)) throw new Error("bad payload");' in page
    # render() is no longer the thing that decides freshness
    assert 'checkStale(' not in page


def test_success_is_booked_after_render_returns(page):
    """ failCount used to reset before render() ran, so a payload that threw
    every time left the page looking healthy forever. """
    body = page.split('.then(function (d) {', 1)[1].split('.catch(', 1)[0]
    assert body.index('render(d);') < body.index('lastRenderMs = Date.now();')
    assert body.index('render(d);') < body.index('failCount = 0;')


def test_render_heartbeat_is_reported_only_after_a_frame_painted(page):
    # r=1 rides the NEXT poll, and only once render() has returned.
    assert 'var ack = reportRender; reportRender = false;' in page     # consumed per request, never re-sent
    assert '(ack ? "&r=1" : "")' in page
    body = page.split('.then(function (d) {', 1)[1].split('.catch(', 1)[0]
    assert body.index('render(d);') < body.index('reportRender = true;')


def test_sensor_silence_is_shown_distinctly_from_a_stale_feed(page):
    assert 'var OBS_STALE_SEC = 300;' in page
    assert 'return "silent";' in page
    assert 'return "stale";' in page
    # the payload's age is topped up by how long the frame has been on screen
    assert 'd.obsAgeSec + recvAgeSec + sinceRecv > OBS_STALE_SEC' in page   # aged on the producer's clock + monotonic elapsed
    # extends the existing masthead mark rather than adding a second indicator
    assert page.count('id="staleflag"') == 1
    assert 'staleEl.classList.toggle("on", !!mode)' in page
