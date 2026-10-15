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

    def route(self, method: str, path: str, status: int = 200, body: object = None,
              headers: dict | None = None) -> None:
        self.routes[(method, path)] = (status, body, headers or {})


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
            state.requests.append({"method": self.command, "path": path, "query": query,
                                   "headers": dict(self.headers),
                                   "body": json.loads(raw) if raw else None})
            status, body, headers = state.routes.get((self.command, path), (404, {"detail": "Not Found"}, {}))
            if callable(body):
                body = body(state.requests[-1])
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
    monkeypatch.setenv("XO_BASE_URL", state.base)
    monkeypatch.setenv("XO_API_KEY", KEY)
    monkeypatch.setenv("XO_SPEC", str(FIXTURES / "openapi.json"))
    yield state
    server.shutdown()
