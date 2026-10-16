"""A fake XO API on 127.0.0.1: tests never reach the real one."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "ak_test_secret_never_printed"


class FakeXo:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        # path -> (status, body or None, extra headers)
        self.routes: dict[tuple[str, str], tuple[int, object, dict]] = {}
        self.strip = True  # match core API paths without /api/core/v1 (the `core` fixture turns it off)

    def route(self, method: str, path: str, status: int = 200, body: object = None,
              headers: dict | None = None) -> None:
        self.routes[(method, path)] = (status, body, headers or {})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    """Every test body runs with the loopback guard on, whatever its module overrides."""
    import socket
    import urllib.request
    assert getattr(socket.getaddrinfo, "__name__", "") == "guard" and urllib.request.getproxies() == {}, (
        f"{item.nodeid}: the loopback guard is not active (is loopback_only overridden?)")
    import subprocess
    from xo import github
    assert github.subprocess is not subprocess, f"{item.nodeid}: the real gh could run (is no_real_gh overridden?)"
    assert not _coder_dirs(), f"{item.nodeid}: a real coder is on PATH (is no_real_coder overridden?)"


@pytest.fixture(autouse=True)
def loopback_only():
    """No test may reach a real host: anything but loopback fails to resolve, and no proxy is used
    (a proxy on loopback would carry the request out). It has its own MonkeyPatch, so a test's
    monkeypatch.undo() cannot remove it."""
    import socket
    import urllib.request
    real = socket.getaddrinfo

    def guard(host, *args, **kwargs):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise socket.gaierror(socket.EAI_NONAME, f"tests reach loopback only, not {host}")
        return real(host, *args, **kwargs)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket, "getaddrinfo", guard)
        mp.setattr(urllib.request, "getproxies", dict)
        yield


@pytest.fixture(autouse=True)
def no_real_gh():
    """No test runs the real gh or sees a GitHub token from the machine: xo.github's subprocess
    answers as if gh were not installed, unless a test puts a fake in its place. It has its own
    MonkeyPatch, so a test's monkeypatch.undo() cannot remove it."""
    import subprocess
    import types
    from xo import github

    def run(*args, **kwargs):
        raise FileNotFoundError("gh")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(github, "installed", lambda: False)
        mp.setattr(github, "subprocess", types.SimpleNamespace(
            run=run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess))
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"):
            mp.delenv(name, raising=False)
        yield


_BASETEMP = None  # pytest's temp root, where every tmp_path lives; set by no_real_coder


def _coder_dirs(path=None, base=None):
    """The PATH directories holding a coder that is not a test's own fake (those live under pytest's
    temp root, not merely anywhere in /tmp). An empty entry is the current folder, as in POSIX."""
    import os
    base = os.path.realpath(base or _BASETEMP)
    dirs = (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    return [d for d in dirs if os.access(os.path.join(d or os.curdir, "coder"), os.X_OK)
            and not os.path.realpath(d or os.curdir).startswith(base + os.sep)]


@pytest.fixture(autouse=True)
def no_real_coder(tmp_path_factory):
    """No test runs the real Coder CLI, which keeps a session token and sends it to any --url: the
    PATH directories that hold one are dropped. A test may still put its own fake coder on PATH."""
    import os
    global _BASETEMP
    _BASETEMP = str(tmp_path_factory.getbasetemp())
    with pytest.MonkeyPatch.context() as mp:
        path = os.environ.get("PATH", "")
        drop = set(_coder_dirs(path))
        mp.setenv("PATH", os.pathsep.join(d for d in path.split(os.pathsep) if d not in drop))
        mp.delenv("CODER_SESSION_TOKEN", raising=False)
        yield


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Every test gets its own empty config folder, so a key saved by xo login on this machine is
    never read, and no test writes to the real one."""
    home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def fake(monkeypatch):
    state = FakeXo()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            path, _, query = self.path.partition("?")
            if state.strip and path.startswith("/api/core/v1/"):
                path = path[len("/api/core/v1"):]  # the test spec's routes sit at the root
            state.requests.append({"method": self.command, "path": path, "query": query,
                                   "headers": dict(self.headers),
                                   "body": json.loads(raw) if raw else None})
            status, body, headers = state.routes.get((self.command, path), (404, {"detail": "Not Found"}, {}))
            if callable(body):
                body = body(state.requests[-1])
                if isinstance(body, tuple):  # (status, body): the answer depends on the request
                    status, body = body
            data = b"" if body is None else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
            try:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up (a timeout test); a traceback here would land in a later test

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        def log_message(self, *args) -> None:  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("XO_URL", state.base)
    monkeypatch.setenv("XO_API_KEY", KEY)
    monkeypatch.setenv("XO_SPEC", str(FIXTURES / "openapi.json"))
    yield state
    server.shutdown()


@pytest.fixture(autouse=True)
def no_real_github():
    """No test clones from GitHub: xo's git points at a closed loopback port unless a test points it
    at a local repository. Its own MonkeyPatch, so a test's monkeypatch.undo() cannot remove it."""
    from xo import gitops
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gitops, "GITHUB", "https://127.0.0.1:9/no-github-in-tests")
        yield
