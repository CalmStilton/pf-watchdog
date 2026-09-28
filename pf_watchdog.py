#!/usr/bin/env python3
"""VPN port-forward watchdog: proves a gluetun forwarded port is reachable
from the internet, remediates by failure type, and reports to Uptime Kuma."""
import http.client
import io
import json
import os
import socket
import sys
import tarfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

OPEN, REFUSED, TIMEOUT, ERROR = "open", "refused", "timeout", "error"


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}", flush=True)


def probe(host, port, timeout=10.0):
    """TCP connect and classify. RST vs timeout is the signal that selects the fix."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return OPEN
    except ConnectionRefusedError:
        return REFUSED
    except TimeoutError:
        return TIMEOUT
    except OSError:
        return ERROR


THRESHOLD = 2
CAP = 3
WINDOW_SECONDS = 6 * 3600


@dataclass
class State:
    consecutive_failures: int = 0
    remediations: list = field(default_factory=list)  # epoch seconds


def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return State(
            consecutive_failures=int(d.get("consecutive_failures", 0)),
            remediations=[float(t) for t in d.get("remediations", [])],
        )
    except FileNotFoundError:
        return State()
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log(f"state file unreadable, starting fresh: {e}")
        return State()


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"consecutive_failures": state.consecutive_failures,
                   "remediations": state.remediations}, f)
    os.replace(tmp, path)


@dataclass
class Decision:
    action: str          # "none" | "restart_qbittorrent" | "reroll_vpn"
    status: str | None   # "up" | "down" | None = push nothing
    msg: str


def decide(state, controls_ok, result, now, target):
    state.remediations = [t for t in state.remediations if now - t < WINDOW_SECONDS]
    if not controls_ok:
        return Decision("none", None, f"inconclusive {target} – control probe failed")
    if result == OPEN:
        state.consecutive_failures = 0
        return Decision("none", "up", f"open {target}")

    state.consecutive_failures += 1
    kind = "refused" if result == REFUSED else "timeout"
    if state.consecutive_failures < THRESHOLD:
        return Decision("none", "down",
                        f"{kind} {target} – confirming ({state.consecutive_failures}/{THRESHOLD})")
    if len(state.remediations) >= CAP:
        return Decision("none", "down", f"{kind} {target} – capped – manual action needed")

    state.remediations.append(now)
    cycle = len(state.remediations)
    if result == REFUSED:
        return Decision("restart_qbittorrent", "down",
                        f"refused {target} – restarting qbittorrent (cycle {cycle}/{CAP})")
    return Decision("reroll_vpn", "down",
                    f"timeout {target} – restarting gluetun (cycle {cycle}/{CAP})")


class DockerError(Exception):
    pass


class Docker:
    """Minimal Docker Engine API client, pointed at pf-socket-proxy."""

    def __init__(self, base_url, timeout=60.0):
        u = urllib.parse.urlparse(base_url)
        self.host, self.port, self.timeout = u.hostname, u.port or 2375, timeout

    def _request(self, method, path):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
        except OSError as e:
            raise DockerError(f"{method} {path}: {e}") from e
        finally:
            conn.close()
        if resp.status >= 300:
            raise DockerError(f"{method} {path} -> {resp.status}: {body[:200]!r}")
        return body

    def inspect(self, name):
        return json.loads(self._request("GET", f"/containers/{name}/json"))

    def read_file(self, name, path):
        body = self._request("GET", f"/containers/{name}/archive?path={urllib.parse.quote(path)}")
        with tarfile.open(fileobj=io.BytesIO(body)) as tar:
            member = tar.next()
            if member is None or not member.isfile():
                raise DockerError(f"{name}:{path} is not a regular file")
            return tar.extractfile(member).read().decode().strip()

    def restart(self, name, t=10):
        self._request("POST", f"/containers/{name}/restart?t={t}")


def push(base_url, status, msg, timeout=10.0):
    url = f"{base_url}?{urllib.parse.urlencode({'status': status, 'msg': msg, 'ping': ''})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read()
    except OSError as e:
        log(f"kuma push failed: {e}")


@dataclass
class Config:
    kuma_push_url: str
    dependents: tuple  # every container sharing the VPN container's network namespace
    docker_url: str = "http://pf-socket-proxy:2375"
    state_file: str = "/state/state.json"
    gluetun: str = "gluetun"
    qbittorrent: str = "qbittorrent"
    settle_seconds: float = 90
    gluetun_ready_timeout: float = 180

    @classmethod
    def from_env(cls, env):
        url = env.get("KUMA_PUSH_URL", "").strip()
        if not url:
            raise ValueError("KUMA_PUSH_URL is required")
        deps = tuple(d.strip() for d in env.get("DEPENDENTS", "").split(",") if d.strip())
        if not deps:
            raise ValueError("DEPENDENTS is required (comma-separated container names)")
        cfg = cls(kuma_push_url=url, dependents=deps)
        cfg.docker_url = env.get("DOCKER_URL", cfg.docker_url)
        cfg.state_file = env.get("STATE_FILE", cfg.state_file)
        cfg.gluetun = env.get("GLUETUN_CONTAINER", cfg.gluetun)
        cfg.qbittorrent = env.get("QBITTORRENT_CONTAINER", cfg.qbittorrent)
        cfg.settle_seconds = float(env.get("SETTLE_SECONDS", cfg.settle_seconds))
        return cfg
