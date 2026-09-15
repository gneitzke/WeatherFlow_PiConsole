"""A local failure (no route, no resolver) never opens a host breaker, so its
retry backs off on its own instead of rerunning a doomed pass every 2 s (offline)."""
from lib import almanac_emit as ae, radar_http as http
from tests.test_radar_hybrid import hybrid  # noqa: F401


def failed_pass_delays(e, errors):
    delays=[]
    e._radar_retained_refresh=lambda *a:None
    e._radar_budget_retry=lambda source,needed,min_delay=0:delays.append(min_delay)
    for error in errors:
        assert not e._radar_failed_pass('iem',error,{})
    return delays


def test_consecutive_local_failures_back_off_to_the_cap(make_emitter):
    e=make_emitter()
    delays=failed_pass_delays(e,[http.LocalTransportError('no route')]*8)
    cap=ae.RADAR_LOCAL_RETRY_MAX_SEC
    assert delays==[2,4,8,16,32,cap,cap,cap]
    assert 'iem' not in e._radar_transport_failures  # still never advances fallback


def test_a_provider_failure_ends_the_local_backoff(make_emitter):
    e=make_emitter()
    local=http.LocalTransportError('no route')
    delays=failed_pass_delays(e,[local]*4+[OSError('host')]+[local])
    assert delays==[2,4,8,16,2,2]


def test_ambiguous_stalls_keep_the_prompt_retry_and_end_the_local_backoff(make_emitter):
    # A reused socket that got no bytes may be the provider stalling: it still
    # never advances fallback, but it must not wait out a local-outage backoff.
    e=make_emitter()
    local=http.LocalTransportError('no route')
    stall=http.AmbiguousTransportError('reused socket, no first byte')
    delays=failed_pass_delays(e,[local]*3+[stall]*3+[local])
    assert delays==[2,4,8,2,2,2,2]
    assert 'iem' not in e._radar_transport_failures


def test_a_long_outage_stays_at_the_cap(make_emitter):
    e=make_emitter()
    e._radar_local_failure_streak=10000
    assert failed_pass_delays(e,[http.LocalTransportError('no route')])==[ae.RADAR_LOCAL_RETRY_MAX_SEC]


def test_real_passes_back_off_on_a_dead_route_and_reset_when_it_returns(make_emitter, hybrid):
    def no_route(req, _):
        if 'iastate.edu' in req.full_url:
            raise http.LocalTransportError('no route to host')
    e=make_emitter()
    delays=[]
    budget_retry=e._radar_budget_retry
    def record(source, needed, min_delay=0):
        if min_delay:
            delays.append(min_delay)
        return budget_retry(source, needed, min_delay=min_delay)
    e._radar_budget_retry=record
    hybrid.failure=no_route
    for _ in range(4):
        e._do_radar(intent_triggered=False)
        hybrid.mono+=120
    assert delays==[2,4,8,16],delays
    assert e._radar_local_failure_streak==4
    hybrid.failure=None
    e._do_radar(intent_triggered=False)
    assert e._radar_result.available and e._radar_result.source_id=='iem-mrms-lcref'
    assert e._radar_local_failure_streak==0
