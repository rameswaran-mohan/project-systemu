"""v0.9.8 (B2) — research-loop convergence steer.

Live RCA: on sparse/ill-posed tasks the agent web_search/web_read'd 9× and NEVER
wrote the deliverable, looping to MAX_ITERATIONS. The keystone credits "search"
objectives from audit evidence, which resets the stall counter, so the stall path
never fires. _research_loop_steer counts consecutive read-only research calls
INDEPENDENT of objective-credit and force-steers the agent to produce.
"""
from systemu.runtime.shadow_runtime import _research_loop_steer
from sharing_on.config import Config


def _step(tool, *, success=True, consec, steers, threshold=5, cap=2):
    return _research_loop_steer(
        tool_name=tool, success=success, consec_reads=consec,
        steers_used=steers, threshold=threshold, cap=cap,
    )


def test_steer_fires_after_threshold_research_calls():
    consec, steers, steer = 0, 0, None
    for _ in range(5):
        consec, steers, steer = _step("web_search", consec=consec, steers=steers)
    assert steer is not None
    assert "STOP searching" in steer
    assert steers == 1
    assert consec == 0  # reset after firing


def test_file_write_resets_counter_no_steer():
    # a successful produce call resets the research counter and never steers,
    # even when the counter was already at threshold.
    consec, steers, steer = _step("file_write", consec=4, steers=0)
    assert consec == 0
    assert steer is None
    consec, steers, steer = _step("file_write", consec=10, steers=0)
    assert consec == 0
    assert steer is None


def test_non_research_tool_does_not_increment():
    consec, steers, steer = _step("calculator", consec=3, steers=0)
    assert consec == 3
    assert steer is None


def test_failed_research_call_does_not_increment():
    consec, steers, steer = _step("web_search", success=False, consec=3, steers=0)
    assert consec == 3
    assert steer is None


def test_cap_stops_further_steers():
    # already used the cap -> no steer even past threshold
    consec, steers, steer = _step("web_search", consec=10, steers=2, cap=2)
    assert steer is None
    assert steers == 2


def test_fetch_json_counts_as_research():
    consec, steers, steer = 0, 0, None
    for _ in range(5):
        consec, steers, steer = _step("fetch_json", consec=consec, steers=steers)
    assert steer is not None


def test_config_has_research_loop_knobs():
    c = Config()
    assert c.research_loop_threshold == 5
    assert c.research_loop_max_steers == 2
