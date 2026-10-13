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
CAP = 6
WINDOW_SECONDS = 6 * 3600
# Minimum minutes between remediation n-1 and n (index n-1). A bad VPN reroll is
# retried quickly at first, then backs off so an outage can't flap the stack.
GAP_MINUTES = (0, 15, 30, 60, 60, 60)
GAP_SLACK_SECONDS = 120  # Ofelia's "@every 15m" doesn't fire to the second


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
    if state.remediations:
        wait = GAP_MINUTES[len(state.remediations)] * 60 - (now - max(state.remediations))
        if wait > GAP_SLACK_SECONDS:
            return Decision("none", "down", f"{kind} {target} – next restart in"
                            f" ~{round(wait / 60)} min (cycle {len(state.remediations) + 1}/{CAP})")

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
    history_file: str = ""  # defaults to history.jsonl next to the state file

    def __post_init__(self):
        if not self.history_file:
            self.history_file = os.path.join(os.path.dirname(self.state_file), "history.jsonl")

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
        cfg.history_file = env.get("HISTORY_FILE",
                                   os.path.join(os.path.dirname(cfg.state_file), "history.jsonl"))
        cfg.gluetun = env.get("GLUETUN_CONTAINER", cfg.gluetun)
        cfg.qbittorrent = env.get("QBITTORRENT_CONTAINER", cfg.qbittorrent)
        cfg.settle_seconds = float(env.get("SETTLE_SECONDS", cfg.settle_seconds))
        return cfg


def read_gluetun(docker, cfg):
    ip = docker.read_file(cfg.gluetun, "/tmp/gluetun/ip")
    port = int(docker.read_file(cfg.gluetun, "/tmp/gluetun/forwarded_port"))
    return ip, port


def read_entry(docker, cfg):
    """VPN server gluetun connected to (OpenVPN 'remote' address), or None.

    The exit IP doesn't identify the server: one server can exit via several
    ranges. Recording both is what lets us tell good servers from bad ones."""
    try:
        conf = docker.read_file(cfg.gluetun, "/etc/openvpn/target.ovpn")
    except (DockerError, OSError, ValueError):
        return None
    for line in conf.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "remote":
            return parts[1]
    return None


HISTORY_MAX_LINES = 5000


def record_history(path, entry, exit_ip, port, result, now):
    """Append to the JSONL history only when the server, address, port or result changes."""
    rec = {"t": now, "entry": entry, "exit": exit_ip, "port": port, "result": result}
    try:
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            lines = []
        if lines:
            last = json.loads(lines[-1])
            if all(last.get(k) == rec[k] for k in ("entry", "exit", "port", "result")):
                return
        lines = (lines + [json.dumps(rec)])[-HISTORY_MAX_LINES:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        log(f"history not recorded: {e}")


def reroll_vpn(cfg, docker, *, now, sleep):
    failed = []
    try:
        docker.restart(cfg.gluetun)
    except DockerError as e:
        log(f"restart {cfg.gluetun} failed: {e}")
        failed.append(cfg.gluetun)
    deadline = now() + cfg.gluetun_ready_timeout
    while True:
        try:
            health = docker.inspect(cfg.gluetun)["State"].get("Health", {}).get("Status")
            if health == "healthy":
                read_gluetun(docker, cfg)
                break
        except (DockerError, ValueError, KeyError):
            pass
        if now() >= deadline:
            log("gluetun not ready before deadline; restarting dependents anyway")
            break
        sleep(5)
    # A gluetun restart orphans its network namespace: every dependent must be restarted.
    for name in cfg.dependents:
        try:
            docker.restart(name)
        except DockerError as e:
            log(f"restart {name} failed: {e}")
            failed.append(name)
    sleep(cfg.settle_seconds)
    return failed


def run(cfg, docker, *, now=time.time, sleep=time.sleep, probe=probe, push=push):
    state = load_state(cfg.state_file)
    try:
        ip, port = read_gluetun(docker, cfg)
    except (DockerError, ValueError) as e:
        msg = f"cannot read gluetun state: {e}"
        log(msg)
        push(cfg.kuma_push_url, "down", msg)
        return 1

    target = f"{ip}:{port}"
    entry = read_entry(docker, cfg)
    wan = probe("1.1.1.1", 443)
    egress = probe("portquiz.net", port)
    result = probe(ip, port)
    log(f"controls 1.1.1.1:443={wan} portquiz.net:{port}={egress};"
        f" target {target}={result} (entry {entry or 'unknown'})")

    controls_ok = wan == OPEN and egress == OPEN
    if controls_ok:  # an inconclusive probe says nothing about the server
        record_history(cfg.history_file, entry, ip, port, result, now())
    d = decide(state, controls_ok, result, now(), target)
    save_state(cfg.state_file, state)
    status, msg = d.status, d.msg

    if d.action != "none":
        log(msg)
        failed = []
        try:
            if d.action == "reroll_vpn":
                failed = reroll_vpn(cfg, docker, now=now, sleep=sleep)
            else:
                docker.restart(cfg.qbittorrent)
                sleep(cfg.settle_seconds)
            ip, port = read_gluetun(docker, cfg)
            after = probe(ip, port)
            record_history(cfg.history_file, read_entry(docker, cfg), ip, port, after, now())
            if after == OPEN:
                state.consecutive_failures = 0
                status, msg = "up", f"recovered on {ip}:{port} after {len(state.remediations)} cycle(s)"
            else:
                msg += f" – still {after} on {ip}:{port} after remediation"
            if failed:
                msg += f" – restart failed: {', '.join(failed)}"
        except (DockerError, ValueError) as e:
            msg += f" – remediation error: {e}"
        save_state(cfg.state_file, state)

    log(msg)
    if status:
        push(cfg.kuma_push_url, status, msg)
    return 0


def main():
    cfg = Config.from_env(os.environ)
    return run(cfg, Docker(cfg.docker_url))


if __name__ == "__main__":
    sys.exit(main())
