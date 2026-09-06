"""Static regression coverage for the Pi-only kiosk launcher."""

import shutil
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "design/almanac/kiosk/almanac-kiosk.sh"


def test_kiosk_launcher_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_kiosk_shutdown_exits_and_reaps_children_without_relaunching():
    script = SCRIPT.read_text()

    assert "trap cleanup EXIT" in script
    assert "trap on_signal INT TERM" in script
    assert "on_signal(){ STOPPING=1; exit 0; }" in script
    assert 'while [ "$STOPPING" -eq 0 ]; do' in script
    assert '[ "$STOPPING" -eq 0 ] || return 0' in script
    assert "stop_process(){" in script
    assert 'kill -KILL "$pid"' in script


def test_kiosk_watchdog_health_requests_are_bounded_and_parse_failures_restart_server():
    script = SCRIPT.read_text()

    assert "curl -sS --connect-timeout 2 --max-time 4" in script
    assert "response=$(health_response) || return 1" in script
    assert 'health_failures=$((health_failures + 1))' in script
    assert 'stop_process "$SERVE_PID" server; launch_server' in script


def test_watchdog_render_heartbeat_counts_renders_not_requests():
    # A growing request count never proved a frame reached the screen: a 404, a
    # throw inside render(), or a LAN browser all bump "polls".
    script = SCRIPT.read_text()

    assert '"renders"' in script
    assert "read_renders()" in script and "renders_growing()" in script
    assert "read_polls()" not in script and "polls_growing" not in script
    # every probe stays bounded (health_response carries --max-time)
    assert "response=$(health_response) || return 1" in script


def test_watchdog_separates_a_silent_sensor_from_a_stalled_engine():
    # "degraded" must be a recognised status — falling through to the unparsable
    # branch would restart the web server on a healthy one.
    script = SCRIPT.read_text()

    assert 'case "$st" in ok|stale|degraded|error)' in script
    assert 'elif [ "$st" = "degraded" ]; then' in script
    # bounded response: one engine restart per episode, then leave a dead
    # station alone rather than restarting every 30 s forever
    assert 'degraded_acted=1' in script
    assert '[ "$degraded_acted" -eq 0 ]' in script
    assert "loops=0; stale_hits=0; health_failures=0; degraded_hits=0; degraded_acted=0" in script


def test_kiosk_launcher_passes_shellcheck_when_available():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        return
    subprocess.run([shellcheck, str(SCRIPT)], check=True)
