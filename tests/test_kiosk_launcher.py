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


def test_kiosk_launcher_passes_shellcheck_when_available():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        return
    subprocess.run([shellcheck, str(SCRIPT)], check=True)
