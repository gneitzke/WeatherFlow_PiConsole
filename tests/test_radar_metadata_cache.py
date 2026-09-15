"""The conditional-GET metadata cache stays bounded. NEXRAD site listings put a
start/end window in the URL, so every discovery pass is a new key; unbounded,
every listing a kiosk ever fetched stayed in memory for the life of the process."""
from lib import almanac_emit as ae
from lib import radar_http as transport


class _Response:
    status = 200
    headers = {'ETag': '"listing"'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return b'{"scans": []}'


def _listing(i):
    return f'{ae.RADAR_SITE_LIST_URL}?operation=list&radar=RTX&product=N0B&start={i}&end={i + 1}'


def test_metadata_cache_keeps_only_the_newest_entries(make_emitter, monkeypatch):
    emitter = make_emitter()
    emitter._radar_session = transport.RadarSession()
    monkeypatch.setattr(emitter._radar_session, 'open', lambda *a, **k: _Response())
    total = ae.RADAR_METADATA_CACHE_SIZE + 25
    for i in range(total):
        emitter._radar_request('iem-nexrad-n0b', _listing(i), ae.time.monotonic() + 10, metadata=True)
    assert len(emitter._radar_metadata) == ae.RADAR_METADATA_CACHE_SIZE
    assert _listing(total - 1) in emitter._radar_metadata  # newest kept
    assert _listing(0) not in emitter._radar_metadata      # oldest evicted
