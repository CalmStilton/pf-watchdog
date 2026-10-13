import itertools
import json

import pf_watchdog as wd

CONTROLS = ("1.1.1.1", "portquiz.net")
DEPS = ("qbittorrent", "app1", "app2")


class FakeDocker:
    def __init__(self, ip="1.2.3.4", port="5000", health="healthy", fail_restart=(), reroll_to=None, entry="169.150.208.129"):
        self.files = {"/tmp/gluetun/ip": ip, "/tmp/gluetun/forwarded_port": port} if ip else {}
        if ip and entry:
            self.files["/etc/openvpn/target.ovpn"] = f"client\nproto tcp-client\nremote {entry} 443\npersist-tun\n"
        self.reroll_to = reroll_to  # (ip, port) gluetun lands on after a restart
        self.health = health
        self.fail_restart = set(fail_restart)
        self.restarts = []
        self.attempts = []

    def read_file(self, name, path):
        assert name == "gluetun"
        if path not in self.files:
            raise wd.DockerError("404")
        return self.files[path]

    def inspect(self, name):
        return {"State": {"Health": {"Status": self.health}}}

    def restart(self, name, t=10):
        self.attempts.append(name)
        if name in self.fail_restart:
            raise wd.DockerError("403 forbidden")
        self.restarts.append(name)
        if name == "gluetun" and self.reroll_to:
            ip, port = self.reroll_to
            self.files.update({"/tmp/gluetun/ip": ip, "/tmp/gluetun/forwarded_port": port})


def make_probe(target_results, controls=wd.OPEN):
    seq = list(target_results)

    def probe(host, port, timeout=10.0):
        if host in CONTROLS:
            return controls
        return seq.pop(0)
    return probe


def setup(tmp_path, state=None):
    cfg = wd.Config(kuma_push_url="http://kuma/api/push/t", dependents=DEPS,
                    state_file=str(tmp_path / "state.json"), settle_seconds=0)
    if state:
        wd.save_state(cfg.state_file, state)
    pushes = []
    return cfg, pushes, (lambda url, status, msg: pushes.append((status, msg)))


def go(cfg, docker, probe, push, now=lambda: 1000.0):
    return wd.run(cfg, docker, now=now, sleep=lambda s: None, probe=probe, push=push)


def test_healthy_run_pushes_up(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    docker = FakeDocker()
    assert go(cfg, docker, make_probe([wd.OPEN]), push) == 0
    assert pushes == [("up", "open 1.2.3.4:5000")]
    assert docker.restarts == []


def test_inconclusive_pushes_nothing(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.TIMEOUT], controls=wd.TIMEOUT), push)
    assert pushes == []
    assert docker.restarts == []


def test_first_failure_pushes_down_and_records(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.TIMEOUT]), push)
    assert pushes == [("down", "timeout 1.2.3.4:5000 – confirming (1/2)")]
    assert docker.restarts == []
    assert wd.load_state(cfg.state_file).consecutive_failures == 1


def test_confirmed_timeout_rerolls_and_recovers(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.OPEN]), push)
    assert docker.restarts == ["gluetun", *DEPS]
    assert pushes == [("up", "recovered on 1.2.3.4:5000 after 1 cycle(s)")]
    assert wd.load_state(cfg.state_file) == wd.State(consecutive_failures=0, remediations=[1000.0])


def test_confirmed_refused_restarts_only_qbittorrent(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.REFUSED, wd.OPEN]), push)
    assert docker.restarts == ["qbittorrent"]
    assert pushes[0][0] == "up"


def test_failed_remediation_stays_down(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.TIMEOUT]), push)
    assert pushes == [("down", "timeout 1.2.3.4:5000 – restarting gluetun (cycle 1/6)"
                               " – still timeout on 1.2.3.4:5000 after remediation")]
    assert wd.load_state(cfg.state_file).consecutive_failures == 2


def test_failed_remediation_names_the_new_target_after_reroll(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(reroll_to=("5.6.7.8", "6000"))
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.TIMEOUT]), push)
    assert pushes == [("down", "timeout 1.2.3.4:5000 – restarting gluetun (cycle 1/6)"
                               " – still timeout on 5.6.7.8:6000 after remediation")]


def test_unreadable_gluetun_pushes_down_without_remediating(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    docker = FakeDocker(ip=None)
    assert go(cfg, docker, make_probe([]), push) == 1
    assert pushes[0][0] == "down"
    assert pushes[0][1].startswith("cannot read gluetun state")
    assert docker.restarts == []


def test_unhealthy_gluetun_still_restarts_dependents(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(health="starting")
    clock = itertools.count(1000, 10)
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.TIMEOUT]), push, now=lambda: float(next(clock)))
    assert docker.restarts == ["gluetun", *DEPS]


def test_remediation_error_still_counts_toward_cap(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(fail_restart={"qbittorrent"})
    go(cfg, docker, make_probe([wd.REFUSED]), push)
    assert pushes[0][0] == "down"
    assert "remediation error" in pushes[0][1]
    assert json.load(open(cfg.state_file))["remediations"] == [1000.0]


def test_gluetun_restart_failure_still_restarts_dependents(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(fail_restart={"gluetun"})
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.TIMEOUT]), push)
    assert docker.attempts == ["gluetun", *DEPS]
    assert docker.restarts == list(DEPS)
    assert pushes == [("down", "timeout 1.2.3.4:5000 – restarting gluetun (cycle 1/6)"
                               " – still timeout on 1.2.3.4:5000 after remediation – restart failed: gluetun")]


def test_recovery_message_reports_failed_dependent_restarts(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(fail_restart={"app1"})
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.OPEN]), push)
    assert docker.attempts == ["gluetun", *DEPS]
    assert pushes == [("up", "recovered on 1.2.3.4:5000 after 1 cycle(s) – restart failed: app1")]


def history(cfg):
    try:
        with open(cfg.history_file) as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def test_history_records_entry_exit_port_and_result(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    go(cfg, FakeDocker(), make_probe([wd.OPEN]), push)
    assert history(cfg) == [{"t": 1000.0, "entry": "169.150.208.129", "exit": "1.2.3.4",
                             "port": 5000, "result": "open"}]


def test_history_only_appends_when_something_changes(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    docker = FakeDocker()
    go(cfg, docker, make_probe([wd.OPEN]), push)
    go(cfg, docker, make_probe([wd.OPEN]), push)
    assert len(history(cfg)) == 1
    go(cfg, docker, make_probe([wd.TIMEOUT]), push)
    assert [h["result"] for h in history(cfg)] == ["open", "timeout"]


def test_unreadable_entry_is_recorded_as_none_and_run_continues(tmp_path):
    cfg, pushes, push = setup(tmp_path)
    go(cfg, FakeDocker(entry=None), make_probe([wd.OPEN]), push)
    assert pushes == [("up", "open 1.2.3.4:5000")]
    assert history(cfg)[0]["entry"] is None


def test_history_records_the_new_server_after_a_reroll(tmp_path):
    cfg, pushes, push = setup(tmp_path, wd.State(consecutive_failures=1))
    docker = FakeDocker(reroll_to=("5.6.7.8", "6000"))
    go(cfg, docker, make_probe([wd.TIMEOUT, wd.OPEN]), push)
    assert [(h["exit"], h["port"], h["result"]) for h in history(cfg)] == [
        ("1.2.3.4", 5000, "timeout"), ("5.6.7.8", 6000, "open")]
