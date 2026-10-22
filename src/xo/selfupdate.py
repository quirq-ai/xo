"""Auto-update: keep an xo that qq-setup installed at setup's newest reviewed commit.

Off unless qq-setup's "keep qq and xo up to date" option wrote the marker file (`auto-update` next
to xo login's key), which holds the one commit setup installed. Then, before a command:

- When an earlier check found a newer reviewed commit, xo updates to it first (the same
  `pip install --force-reinstall --no-deps` setup runs), then runs the command on the new xo.
- At most once a day it starts a check in the background: a detached process reads setup's
  pins.json and records the commit. The command never waits on the network, so it works offline.

An xo installed any other way (by hand at another commit, or from a checkout) is left alone: xo
updates only while it is still at the commit the marker names, and the next Update in setup
writes the marker again. Nothing runs when CI is set. "Newest" is whatever setup's main says: if
setup moves the pin back (a revert), xo follows it back.

Every Python this starts runs isolated (-I) from the config folder, and the re-run starts the venv's
own xo script, so no file in the folder you ran xo from is ever imported. One update at a time:
an update holds auto-update.lock, and a command that finds it held runs on the xo it has.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

from xo.client import XoError, credentials_path, get_pins

PINS_URL = "https://raw.githubusercontent.com/quirq-ai/setup/refs/heads/main/pins.json"
PINS_SCHEMA = "qq-setup-pins/1"
REPO = "https://github.com/quirq-ai/xo"
DIST = "quirq-xo"
COMMIT = re.compile(r"[0-9a-f]{40}")
DAY = 24 * 3600
RETRY = 3600  # after a check or an update that failed
CHECK_TIMEOUT = 10.0  # in the background only: no command waits for it
PIP_TIMEOUT = 300.0
RAN_ENV = "XO_AUTO_UPDATE_RAN"  # set on the re-run after an update, so it never loops
MAX_LOG = 64 * 1024


def marker_path() -> Path:
    return credentials_path().with_name("auto-update")


def state_path() -> Path:
    return credentials_path().with_name("auto-update.json")


def log_path() -> Path:
    return credentials_path().with_name("auto-update.log")


def lock_path() -> Path:
    return credentials_path().with_name("auto-update.lock")


@contextlib.contextmanager
def _locked():
    """Yield True while this process holds the update lock, False when another one does."""
    path = lock_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True  # the lock goes with the file descriptor, closed below
    finally:
        os.close(fd)


def installed_commit() -> str | None:
    """The commit pip installed this xo from, when it came from quirq-ai/xo by git; else None."""
    importlib.invalidate_caches()  # another process may have reinstalled xo since this one started
    try:
        text = metadata.distribution(DIST).read_text("direct_url.json")
        data = json.loads(text or "")
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or str(data.get("url", "")).removesuffix(".git") != REPO:
        return None
    vcs = data.get("vcs_info")
    commit = vcs.get("commit_id") if isinstance(vcs, dict) and vcs.get("vcs") == "git" else None
    return commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None


def pin_from(data: object) -> str:
    """xo's commit in setup's pins.json. Only xo's own fields are checked: the file may gain
    other tools' pins, and an installed xo must keep working when it does."""
    if not isinstance(data, dict) or data.get("schema") != PINS_SCHEMA:
        raise XoError(f"pins.json is not {PINS_SCHEMA}")
    pin = data.get("xo")
    if not isinstance(pin, str) or not COMMIT.fullmatch(pin):
        raise XoError("pins.json has no full commit for xo")
    return pin


def _read_marker() -> tuple[str, float] | None:
    """The commit setup (or the last auto-update) installed, and when it was written."""
    path = marker_path()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200].strip()
        written = path.stat().st_mtime
    except OSError:
        return None
    return (text, written) if COMMIT.fullmatch(text) else None


def _read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, text: str) -> None:
    """Swap `text` into `path` whole, so a concurrent reader never sees half of it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _save_state(state: dict) -> None:
    try:
        _write(state_path(), json.dumps(state) + "\n")
    except OSError:
        pass


def _log(line: str) -> None:
    path = log_path()
    try:
        if path.stat().st_size > MAX_LOG:
            path.unlink()
    except OSError:
        pass
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")
    except OSError:
        pass


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def check() -> int:
    """The background check: read setup's pins.json and record xo's commit. Never raises."""
    state = _read_state()
    try:
        state["pin"] = pin_from(get_pins(PINS_URL, CHECK_TIMEOUT))
        state["checked"] = state["pin_at"] = time.time()
        _log(f"check: setup's reviewed xo is {state['pin']}")
    except XoError as e:
        state["checked"] = time.time() - DAY + RETRY  # try again in an hour
        _log(f"check failed: {e}")
    _save_state(state)
    return 0


def _start_check(state: dict) -> None:
    """Record the check as started (so other commands do not start one too), then run it detached."""
    state["checked"] = time.time()
    _save_state(state)
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-I", "-m", "xo.selfupdate"], cwd=state_path().parent, **kwargs)
    except OSError as e:
        _log(f"could not start the check: {e}")


def _scripts() -> Path:
    return Path(sys.executable).parent


def _clean_old() -> None:
    """Delete the xo.exe copies an earlier update moved aside (Windows only; one still running stays)."""
    if os.name != "nt":
        return
    for old in _scripts().glob("xo.exe.*.old"):
        try:
            old.unlink()
        except OSError:
            pass


def install(pin: str) -> bool:
    """pip-install xo at `pin` into this venv. On Windows the running xo.exe cannot be overwritten
    but can be renamed, so it is moved aside first and put back if pip fails."""
    exe = _scripts() / "xo.exe"
    aside = exe.with_name(f"xo.exe.{os.getpid()}.old")
    moved = False
    if os.name == "nt" and exe.exists():
        try:
            os.replace(exe, aside)
            moved = True
        except OSError as e:
            _log(f"update to {pin}: could not move xo.exe aside: {e}")
            return False
    cmd = [sys.executable, "-I", "-m", "pip", "install", "--force-reinstall", "--no-deps", "--quiet",
           "--disable-pip-version-check", f"{DIST} @ git+{REPO}@{pin}"]
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=PIP_TIMEOUT, cwd=state_path().parent)
        ok, detail = done.returncode == 0, (done.stderr or done.stdout).strip()[-2000:]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, detail = False, str(e)
    if not ok and moved and not exe.exists():
        try:
            os.replace(aside, exe)
        except OSError:
            pass
    _log(f"update to {pin}: {'done' if ok else 'failed'}" + (f"\n{detail}" if detail and not ok else ""))
    return ok


def _rerun(argv: list[str]) -> int:
    """Run the same command again on the xo just installed: the venv's own xo script, by path."""
    env = {**os.environ, RAN_ENV: "1"}
    script = _scripts() / ("xo.exe" if os.name == "nt" else "xo")
    cmd = [str(script), *argv]
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name != "nt":
        os.execve(script, cmd, env)
    try:
        return subprocess.run(cmd, env=env).returncode
    except KeyboardInterrupt:
        return 130


def before(argv: list[str]) -> int | None:
    """Run before a command. None: go on with this xo. A number: the command already ran (on the
    updated xo) and this is its exit code. Never raises: a problem here only skips the update."""
    if os.environ.get(RAN_ENV) or os.environ.get("CI"):
        return None
    found = _read_marker()
    if found is None:
        return None
    marker, written = found
    try:
        _clean_old()
        if installed_commit() != marker:
            return None  # installed another way since setup: leave it until setup's Update
        state = _read_state()
        pin = state.get("pin")
        failed_at = _number(state.get("failed_at"))
        # Only a pin read after the marker was written: an older one may be behind what setup installed since.
        if (isinstance(pin, str) and COMMIT.fullmatch(pin) and pin != marker and _number(state.get("pin_at")) > written
                and not (state.get("failed") == pin and time.time() - failed_at < RETRY)):
            with _locked() as mine:
                now = installed_commit() if mine else None
                if now == pin:
                    ok = True  # another xo updated it since this one started
                elif now == marker:
                    print(f"xo: updating to {pin[:7]}, setup's newest reviewed xo (auto-update is on)",
                          file=sys.stderr)
                    if install(pin):
                        _write(marker_path(), pin + "\n")
                        state = _read_state()
                        state.pop("failed", None)
                        state.pop("failed_at", None)
                        _save_state(state)
                        ok = True
                    else:
                        state = _read_state()
                        state["failed"], state["failed_at"] = pin, time.time()
                        _save_state(state)
                        print(f"xo: could not update (details in {log_path()}); running this xo, "
                              "will try again in an hour", file=sys.stderr)
                        ok = False
                else:
                    ok = False  # another xo is updating (lock held) or xo changed meanwhile: run this one
            if ok:
                return _rerun(argv)
        if time.time() - _number(state.get("checked")) >= DAY:
            _start_check(state)
    except Exception as e:  # noqa: BLE001 - auto-update never stops a command
        _log(f"skipped: {type(e).__name__}: {e}")
    return None


if __name__ == "__main__":
    sys.exit(check())
