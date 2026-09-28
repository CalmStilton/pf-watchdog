import http.server
import io
import json
import tarfile
import threading
import urllib.parse

import pytest

import pf_watchdog as wd


def _tar(name, content):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        data = content.encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_docker():
    calls = []
    files = {"/tmp/gluetun/forwarded_port": "64923\n", "/tmp/gluetun/ip": "203.0.113.5\n"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=b"", ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            calls.append(("GET", self.path))
            u = urllib.parse.urlparse(self.path)
            if u.path == "/containers/gluetun/json":
                return self._send(200, json.dumps({"State": {"Health": {"Status": "healthy"}}}).encode())
            if u.path == "/containers/gluetun/archive":
                p = urllib.parse.parse_qs(u.query)["path"][0]
                if p in files:
                    return self._send(200, _tar(p.rsplit("/", 1)[1], files[p]), "application/x-tar")
                return self._send(404, b'{"message":"no such file"}')
            self._send(404, b"{}")

        def do_POST(self):
            calls.append(("POST", self.path))
            self._send(204)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield wd.Docker(f"http://127.0.0.1:{srv.server_address[1]}"), calls
    srv.shutdown()


def test_inspect_returns_health(fake_docker):
    docker, _ = fake_docker
    assert docker.inspect("gluetun")["State"]["Health"]["Status"] == "healthy"


def test_read_file_extracts_and_strips(fake_docker):
    docker, calls = fake_docker
    assert docker.read_file("gluetun", "/tmp/gluetun/forwarded_port") == "64923"
    assert calls[-1] == ("GET", "/containers/gluetun/archive?path=/tmp/gluetun/forwarded_port")


def test_missing_file_raises(fake_docker):
    docker, _ = fake_docker
    with pytest.raises(wd.DockerError):
        docker.read_file("gluetun", "/tmp/gluetun/nope")


def test_restart_posts(fake_docker):
    docker, calls = fake_docker
    docker.restart("qbittorrent")
    assert calls[-1] == ("POST", "/containers/qbittorrent/restart?t=10")
