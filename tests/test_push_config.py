import http.server
import socket
import threading
import urllib.parse

import pytest

import pf_watchdog as wd


def test_push_sends_status_and_msg():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        wd.push(f"http://127.0.0.1:{srv.server_address[1]}/api/push/abc", "down", "timeout 1.2.3.4:5 – x")
    finally:
        srv.shutdown()
    u = urllib.parse.urlparse(seen[0])
    q = urllib.parse.parse_qs(u.query)
    assert u.path == "/api/push/abc"
    assert q["status"] == ["down"]
    assert q["msg"] == ["timeout 1.2.3.4:5 – x"]


def test_push_to_dead_endpoint_does_not_raise():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    wd.push(f"http://127.0.0.1:{port}/api/push/abc", "up", "open")


def test_config_requires_push_url():
    with pytest.raises(ValueError):
        wd.Config.from_env({"DEPENDENTS": "app1"})


def test_config_requires_dependents():
    with pytest.raises(ValueError):
        wd.Config.from_env({"KUMA_PUSH_URL": "http://k/api/push/t", "DEPENDENTS": " , "})


def test_config_defaults_and_overrides():
    cfg = wd.Config.from_env({"KUMA_PUSH_URL": "http://k/api/push/t",
                              "DEPENDENTS": "app1, app2,,app3 ", "SETTLE_SECONDS": "5",
                              "GLUETUN_CONTAINER": "vpn"})
    assert cfg.kuma_push_url == "http://k/api/push/t"
    assert cfg.dependents == ("app1", "app2", "app3")
    assert cfg.docker_url == "http://pf-socket-proxy:2375"
    assert cfg.state_file == "/state/state.json"
    assert cfg.gluetun == "vpn"
    assert cfg.qbittorrent == "qbittorrent"
    assert cfg.settle_seconds == 5.0
