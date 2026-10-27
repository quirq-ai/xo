"""A fake XO API on 127.0.0.1: tests never reach the real one."""
from __future__ import annotations

import json
import os
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
    from xo import attach, github

    def run(*args, **kwargs):
        raise FileNotFoundError("gh")
    stub = types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired,
                                 CompletedProcess=subprocess.CompletedProcess)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(github, "installed", lambda: False)
        mp.setattr(github, "subprocess", stub)
        # xo gh / xo sh run their tools through attach: no real gh or spacehub either.
        mp.setattr(attach, "subprocess", stub)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST"):
            mp.delenv(name, raising=False)
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


def git(*args, cwd=None) -> str:
    """The test's own git, for setting up repositories (never xo's)."""
    import subprocess
    # The user's own git config stays out (a contributor who signs commits must still pass).
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class Hub:
    """A local stand-in for github.com: bare OWNER/REPO repositories under one folder, each with a
    seed checkout to make commits, branches and tags from."""

    def __init__(self, root: Path, seeds: Path) -> None:
        self.root, self.seeds = root, seeds

    def repo(self, name: str) -> Path:
        seed = self.seeds / name
        seed.mkdir(parents=True)
        git("init", "-q", "-b", "main", cwd=seed)
        (seed / "README").write_text("hi\n")
        git("add", "README", cwd=seed)
        git("commit", "-q", "-m", "first", cwd=seed)
        git("tag", "v1", cwd=seed)
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", "--bare", str(seed), str(self.root / name))
        git("remote", "add", "origin", str(self.root / name), cwd=seed)
        return seed

    def commit(self, name: str, branch: str, text: str, tag: str | None = None) -> str:
        """A commit on branch (made if new), pushed, with an optional tag; its sha."""
        seed = self.seeds / name
        git("switch", "-q", "-C", branch, cwd=seed) if branch != git("branch", "--show-current", cwd=seed) else None
        (seed / "README").write_text(text)
        git("commit", "-q", "-am", text, cwd=seed)
        git("push", "-q", "origin", branch, cwd=seed)
        if tag:
            git("tag", tag, cwd=seed)
            git("push", "-q", "origin", tag, cwd=seed)
        return git("rev-parse", "HEAD", cwd=seed)


@pytest.fixture
def github(tmp_path, monkeypatch):
    """xo's git pointed at a Hub over file://, working in an empty folder."""
    from xo import gitops
    hub = Hub(tmp_path / "github", tmp_path / "seeds")
    monkeypatch.setattr(gitops, "GITHUB", f"file://{hub.root}")
    monkeypatch.setattr(gitops, "SAFE", [a.replace("protocol.allow=never", "protocol.file.allow=always")
                                          for a in gitops.SAFE])
    monkeypatch.setattr(gitops, "ALLOWED_PROTOCOLS", "https:file")
    work = tmp_path / "here"
    work.mkdir()
    monkeypatch.chdir(work)
    # xo's own git runs with no user or system config either, so signing or hooks there change nothing.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    return hub
