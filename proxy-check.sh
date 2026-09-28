#!/usr/bin/env bash
# Verifies the socket-proxy permission set pf-watchdog relies on, against the local Docker engine.
# Requires: docker build -t pf-watchdog:local .
set -euo pipefail
NET=pfw-check
SO=(--security-opt apparmor=unconfined)
cleanup() { docker rm -f pfw-proxy pfw-dummy >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; docker volume rm pfw-state >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null
docker run -d --name pfw-dummy "${SO[@]}" --network "$NET" alpine sleep 600 >/dev/null
docker run -d --name pfw-proxy "${SO[@]}" --network "$NET" \
  -e CONTAINERS=1 -e ALLOW_RESTARTS=1 -e ALLOW_ARCHIVE=1 -e POST=0 \
  --read-only --tmpfs /run:rw,mode=1777 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  lscr.io/linuxserver/socket-proxy:3.4.5 >/dev/null
sleep 3

fail=0
expect() {  # expect <code> <method> <path>
  local got
  got=$(docker run --rm "${SO[@]}" --network "$NET" curlimages/curl -s -o /dev/null -w '%{http_code}' \
        -X "$2" -H 'Content-Type: application/json' -d '{}' "http://pfw-proxy:2375$3")
  if [[ "$got" == "$1" ]]; then echo "ok   $2 $3 -> $got"; else echo "FAIL $2 $3 -> $got (want $1)"; fail=1; fi
}
expect 200 GET    /containers/pfw-dummy/json
expect 200 GET    "/containers/pfw-dummy/archive?path=/etc/hostname"
expect 204 POST   "/containers/pfw-dummy/restart?t=1"
expect 403 POST   /containers/pfw-dummy/exec
expect 403 PUT    "/containers/pfw-dummy/archive?path=/tmp"
expect 403 POST   /containers/create
expect 403 DELETE /containers/pfw-dummy
expect 403 GET    /images/json

# Real tar parsing through the real proxy, and a writable state volume, in the real image as nobody.
docker run --rm "${SO[@]}" --network "$NET" -v pfw-state:/state pf-watchdog:local \
  python -c "import pf_watchdog as w; \
print('ok   read_file ->', w.Docker('http://pfw-proxy:2375').read_file('pfw-dummy','/etc/hostname')); \
w.save_state('/state/state.json', w.State(1, [1.0])); print('ok   state ->', w.load_state('/state/state.json'))" || fail=1
exit $fail
