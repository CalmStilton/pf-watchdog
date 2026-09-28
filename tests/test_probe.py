import socket

import pf_watchdog as wd


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_open_when_something_listens():
    s = _listener()
    try:
        assert wd.probe("127.0.0.1", s.getsockname()[1], timeout=2) == wd.OPEN
    finally:
        s.close()


def test_refused_when_port_closed():
    s = _listener()
    port = s.getsockname()[1]
    s.close()
    assert wd.probe("127.0.0.1", port, timeout=2) == wd.REFUSED


def test_timeout_when_packets_silently_dropped(monkeypatch):
    def fake(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(wd.socket, "create_connection", fake)
    assert wd.probe("192.0.2.1", 1, timeout=0.1) == wd.TIMEOUT


def test_other_oserror_is_error(monkeypatch):
    def fake(*args, **kwargs):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(wd.socket, "create_connection", fake)
    assert wd.probe("portquiz.net", 1, timeout=0.1) == wd.ERROR
