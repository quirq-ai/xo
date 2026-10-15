"""xo serve against a fake Clerk Backend API on 127.0.0.1. SYNTHETIC: no response here is a copy
of a real Clerk answer; shapes follow @clerk/backend's APIKeyJSON and UserJSON."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from xo import cli, server

SECRET = "sk_test_clerk_secret_never_printed"
USER_KEY = "ak_user_secret_never_printed"
ADMIN_KEY = "ak_admin_secret_never_printed"
FORGED_KEY = "ak_forged_secret_never_printed"

KEYS = {
    USER_KEY: {"id": "ak_user", "subject": "user_alice", "claims": None, "revoked": False, "expired": False},
    ADMIN_KEY: {"id": "ak_admin", "subject": "user_suraj", "claims": {"issued_by": "xo", "xo_admin": True},
                "revoked": False, "expired": False},
    # Claims admin but its id is not on XO_ADMIN_KEY_IDS.
    FORGED_KEY: {"id": "ak_forged", "subject": "user_mallory", "claims": {"xo_admin": True},
                 "revoked": False, "expired": False},
    "ak_org": {"id": "ak_org", "subject": "org_1", "claims": None, "revoked": False, "expired": False},
    "ak_revoked": {"id": "ak_rev", "subject": "user_alice", "claims": None, "revoked": True, "expired": False},
    "ak_expired": {"id": "ak_exp", "subject": "user_alice", "claims": None, "revoked": False, "expired": True},
    # On XO_ADMIN_KEY_IDS but without the claim.
    "ak_listed": {"id": "ak_listed", "subject": "user_suraj", "claims": {"issued_by": "xo"},
                  "revoked": False, "expired": False},
}
USERS = {
    "user_alice": {"id": "user_alice", "username": "alice", "first_name": "Alice", "last_name": "A",
                   "image_url": "https://img.example/a", "created_at": 1700000000000,
                   "email_addresses": [{"email_address": "alice@example.com"}],
                   "public_metadata": {"plan": "free"}, "private_metadata": {"stripeId": "cus_1"}},
    "user_suraj": {"id": "user_suraj", "username": "suraj", "public_metadata": {}, "private_metadata": {}},
}


class FakeClerk:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail: int | None = None
        self.redirect: str | None = None  # answer every call with a redirect here
        self.redirect_status = 307
        self.raw: bytes | None = None  # answer every call with these bytes and a 200


@pytest.fixture
def clerk(monkeypatch):
    state = FakeClerk()

    class Handler(server.BaseHTTPRequestHandler):
        def _serve(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.loads(raw) if raw else None
            state.calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                "body": body})
            if state.redirect:
                self.send_response(state.redirect_status)
                self.send_header("Location", state.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, out = self._answer(body)
            data = state.raw if state.raw is not None else json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _answer(self, body):
            if self.headers.get("Authorization") != f"Bearer {SECRET}":
                return 401, {"errors": [{"code": "authentication_invalid"}]}
            if state.fail:
                return state.fail, {"errors": [{"code": "form_param_value_invalid"}]}
            if self.command == "POST" and self.path == "/v1/api_keys/verify":
                key = KEYS.get(body.get("secret"))
                return (200, key) if key else (404, {"errors": [{"code": "resource_not_found"}]})
            parts = self.path.split("/")  # "", v1, users, id[, metadata]
            user = USERS.get(parts[3]) if len(parts) > 3 and parts[2] == "users" else None
            if user is None:
                return 404, {"errors": [{"code": "resource_not_found"}]}
            if self.command == "PATCH" and parts[4:] == ["metadata"]:
                for bucket, changes in body.items():
                    stored = user.setdefault(bucket, {})
                    for k, v in changes.items():
                        stored.pop(k, None) if v is None else stored.__setitem__(k, v)
            return 200, user

        do_GET = do_POST = do_PATCH = _serve

        def log_message(self, *args) -> None:
            pass

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setenv("CLERK_SECRET_KEY", SECRET)
    monkeypatch.setenv("CLERK_API_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("XO_EDITABLE_PUBLIC_KEYS", "plan,beta")
    monkeypatch.setenv("XO_EDITABLE_PRIVATE_KEYS", "stripeId")
    monkeypatch.setenv("XO_ADMIN_KEY_IDS", "ak_admin,ak_listed")
    monkeypatch.setenv("XO_CLERK_PER_SECOND", "1000")  # the budget has its own test
    yield state
    httpd.shutdown()


@pytest.fixture
def xo_server(clerk, capfd):
    """`xo serve` on a free port, with its audit lines collected."""
    lines: list[str] = []
    app = server.App(server.Config(), audit=lines.append)
    httpd = server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.audit = lines
    httpd.app = app
    yield httpd
    httpd.shutdown()
    err = capfd.readouterr().err
    for secret in (SECRET, USER_KEY, ADMIN_KEY, FORGED_KEY):
        assert secret not in err and all(secret not in line for line in lines), "a secret was logged"


def call(srv, method: str, path: str, key: str | None = None, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(srv.url + path, data=data, method=method)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def audits(srv) -> list[dict]:
    return [json.loads(line) for line in srv.audit]


def test_health_needs_no_key_and_no_clerk_call(xo_server, clerk):
    assert call(xo_server, "GET", "/healthz") == (200, {"success": True, "data": {"status": "ok"}})
    assert clerk.calls == []


def test_me_verifies_the_key_and_returns_the_fixed_fields(xo_server, clerk):
    status, out = call(xo_server, "GET", "/me", USER_KEY)
    assert status == 200
    assert out["data"] == {"id": "user_alice", "username": "alice", "firstName": "Alice", "lastName": "A",
                           "imageUrl": "https://img.example/a", "createdAt": 1700000000000}
    verify = clerk.calls[0]
    assert (verify["method"], verify["path"], verify["body"]) == ("POST", "/v1/api_keys/verify", {"secret": USER_KEY})
    sent = {k.lower(): v for k, v in verify["headers"].items()}
    assert sent["clerk-api-version"] == server.CLERK_API_VERSION and sent["authorization"] == f"Bearer {SECRET}"
    assert clerk.calls[1]["path"] == "/v1/users/user_alice"


def test_me_never_returns_email_or_metadata(xo_server):
    out = json.dumps(call(xo_server, "GET", "/me", USER_KEY)[1])
    assert "alice@example.com" not in out and "stripeId" not in out and "plan" not in out


@pytest.mark.parametrize("key, error", [(None, "Missing API key"), ("ak_unknown", "Invalid API key"),
                                        ("ak_revoked", "Invalid API key"), ("ak_expired", "Invalid API key"),
                                        ("ak_org", "API key is not tied to a user")])
def test_bad_keys_get_401(xo_server, key, error):
    assert call(xo_server, "GET", "/me", key) == (401, {"success": False, "error": error})


def test_a_normal_key_cannot_read_metadata_and_is_audited(xo_server, clerk):
    status, out = call(xo_server, "GET", "/user-metadata/user_alice", USER_KEY)
    assert (status, out["error"]) == (403, "Admin key required")
    assert not any(c["path"].startswith("/v1/users") for c in clerk.calls)  # 403 before any lookup
    assert audits(xo_server)[-1] | {"time": ""} == {
        "time": "", "method": "GET", "route": "/user-metadata", "keyId": "ak_user", "caller": "user_alice",
        "target": "user_alice", "outcome": "denied", "claimedAdmin": False}


def test_a_key_claiming_admin_but_not_listed_is_denied_and_flagged(xo_server):
    status, _ = call(xo_server, "PATCH", "/user-metadata/user_alice", FORGED_KEY,
                     body={"publicMetadata": {"plan": "pro"}})
    assert status == 403
    entry = audits(xo_server)[-1]
    assert entry["outcome"] == "denied" and entry["claimedAdmin"] is True and entry["keyId"] == "ak_forged"
    assert USERS["user_alice"]["public_metadata"] == {"plan": "free"}


def test_admin_reads_both_buckets_and_the_read_is_audited(xo_server):
    status, out = call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)
    assert status == 200
    assert out["data"] == {"publicMetadata": {"plan": "free"}, "privateMetadata": {"stripeId": "cus_1"}}
    entry = audits(xo_server)[-1]
    assert (entry["method"], entry["keyId"], entry["target"], entry["outcome"]) == ("GET", "ak_admin", "user_alice", 200)
    assert "cus_1" not in json.dumps(audits(xo_server)) and "free" not in json.dumps(audits(xo_server))


def test_admin_patch_deep_merges_allow_listed_keys(xo_server, clerk):
    status, out = call(xo_server, "PATCH", "/user-metadata/user_suraj", ADMIN_KEY,
                       body={"publicMetadata": {"plan": "pro", "beta": None}, "privateMetadata": {"stripeId": "cus_9"}})
    assert status == 200
    assert out["data"] == {"publicMetadata": {"plan": "pro"}, "privateMetadata": {"stripeId": "cus_9"}}
    patch = clerk.calls[-1]
    assert (patch["method"], patch["path"]) == ("PATCH", "/v1/users/user_suraj/metadata")
    assert patch["body"] == {"public_metadata": {"plan": "pro", "beta": None}, "private_metadata": {"stripeId": "cus_9"}}
    entry = audits(xo_server)[-1]
    assert entry["changed"] == {"publicMetadata": ["beta", "plan"], "privateMetadata": ["stripeId"]}
    assert "cus_9" not in json.dumps(entry)


@pytest.mark.parametrize("body, error", [
    ({"publicMetadata": {"role": "admin"}}, "publicMetadata.role is not editable"),
    ({"privateMetadata": {"plan": "x"}}, "privateMetadata.plan is not editable"),
    ({"unsafeMetadata": {"plan": "x"}}, "Body may only hold publicMetadata and privateMetadata"),
    ({}, "Body must be an object with publicMetadata, privateMetadata or both"),
    ([], "Body must be an object with publicMetadata, privateMetadata or both"),
    ({"publicMetadata": None}, "publicMetadata must be a JSON object"),
    ({"publicMetadata": []}, "publicMetadata must be a JSON object"),
])
def test_patch_refuses_before_calling_clerk(xo_server, clerk, body, error):
    status, out = call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, body=body)
    assert (status, out["error"]) == (422, error)
    assert not any(c["method"] == "PATCH" for c in clerk.calls)
    assert audits(xo_server)[-1]["outcome"] == "refused"


def test_patch_with_bad_json_is_refused(xo_server):
    assert call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, raw=b"{plan: pro}")[0] == 422


@pytest.mark.parametrize("user_id", ["alice", "user_1%3Fx%3D1", "..%2Fapi_keys", "user_" + "a" * 65])
def test_a_bad_user_id_is_refused(xo_server, clerk, user_id):
    status, out = call(xo_server, "GET", f"/user-metadata/{user_id}", ADMIN_KEY)
    assert status == 422 and "not a Clerk user id" in out["error"]
    assert not any(c["path"].startswith("/v1/users") for c in clerk.calls)


def test_auth_comes_before_body_and_id_checks(xo_server):
    assert call(xo_server, "PATCH", "/user-metadata/bad", None, raw=b"junk")[0] == 401
    assert call(xo_server, "PATCH", "/user-metadata/bad", USER_KEY, raw=b"junk")[0] == 403


def test_clerk_errors_map_to_404_and_422(xo_server, clerk):
    assert call(xo_server, "GET", "/user-metadata/user_nobody", ADMIN_KEY)[0] == 404
    assert audits(xo_server)[-1]["outcome"] == 404
    clerk.fail = 422
    clerk_calls = len(clerk.calls)
    # verify also fails while Clerk is failing, so drive the app with a working verify.
    xo_server.app.clerk.verify_key = lambda secret: KEYS[secret]
    status, out = call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, body={"publicMetadata": {"plan": "x"}})
    assert (status, out["error"]) == (422, "Clerk refused the change (each bucket holds at most 8KB)")
    assert len(clerk.calls) == clerk_calls + 1


def test_an_oversized_body_is_refused(xo_server):
    status, out = call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, raw=b" " * (server.MAX_BODY + 1))
    assert status == 413


def test_requests_are_rate_limited_per_key(xo_server, clerk):
    for _ in range(server.LIMITS["user-metadata"]):
        call(xo_server, "GET", "/user-metadata/user_alice", USER_KEY)
    verifies = len([c for c in clerk.calls if c["path"] == "/v1/api_keys/verify"])
    assert call(xo_server, "GET", "/user-metadata/user_alice", USER_KEY)[0] == 429
    assert len([c for c in clerk.calls if c["path"] == "/v1/api_keys/verify"]) == verifies  # Clerk not asked
    assert call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)[0] == 200  # other keys unaffected


def test_unknown_routes_and_methods(xo_server):
    assert call(xo_server, "GET", "/spaces/x", USER_KEY)[0] == 404
    assert call(xo_server, "DELETE", "/user-metadata/user_alice", ADMIN_KEY)[0] == 405


def test_serve_refuses_to_start_without_the_secret(monkeypatch, capsys):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    assert cli.main(["serve"]) == 1
    assert "xo serve needs CLERK_SECRET_KEY" in capsys.readouterr().err


def test_serve_refuses_plain_http_to_another_host(monkeypatch, capsys):
    monkeypatch.setenv("CLERK_SECRET_KEY", SECRET)
    monkeypatch.setenv("CLERK_API_URL", "http://clerk.example")
    assert cli.main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "CLERK_API_URL must be https://" in err and SECRET not in err


def _no_secret(*texts: str) -> None:
    for secret in (SECRET, USER_KEY, ADMIN_KEY, FORGED_KEY):
        assert all(secret not in t for t in texts), "a secret was printed"


def test_the_cli_and_the_server_work_together(xo_server, monkeypatch, capfd):
    monkeypatch.setenv("XO_SERVER_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", ADMIN_KEY)
    assert cli.main(["user-metadata", "update", "--user-id", "user_suraj",
                     "--public-metadata", '{"beta": true}']) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out)["data"]["publicMetadata"]["beta"] is True
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    assert cli.main(["me"]) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out)["data"]["username"] == "alice"


def test_client_mode_never_loads_the_server():
    import subprocess
    import sys
    code = "import sys, xo.cli; xo.cli.main(['ops']); sys.exit('xo.server' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


# --- abuse, Clerk failures and the gaps the audit's mutations found ------------------------------

def test_a_token_that_is_not_a_clerk_key_never_reaches_clerk(xo_server, clerk):
    for token in ("sk_live_xyz", "x" * 10_000, "ak_", "ak_bad!chars", "ak_bad%2Fchars", "ak_" + "a" * 201):
        assert call(xo_server, "GET", "/me", token) == (401, {"success": False, "error": "Invalid API key"})
    assert clerk.calls == []


def test_many_made_up_keys_cannot_flood_clerk(clerk, monkeypatch):  # M1
    monkeypatch.setenv("XO_CLERK_PER_SECOND", "5")
    app = server.App(server.Config(), audit=lambda line: None, clock=lambda: 1000.0)  # time stands still
    answers = [app.handle("GET", "/me", {"authorization": f"Bearer ak_madeup{i}"}, None)[0] for i in range(50)]
    assert len(clerk.calls) == 5
    assert answers.count(401) == 5 and answers.count(503) == 45


def test_the_rate_limit_table_stays_bounded(clerk, monkeypatch):  # M1
    monkeypatch.setattr(server, "MAX_TRACKED", 10)
    app = server.App(server.Config(), audit=lambda line: None)
    for i in range(50):
        app.handle("GET", "/me", {"authorization": f"Bearer ak_madeup{i}"}, None)
    assert len(app._hits) == 10


def test_a_wrong_server_secret_is_a_503_not_a_bad_user_key(xo_server, capfd):  # S1
    xo_server.app.cfg.secret = "sk_test_wrong"
    assert call(xo_server, "GET", "/me", USER_KEY) == (503, {"success": False, "error": server.MISCONFIGURED})
    assert "Clerk refused this server's CLERK_SECRET_KEY" in capfd.readouterr().err


@pytest.mark.parametrize("status, expected", [(429, 503), (500, 502), (403, 503), (400, 401)])
def test_clerk_failures_on_the_key_check(xo_server, clerk, status, expected):  # S1, S6
    clerk.fail = status
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == expected


def test_the_key_check_is_never_cached(xo_server, monkeypatch):  # S6
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == 200
    monkeypatch.setitem(KEYS, USER_KEY, {**KEYS[USER_KEY], "revoked": True})
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == 401


def test_a_listed_id_without_the_claim_is_not_admin(xo_server):  # S6
    assert call(xo_server, "GET", "/user-metadata/user_alice", "ak_listed")[0] == 403


@pytest.mark.parametrize("status", [302, 303, 307])  # urllib's default opener follows 302 and 303
def test_a_redirect_from_clerk_is_not_followed_and_carries_no_secret(xo_server, clerk, fake, status):  # S6
    clerk.redirect, clerk.redirect_status = fake.base + "/stolen", status
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == 502
    assert fake.requests == []


def test_a_clerk_answer_that_is_not_an_object_is_a_502(xo_server, clerk):  # S7
    xo_server.app.clerk.verify_key = lambda secret: KEYS[secret]
    clerk.raw = b"[]"
    status, out = call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)
    assert (status, out["error"]) == (502, "Clerk did not answer")
    assert audits(xo_server)[-1]["outcome"] == 502


def test_refusals_before_auth_are_audited(xo_server):  # S3
    call(xo_server, "GET", "/user-metadata/user_alice", "ak_unknown")
    call(xo_server, "GET", "/user-metadata/user_alice")
    call(xo_server, "DELETE", "/user-metadata/user_alice", ADMIN_KEY)
    call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, raw=b" " * (server.MAX_BODY + 1))
    assert [a["outcome"] for a in audits(xo_server)] == [401, 401, 405, 413]
    assert all(a["keyId"] is None for a in audits(xo_server))


def test_open_connections_are_capped(clerk, monkeypatch):  # S2
    import socket
    monkeypatch.setenv("XO_SERVE_MAX_CONNECTIONS", "2")
    httpd = server.make_server(server.App(server.Config(), audit=lambda line: None), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    slow = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
    for s in slow:
        s.sendall(b"GET /healthz HTTP/1.1\r\n")  # never finished
    import time
    time.sleep(0.2)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as third:
        third.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert third.recv(100).startswith(b"HTTP/1.1 503")
    for s in slow:
        s.close()
    httpd.shutdown()


def _wait_for_free_slot(httpd) -> None:
    """A handler thread releases its slot just after its client sees the connection close."""
    import time
    deadline = time.monotonic() + 3
    while httpd._slots._value < 1:
        assert time.monotonic() < deadline, "a connection slot was never released"
        time.sleep(0.01)


def test_connection_slots_are_released(clerk, monkeypatch):  # S2
    import socket
    monkeypatch.setenv("XO_SERVE_MAX_CONNECTIONS", "1")
    httpd = server.make_server(server.App(server.Config(), audit=lambda line: None), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    for ending in (b"Host: x\r\n\r\n", b"", b"\x00junk\r\n\r\n"):  # answered, closed early, garbage
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\n" + ending)
            if ending:
                s.recv(100)
    for _ in range(3):  # each slot came back, so the one slot is free again
        _wait_for_free_slot(httpd)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(100).startswith(b"HTTP/1.0 200")
    httpd.shutdown()


def test_a_trickling_client_is_cut_off_at_the_deadline(clerk, monkeypatch):  # S2
    import socket
    import time
    monkeypatch.setattr(server, "REQUEST_DEADLINE", 1)
    monkeypatch.setenv("XO_SERVE_MAX_CONNECTIONS", "1")
    httpd = server.make_server(server.App(server.Config(), audit=lambda line: None), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    start = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET /healthz HTTP/1.1\r\n")
        try:
            while time.monotonic() - start < 4:  # one byte every 0.2 s, well inside the read timeout
                s.sendall(b"X")
                time.sleep(0.2)
        except OSError:
            pass
        assert time.monotonic() - start < 3, "the connection outlived its deadline"
    _wait_for_free_slot(httpd)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:  # and the slot is free
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert s.recv(100).startswith(b"HTTP/1.0 200")
    httpd.shutdown()


def test_control_characters_in_the_request_line_are_escaped_in_the_log(xo_server, capfd):
    import socket
    port = xo_server.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET /a\x01b\x1bc HTTP/1.1\r\nHost: x\r\n\r\n")
        s.recv(100)
    err = capfd.readouterr().err
    assert "\\x01" in err and "\\x1b" in err and "\x01" not in err and "\x1b" not in err


def test_a_base64_shaped_key_reaches_clerk(xo_server, clerk, monkeypatch):
    key = "ak_LCWGdaM8mv8K4PC/57IICZQXAeWfCgF30DZaFXHoGn9="  # Clerk's own test fixture
    monkeypatch.setitem(KEYS, key, KEYS[USER_KEY])
    assert call(xo_server, "GET", "/me", key)[0] == 200


def test_a_deeply_nested_body_is_refused_and_audited(xo_server, clerk):
    status, out = call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, raw=b"[" * 30_000)
    assert (status, out["error"]) == (422, "Body is not JSON")
    assert audits(xo_server)[-1]["outcome"] == "refused"


def test_unicode_digits_are_not_numbers(clerk, monkeypatch, capfd):
    application = server.wsgi_app()
    environ_length = "\u00b2"  # "²" passes str.isdigit() but not int()
    import io
    environ = {"REQUEST_METHOD": "PATCH", "PATH_INFO": "/user-metadata/user_alice", "QUERY_STRING": "",
               "wsgi.input": io.BytesIO(b"{}"), "CONTENT_LENGTH": environ_length,
               "HTTP_AUTHORIZATION": f"Bearer {ADMIN_KEY}"}
    seen = {}
    b"".join(application(environ, lambda status, headers: seen.setdefault("status", status)))
    assert seen["status"].startswith("413")
    monkeypatch.setenv("XO_CLERK_PER_SECOND", "\u00b2")
    with pytest.raises(server.XoError, match="XO_CLERK_PER_SECOND must be a whole number"):
        server.Config()


def test_the_startup_line_never_shows_the_secret(clerk, monkeypatch, capfd):  # S6
    class Stub:
        server_address = ("127.0.0.1", 8787)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(server, "make_server", lambda app, host, port: Stub())
    assert server.serve("0.0.0.0", 8787) == 0
    err = capfd.readouterr().err
    assert "listening on" in err and "behind HTTPS" in err and SECRET not in err


# --- Vercel: the same server as a WSGI app (root app.py) -----------------------------------------

def wsgi_call(application, method: str, path: str, key: str | None = None, body: bytes | None = None):
    import io
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "wsgi.input": io.BytesIO(body or b""), "CONTENT_LENGTH": str(len(body)) if body else ""}
    if key:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {key}"
    seen = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)
    out = b"".join(application(environ, start_response))
    return int(seen["status"].split()[0]), json.loads(out), seen["headers"]


def test_wsgi_serves_the_same_routes(clerk, capfd):
    application = server.wsgi_app()
    assert wsgi_call(application, "GET", "/healthz")[:2] == (200, {"success": True, "data": {"status": "ok"}})
    status, out, headers = wsgi_call(application, "GET", "/me", USER_KEY)
    assert status == 200 and out["data"]["username"] == "alice"
    assert headers["Cache-Control"] == "no-store"
    assert wsgi_call(application, "GET", "/user-metadata/user_alice", USER_KEY)[0] == 403
    assert wsgi_call(application, "PATCH", "/user-metadata/user_alice", ADMIN_KEY,
                     body=b" " * (server.MAX_BODY + 1))[0] == 413
    _no_secret(*capfd.readouterr())


def test_wsgi_without_the_secret_answers_503_and_says_why(monkeypatch, capfd):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    application = server.wsgi_app()  # building the app must not fail the import
    status, out, _ = wsgi_call(application, "GET", "/healthz")
    assert (status, out["error"]) == (503, server.MISCONFIGURED)
    assert "xo serve needs CLERK_SECRET_KEY" in capfd.readouterr().err


def test_vercel_entrypoint_exposes_a_wsgi_app(monkeypatch):
    import pathlib
    import runpy
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    module = runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent / "app.py"))
    assert callable(module["app"])
