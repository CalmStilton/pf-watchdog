#!/usr/bin/env python3
"""VPN port-forward watchdog: proves a gluetun forwarded port is reachable
from the internet, remediates by failure type, and reports to Uptime Kuma."""
import json
import os
import socket
import sys
import time
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
