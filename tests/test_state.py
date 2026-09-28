import pf_watchdog as wd


def test_missing_file_gives_default(tmp_path):
    assert wd.load_state(str(tmp_path / "state.json")) == wd.State()


def test_round_trip(tmp_path):
    path = str(tmp_path / "state.json")
    wd.save_state(path, wd.State(consecutive_failures=2, remediations=[100.0, 200.0]))
    assert wd.load_state(path) == wd.State(consecutive_failures=2, remediations=[100.0, 200.0])


def test_corrupt_file_gives_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert wd.load_state(str(path)) == wd.State()
