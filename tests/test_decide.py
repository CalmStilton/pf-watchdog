import pf_watchdog as wd

T = "1.2.3.4:5000"


def test_inconclusive_pushes_nothing_and_keeps_counter():
    s = wd.State(consecutive_failures=1)
    d = wd.decide(s, False, wd.TIMEOUT, 1000.0, T)
    assert (d.action, d.status) == ("none", None)
    assert s.consecutive_failures == 1


def test_open_resets_counter_and_pushes_up():
    s = wd.State(consecutive_failures=1)
    d = wd.decide(s, True, wd.OPEN, 1000.0, T)
    assert (d.action, d.status, d.msg) == ("none", "up", f"open {T}")
    assert s.consecutive_failures == 0


def test_first_failure_pushes_down_without_remediating():
    s = wd.State()
    d = wd.decide(s, True, wd.TIMEOUT, 1000.0, T)
    assert (d.action, d.status) == ("none", "down")
    assert d.msg == f"timeout {T} – confirming (1/2)"
    assert s.remediations == []


def test_second_timeout_rerolls_vpn():
    s = wd.State(consecutive_failures=1)
    d = wd.decide(s, True, wd.TIMEOUT, 1000.0, T)
    assert (d.action, d.status) == ("reroll_vpn", "down")
    assert d.msg == f"timeout {T} – restarting gluetun (cycle 1/6)"
    assert s.remediations == [1000.0]


def test_second_refused_restarts_qbittorrent_only():
    s = wd.State(consecutive_failures=1)
    d = wd.decide(s, True, wd.REFUSED, 1000.0, T)
    assert d.action == "restart_qbittorrent"
    assert d.msg == f"refused {T} – restarting qbittorrent (cycle 1/6)"


def test_error_result_is_treated_as_timeout():
    s = wd.State(consecutive_failures=1)
    assert wd.decide(s, True, wd.ERROR, 1000.0, T).action == "reroll_vpn"


def test_cap_blocks_seventh_remediation():
    s = wd.State(consecutive_failures=9, remediations=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    d = wd.decide(s, True, wd.TIMEOUT, 20000.0, T)
    assert (d.action, d.status) == ("none", "down")
    assert d.msg == f"timeout {T} – capped – manual action needed"
    assert len(s.remediations) == 6


def test_second_cycle_allowed_15_minutes_after_first():
    s = wd.State(consecutive_failures=3, remediations=[1000.0])
    d = wd.decide(s, True, wd.TIMEOUT, 1000.0 + 900, T)
    assert d.action == "reroll_vpn"
    assert d.msg == f"timeout {T} – restarting gluetun (cycle 2/6)"


def test_backoff_tolerates_scheduler_jitter():
    s = wd.State(consecutive_failures=3, remediations=[1000.0])
    assert wd.decide(s, True, wd.TIMEOUT, 1000.0 + 890, T).action == "reroll_vpn"


def test_third_cycle_waits_30_minutes_after_second():
    s = wd.State(consecutive_failures=4, remediations=[0.0, 900.0])
    d = wd.decide(s, True, wd.TIMEOUT, 1800.0, T)
    assert (d.action, d.status) == ("none", "down")
    assert d.msg == f"timeout {T} – next restart in ~15 min (cycle 3/6)"
    assert s.remediations == [0.0, 900.0]
    assert wd.decide(s, True, wd.TIMEOUT, 2700.0, T).action == "reroll_vpn"


def test_later_cycles_wait_an_hour():
    s = wd.State(consecutive_failures=5, remediations=[0.0, 900.0, 2700.0])
    d = wd.decide(s, True, wd.TIMEOUT, 2700.0 + 1800, T)
    assert d.action == "none"
    assert d.msg == f"timeout {T} – next restart in ~30 min (cycle 4/6)"
    assert wd.decide(s, True, wd.TIMEOUT, 2700.0 + 3600, T).action == "reroll_vpn"


def test_backoff_applies_to_qbittorrent_restarts_too():
    s = wd.State(consecutive_failures=4, remediations=[0.0, 900.0])
    assert wd.decide(s, True, wd.REFUSED, 1800.0, T).action == "none"


def test_remediations_outside_window_expire():
    s = wd.State(consecutive_failures=5, remediations=[0.0, 1.0, 2.0])
    now = wd.WINDOW_SECONDS + 10.0
    d = wd.decide(s, True, wd.TIMEOUT, now, T)
    assert d.action == "reroll_vpn"
    assert s.remediations == [now]
