# pf-watchdog

Checks that a VPN's forwarded port is actually reachable from the internet,
not just that every config layer agrees on the number. Built for a
[gluetun](https://github.com/qdm12/gluetun) + qBittorrent setup, where
a provider can grant a port forward and then silently drop the traffic.

Each run:

1. Reads gluetun's public IP and forwarded port (`/tmp/gluetun/ip`,
   `/tmp/gluetun/forwarded_port`) through the Docker API.
2. Proves the probing host's own egress works: `1.1.1.1:443` and
   `portquiz.net:<port>` must both connect. If either fails, the run is
   inconclusive and does nothing.
3. Connects to `<ip>:<port>` from outside the tunnel and classifies:
   - **open**: healthy.
   - **refused** (fast RST): the forward works but nothing is listening. After
     2 consecutive failures, restart the torrent client only.
   - **timeout**: the forward is dead. After 2 consecutive failures, restart
     gluetun, then every container in its network namespace.
4. Limits remediation to 6 cycles per 6 hours, spaced out: a 2nd cycle is
   allowed 15 minutes after the 1st, a 3rd 30 minutes after the 2nd, and later
   ones an hour apart (each with 2 minutes' slack for scheduler jitter). A bad
   VPN reroll usually recovers within the hour, while a real provider outage
   can't flap the stack indefinitely. While waiting it reports `down` with
   "next restart in ~N min".
5. Records which VPN server it connected through (the `remote` in gluetun's
   `/etc/openvpn/target.ovpn`) alongside the exit IP, port and result, both in
   the log line and in `history.jsonl` next to the state file. A line is
   appended only when one of those changes, so the file stays small; it keeps the
   last 5000 entries. One server can exit through several IP ranges, so this is
   the evidence for telling servers whose forwards work from ones whose don't.
6. Reports to an [Uptime Kuma](https://github.com/louislam/uptime-kuma) Push
   monitor (`up`/`down` plus a message). Inconclusive runs push nothing, so set the
   monitor's heartbeat to about 2× the schedule and a dead watchdog alerts too.

It's a one-shot container: schedule it with Ofelia, cron or similar.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `KUMA_PUSH_URL` | yes | | Push URL without the query string |
| `DEPENDENTS` | yes | | Comma-separated containers using `network_mode: service:<gluetun>`, all restarted after a gluetun restart |
| `GLUETUN_CONTAINER` | | `gluetun` | |
| `QBITTORRENT_CONTAINER` | | `qbittorrent` | |
| `DOCKER_URL` | | `http://pf-socket-proxy:2375` | Docker API endpoint |
| `STATE_FILE` | | `/state/state.json` | Mount a volume at `/state` |
| `HISTORY_FILE` | | `history.jsonl` beside `STATE_FILE` | Server/exit/port/result change log (JSON lines) |
| `SETTLE_SECONDS` | | `90` | Wait after restarts before re-probing |
| `TZ` | | UTC | Timezone for log timestamps, e.g. `Europe/London` |

## Docker API access

Don't mount the Docker socket directly. Use
[linuxserver/socket-proxy](https://github.com/linuxserver/docker-socket-proxy)
with exactly this:

```yaml
image: lscr.io/linuxserver/socket-proxy:3.4.5
read_only: true
tmpfs:
  - /run:rw,mode=1777
environment:
  CONTAINERS: 1      # inspect
  ALLOW_ARCHIVE: 1   # read gluetun's files (GET only while POST=0)
  ALLOW_RESTARTS: 1  # stop/restart/kill only
  POST: 0            # never 1: with CONTAINERS=1 it also allows exec/create
```

`tecnativa/docker-socket-proxy` (v0.5.0) can't express this: its POST rule is
evaluated before `ALLOW_RESTARTS`, so restarts need `POST=1`, which also opens
`exec`/`create`.

`./proxy-check.sh` verifies that permission set against a local Docker engine.

The watchdog container must **not** share gluetun's network namespace. The
probe has to leave through your normal WAN connection.

## Development

    pip install -r requirements-dev.txt
    python -m pytest
