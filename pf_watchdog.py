#!/usr/bin/env python3
"""VPN port-forward watchdog: proves a gluetun forwarded port is reachable
from the internet, remediates by failure type, and reports to Uptime Kuma."""
import socket
import sys
import time

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
