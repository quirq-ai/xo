"""Auto-update (src/xo/selfupdate.py): off without setup's marker, a background check at most once a
day, the update first on the next run, and never a hand-installed xo."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from xo import cli, selfupdate
from xo.client import XoError, get_pins

A = "a" * 40
B = "b" * 40
PINS = {"schema": "qq-setup-pins/1", "xo": B, "qqsync": "c" * 40}


@pytest.fixture
def auto(monkeypatch):
    """Auto-update on, xo installed by setup at A, and nothing really spawned or installed."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(selfupdate.RAN_ENV, raising=False)
    path = selfupdate.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(A + "\n")
    monkeypatch.setattr(selfupdate, "installed_commit", lambda: A)
    monkeypatch.setattr(selfupdate, "WINDOWS", False)  # the update-first path; Windows' own test sets it
    calls = {"spawned": [], "installed": [], "rerun": []}

    def popen(cmd, **kwargs):
        assert kwargs["cwd"] == selfupdate.state_path().parent
        calls["spawned"].append(cmd)
    monkeypatch.setattr(selfupdate.subprocess, "Popen", popen)

    def install(pin):
        calls["installed"].append(pin)
        return calls.get("install_ok", True)
    monkeypatch.setattr(selfupdate, "install", install)

    def rerun(argv):
        calls["rerun"].append(argv)
        return 7
    monkeypatch.setattr(selfupdate, "_rerun", rerun)
    return calls


def state(**kw):
    selfupdate.state_path().write_text(json.dumps(kw))


def test_off_without_the_marker(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(selfupdate.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert selfupdate.before(["list"]) is None
    assert not selfupdate.state_path().exists()


def test_ci_and_the_rerun_never_update(auto, monkeypatch):
    state(pin=B, pin_at=time.time() + 5)
    monkeypatch.setenv("CI", "true")
    assert selfupdate.before(["list"]) is None
    monkeypatch.delenv("CI")
    monkeypatch.setenv(selfupdate.RAN_ENV, "1")
    assert selfupdate.before(["list"]) is None
    assert auto["installed"] == [] and auto["spawned"] == []


def test_a_hand_installed_xo_is_left_alone(auto, monkeypatch):
    monkeypatch.setattr(selfupdate, "installed_commit", lambda: "d" * 40)
    state(pin=B, pin_at=time.time() + 5)
    assert selfupdate.before(["list"]) is None
    assert auto["installed"] == [] and auto["spawned"] == []


def test_the_check_runs_in_the_background_once_a_day(auto):
    assert selfupdate.before(["list"]) is None
    assert auto["spawned"] and auto["spawned"][0][1:] == ["-I", "-m", "xo.selfupdate"], "isolated from the cwd"
    assert selfupdate.before(["list"]) is None
    assert len(auto["spawned"]) == 1, "the second command must not start another check"
    assert auto["installed"] == []


def test_a_newer_pin_updates_first_then_runs_the_command(auto, capsys):
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    assert selfupdate.before(["list", "spaces"]) == 7
    assert auto["installed"] == [B] and auto["rerun"] == [["list", "spaces"]]
    assert selfupdate.marker_path().read_text().strip() == B
    assert "updating to bbbbbbb" in capsys.readouterr().err


def test_a_pin_older_than_the_marker_is_ignored(auto):
    """setup's Update wrote the marker after the last check: that check may be behind it."""
    state(pin=B, pin_at=time.time() - 60, checked=time.time())
    assert selfupdate.before(["list"]) is None
    assert auto["installed"] == []


def test_a_failed_update_runs_this_xo_and_waits_an_hour(auto, capsys):
    auto["install_ok"] = False
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    assert selfupdate.before(["list"]) is None
    assert "could not update" in capsys.readouterr().err
    assert selfupdate.before(["list"]) is None
    assert auto["installed"] == [B], "no second try within the hour"
    assert selfupdate.marker_path().read_text().strip() == A


def test_a_broken_state_file_never_stops_a_command(auto):
    selfupdate.state_path().write_text("{not json")
    assert selfupdate.before(["list"]) is None


def test_pins_check_only_xos_own_fields():
    assert selfupdate.pin_from({**PINS, "qq": "e" * 40, "later": {"x": 1}}) == B
    for bad in (None, [], {"xo": B}, {**PINS, "schema": "qq-setup-pins/2"}, {**PINS, "xo": B[:7]},
                {**PINS, "xo": B.upper()}, {**PINS, "xo": None}):
        with pytest.raises(XoError):
            selfupdate.pin_from(bad)


def test_the_check_records_the_pin_or_retries_in_an_hour(monkeypatch):
    monkeypatch.setattr(selfupdate, "get_pins", lambda url, timeout: PINS)
    selfupdate.check()
    s = json.loads(selfupdate.state_path().read_text())
    assert s["pin"] == B and s["pin_at"] == s["checked"]

    def down(url, timeout):
        raise XoError("pins.json could not be reached")
    monkeypatch.setattr(selfupdate, "get_pins", down)
    selfupdate.check()
    s = json.loads(selfupdate.state_path().read_text())
    assert s["pin"] == B, "a failed check keeps the last pin"
    assert time.time() - s["checked"] == pytest.approx(selfupdate.DAY - selfupdate.RETRY, abs=60)
    assert "could not be reached" in selfupdate.log_path().read_text()


def test_pins_are_read_over_https_only():
    assert selfupdate.PINS_URL.startswith("https://raw.githubusercontent.com/quirq-ai/setup/refs/heads/main/")
    with pytest.raises(XoError, match="not https"):
        get_pins("http://127.0.0.1:1/pins.json", 1)


def test_install_runs_setups_update_at_the_pin(monkeypatch):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(selfupdate.subprocess, "run", run)
    assert selfupdate.install(B)
    assert seen[0][1:] == ["-I", "-m", "pip", "install", "--force-reinstall", "--no-deps", "--quiet",
                           "--disable-pip-version-check", f"quirq-xo @ git+https://github.com/quirq-ai/xo@{B}"]


def test_installed_commit_reads_pips_record(monkeypatch):
    class Dist:
        def __init__(self, text):
            self.text = text

        def read_text(self, name):
            return self.text
    good = {"url": "https://github.com/quirq-ai/xo", "vcs_info": {"vcs": "git", "commit_id": A}}
    for data, want in ((good, A), ({**good, "url": "https://github.com/quirq-ai/xo.git"}, A),
                       ({**good, "url": "https://example.com/quirq-ai/xo"}, None),
                       ({"url": "file:///src/xo", "dir_info": {"editable": True}}, None)):
        monkeypatch.setattr(selfupdate.metadata, "distribution", lambda name, d=data: Dist(json.dumps(d)))
        assert selfupdate.installed_commit() == want


def test_version_names_the_commit(monkeypatch, capsys):
    monkeypatch.setattr(selfupdate, "installed_commit", lambda: A)
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == "xo 0.1.0 (aaaaaaa)"


def test_a_command_updates_first(auto, monkeypatch):
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    assert cli.main(["status"]) == 7
    assert auto["rerun"] == [["status"]]


def test_serve_never_updates(auto, monkeypatch):
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    monkeypatch.setattr(cli, "_serve", lambda args: 0, raising=False)
    monkeypatch.setattr(selfupdate, "before", lambda argv: pytest.fail("serve must not update"))
    try:
        cli.main(["serve", "--port", "0"])
    except SystemExit:
        pass


def test_windows_defers_the_update_until_xo_exits(auto, monkeypatch, capsys):
    """A running xo.exe cannot be replaced: a detached helper updates later, and this xo runs."""
    monkeypatch.setattr(selfupdate, "WINDOWS", True)
    deferred = []
    monkeypatch.setattr(selfupdate, "_defer", lambda pin, marker: deferred.append((pin, marker)))
    state(pin=B, pin_at=time.time() + 5, checked=time.time(), pending=B, pending_at=time.time() - 2 * 3600)
    assert selfupdate.before(["list"]) is None
    assert deferred == [(B, A)] and auto["installed"] == [] and auto["rerun"] == []
    assert "once this command ends" in capsys.readouterr().err
    state(pin=B, pin_at=time.time() + 5, checked=time.time(), pending=B, pending_at=time.time())
    assert selfupdate.before(["list"]) is None
    assert len(deferred) == 1, "one helper per pin within the hour"


def test_the_helper_waits_for_xo_exe_then_updates(auto, monkeypatch, tmp_path):
    monkeypatch.setattr(selfupdate, "_scripts", lambda: tmp_path)
    frees = iter([False, False, True])
    monkeypatch.setattr(selfupdate, "_exe_free", lambda exe: next(frees))
    monkeypatch.setattr(selfupdate.time, "sleep", lambda s: None)
    assert selfupdate.install_when_free(B, A) == 0
    assert auto["installed"] == [B] and selfupdate.marker_path().read_text().strip() == B


def test_the_helper_leaves_a_changed_xo_and_gives_up_in_time(auto, monkeypatch, tmp_path):
    monkeypatch.setattr(selfupdate, "_scripts", lambda: tmp_path)
    monkeypatch.setattr(selfupdate, "_exe_free", lambda exe: True)
    assert selfupdate.install_when_free(B, "d" * 40) == 0 and auto["installed"] == []
    monkeypatch.setattr(selfupdate, "_exe_free", lambda exe: False)
    monkeypatch.setattr(selfupdate.time, "sleep", lambda s: None)
    assert selfupdate.install_when_free(B, A, wait=0) == 1 and auto["installed"] == []
    assert json.loads(selfupdate.state_path().read_text())["failed"] == B


def test_the_helper_takes_only_full_commits():
    assert selfupdate._main(["--install", "x", "--from", A]) == 0  # not an install: falls back to the check


@pytest.mark.skipif(os.name != "nt", reason="only Windows locks a running .exe")
def test_exe_free_sees_a_running_exe(tmp_path):
    import shutil
    exe = tmp_path / "busy.exe"
    shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "PING.EXE"), exe)
    p = subprocess.Popen([str(exe), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(1)
        assert not selfupdate._exe_free(exe)
    finally:
        p.kill()
        p.wait()
    assert selfupdate._exe_free(exe)


def test_one_update_at_a_time(auto):
    """A command that finds the lock held runs on the xo it has, and updates nothing."""
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    with selfupdate._locked() as mine:
        assert mine
        with selfupdate._locked() as other:
            assert not other
        if os.name != "nt":  # flock locks are per open file, so a second open in this process is refused too
            assert selfupdate.before(["list"]) is None
            assert auto["installed"] == [] and auto["rerun"] == []
    assert selfupdate.before(["list"]) == 7 and auto["installed"] == [B]


def test_an_update_another_xo_finished_is_not_repeated(auto, monkeypatch):
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    seen = iter([A, B])  # A before the lock, B once it is held
    monkeypatch.setattr(selfupdate, "installed_commit", lambda: next(seen))
    assert selfupdate.before(["list"]) == 7
    assert auto["installed"] == [] and auto["rerun"] == [["list"]]


def test_the_rerun_starts_the_venvs_xo_script(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(selfupdate, "_scripts", lambda: tmp_path)
    monkeypatch.setattr(selfupdate.os, "execve", lambda path, argv, env: seen.append((path, argv, env)))
    monkeypatch.setattr(selfupdate.subprocess, "run", lambda cmd, env: seen.append((cmd[0], cmd, env))
                        or subprocess.CompletedProcess(cmd, 0))
    selfupdate._rerun(["list", "spaces"])
    path, argv, env = seen[0]
    exe = tmp_path / ("xo.exe" if os.name == "nt" else "xo")
    assert str(path) == str(exe) and argv == [str(exe), "list", "spaces"] and env[selfupdate.RAN_ENV] == "1"


def test_an_unwritable_marker_still_runs_the_updated_xo(auto, monkeypatch):
    state(pin=B, pin_at=time.time() + 5, checked=time.time())
    real = selfupdate._write

    def write(path, text):
        if path == selfupdate.marker_path():
            raise PermissionError("read-only")
        real(path, text)
    monkeypatch.setattr(selfupdate, "_write", write)
    assert selfupdate.before(["list"]) == 7 and auto["installed"] == [B]
    assert "could not write" in selfupdate.log_path().read_text()
