"""xo serve against a fake Clerk Backend API on 127.0.0.1. SYNTHETIC: no response here is a copy
of a real Clerk answer; shapes follow @clerk/backend's APIKeyJSON and UserJSON."""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from xo import cli, server, services

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
                   "primary_email_address_id": "idn_2",
                   "email_addresses": [{"id": "idn_1", "email_address": "old@example.com"},
                                       {"id": "idn_2", "email_address": "alice@example.com"}],
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
        # The fake core API (XO_CORE_API_URL is under /core here): every request it got, and what it
        # answers: (status, body bytes, extra headers), or a number of seconds to stall first.
        self.core: list[dict] = []
        self.core_answer: tuple[int, bytes, dict] = (200, b'{"success": true, "data": {"id": "space_1"}}', {})
        self.core_stall = 0.0
        self.core_trickle = 0.0  # seconds between the answer's bytes, after its headers
        # Clerk's Frontend API for browser login (XO_CLERK_FRONTEND_URL is under /fapi here): every
        # request, and the code it takes, the user who signed in, and what it answers otherwise.
        self.fapi: list[dict] = []
        self.codes = {"code_1": "user_alice"}  # code -> who signed in (each works once)
        self.token_answer: tuple[int, dict] | None = None
        self.userinfo_answer: tuple[int, dict] | None = None
        self.mint_answer: tuple[int, object] | None = None
        self.minted: list[dict] = []
        self.user_keys: list = []  # GET /v1/api_keys?subject=: the keys the user already has
        self.secrets: dict[str, object] = {}  # key id -> what GET /v1/api_keys/{id}/secret answers


@pytest.fixture
def clerk(monkeypatch):
    state = FakeClerk()

    class Handler(server.BaseHTTPRequestHandler):
        def _serve(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.startswith("/core/"):
                state.core.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                   "body": raw})
                if state.core_stall:
                    import time
                    time.sleep(state.core_stall)
                status, data, extra = state.core_answer
                try:
                    self.send_response(status)
                    for k, v in extra.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if state.core_trickle:
                        import time
                        for byte in data:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(state.core_trickle)
                    else:
                        self.wfile.write(data)
                except OSError:
                    pass
                return
            if self.path.startswith("/fapi/"):
                status, out = self._frontend(raw)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
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

        def _frontend(self, raw):
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            state.fapi.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "form": form})
            if self.command == "POST" and self.path == "/fapi/oauth/token":
                if state.token_answer:
                    return state.token_answer
                who = state.codes.pop(form.get("code"), None)
                if (who is None or form.get("grant_type") != "authorization_code" or form.get("client_id") != CLIENT_ID
                        or form.get("redirect_uri") != REDIRECT or form.get("code_verifier") != VERIFIER):
                    return 400, {"error": "invalid_grant"}
                return 200, {"access_token": "oat_" + who, "token_type": "Bearer"}
            if self.command == "GET" and self.path == "/fapi/oauth/userinfo":
                if state.userinfo_answer:
                    return state.userinfo_answer
                token = self.headers.get("Authorization", "")
                if not token.startswith("Bearer oat_"):
                    return 401, {"error": "invalid_token"}
                return 200, {"sub": token[len("Bearer oat_"):], "email": "alice@example.com"}
            return 404, {"error": "not_found"}

        def _answer(self, body):
            if self.headers.get("Authorization") != f"Bearer {SECRET}":
                return 401, {"errors": [{"code": "authentication_invalid"}]}
            if state.fail:
                return state.fail, {"errors": [{"code": "form_param_value_invalid"}]}
            if self.command == "POST" and self.path == "/v1/api_keys":
                state.minted.append(body)
                if state.mint_answer:
                    return state.mint_answer
                secret = "ak_" + "minted_for_" + body["subject"]
                return 200, {"id": "ak_new_1", "subject": body["subject"], "name": body["name"], "secret": secret,
                             "claims": None, "revoked": False, "expired": False}
            if self.command == "GET" and self.path.startswith("/v1/api_keys?"):
                return 200, {"data": state.user_keys, "total_count": len(state.user_keys)}
            if self.command == "GET" and self.path.startswith("/v1/api_keys/") and self.path.endswith("/secret"):
                answer = state.secrets.get(self.path.split("/")[3])
                return answer if isinstance(answer, tuple) else (
                    (200, {"secret": answer}) if answer else (404, {"errors": [{"code": "resource_not_found"}]}))
            if self.command == "POST" and self.path == "/v1/api_keys/verify":
                key = KEYS.get(body.get("secret"))
                return (200, key) if key else (404, {"errors": [{"code": "resource_not_found"}]})
            if self.command == "GET" and self.path.startswith("/v1/users?"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if "user_id" in query:
                    return 200, [u for u in USERS.values() if u["id"] in query["user_id"]]
                wanted = query.get("username", [None])[0]
                return 200, [u for u in USERS.values() if u.get("username") == wanted]
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
    monkeypatch.setenv("XO_CORE_API_URL", f"http://127.0.0.1:{httpd.server_address[1]}/core")
    monkeypatch.setenv("XO_EDITABLE_PUBLIC_KEYS", "plan,beta")
    monkeypatch.setenv("XO_EDITABLE_PRIVATE_KEYS", "stripeId")
    monkeypatch.setenv("XO_ADMIN_KEY_IDS", "ak_admin,ak_listed")
    monkeypatch.setenv("XO_CLERK_PER_SECOND", "1000")  # the budget has its own test
    monkeypatch.setenv("XO_CORE_PER_SECOND", "1000")  # so has this one
    yield state
    httpd.shutdown()


CLIENT_ID = "cli_xo_test"
REDIRECT = server.LOGIN_REDIRECTS[0]
VERIFIER = "v" * 43
MINTED = "ak_" + "minted_for_user_alice"  # built at runtime: not a key-shaped literal
OLD = "ak_" + "alice_already_had"


@pytest.fixture
def browser_on(clerk, monkeypatch):
    monkeypatch.setenv("XO_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("XO_CLERK_FRONTEND_URL", os.environ["CLERK_API_URL"] + "/fapi/")
    return clerk


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
    for secret in (SECRET, USER_KEY, ADMIN_KEY, FORGED_KEY, MINTED, OLD, VERIFIER, "oat_user_alice", "code_1"):
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


def test_me_verifies_the_key_and_returns_the_profile(xo_server, clerk):
    status, out = call(xo_server, "GET", "/me", USER_KEY)
    assert status == 200
    assert out["data"] == {"id": "user_alice", "username": "alice", "firstName": "Alice", "lastName": "A",
                           "imageUrl": "https://img.example/a", "createdAt": 1700000000000,
                           "email": "alice@example.com", "publicMetadata": {"plan": "free"},
                           "privateMetadata": {"stripeId": "cus_1"}}
    verify = clerk.calls[0]
    assert (verify["method"], verify["path"], verify["body"]) == ("POST", "/v1/api_keys/verify", {"secret": USER_KEY})
    sent = {k.lower(): v for k, v in verify["headers"].items()}
    assert sent["clerk-api-version"] == server.CLERK_API_VERSION and sent["authorization"] == f"Bearer {SECRET}"
    assert [c["path"] for c in clerk.calls[1:]] == ["/v1/users/user_alice"]  # only the key's own user


def test_me_with_no_metadata_or_email_says_so(xo_server):
    out = call(xo_server, "GET", "/me", ADMIN_KEY)[1]["data"]
    assert (out["email"], out["publicMetadata"], out["privateMetadata"]) == (None, {}, {})


def test_me_masks_secret_looking_metadata_at_any_depth(xo_server, monkeypatch):
    monkeypatch.setitem(USERS["user_alice"], "private_metadata", {
        "githubToken": "ghp_abcdefghijklmnop1234", "api_key": "sk-live-0123456789wxyz", "password": "short",
        "nested": {"clientSecret": {"a": 1}, "keys": ["k1"], "monkey": "banana", "keyboard": "us"},
        "list": [{"accessKey": "AKIAABCDEFGHIJKL5678"}], "plan": "pro", "emptyToken": ""})
    monkeypatch.setitem(USERS["user_alice"], "public_metadata", {"session_cookie": "c" * 20 + "WXYZ"})
    out = call(xo_server, "GET", "/me", USER_KEY)[1]["data"]
    assert out["privateMetadata"] == {
        "githubToken": "\u20261234", "api_key": "\u2026wxyz", "password": "(hidden)",
        "nested": {"clientSecret": "(hidden)", "keys": "(hidden)", "monkey": "banana", "keyboard": "us"},
        "list": [{"accessKey": "\u20265678"}], "plan": "pro", "emptyToken": ""}
    assert out["publicMetadata"] == {"session_cookie": "\u2026WXYZ"}
    text = json.dumps(out)
    assert "ghp_abcdefghijkl" not in text and "sk-live" not in text and "AKIAABCD" not in text


@pytest.mark.parametrize("value", [
    "sk-proj-abcdefghijklmnopWXYZ", "sk-ant-api03-abcdefghWXYZ", "ghp_abcdefghijklmnopWXYZ", "ak_abcdefghijWXYZ",
    "sk_live_abcdefghijWXYZ", "AKIAABCDEFGHIJKLWXYZ", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sigWXYZ",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBWXYZ", "xoxb-1234-5678-abcdWXYZ", "postgres://u:pw@db.example/xoWXYZ",
    "Bearer abcdefghijklWXYZ", "github_pat_abcdefghijWXYZ"])
def test_me_masks_credential_looking_values_under_any_name(xo_server, monkeypatch, value):
    monkeypatch.setitem(USERS["user_alice"], "public_metadata", {"openai": value, "plan": "free"})
    out = call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["publicMetadata"]
    assert out == {"openai": "\u2026WXYZ", "plan": "free"}


@pytest.mark.parametrize("name", ["sk", "pat", "jwt", "bearer", "auth", "authorization", "sessionId", "sid",
                                  "creds", "dsn", "webhookUrl", "hmac", "githubPAT", "mnemonic", "seedPhrase"])
def test_me_masks_more_credential_names(xo_server, monkeypatch, name):
    monkeypatch.setitem(USERS["user_alice"], "private_metadata", {name: "plain-looking-valueWXYZ", "stripeId": "cus_1"})
    out = call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["privateMetadata"]
    assert out == {name: "\u2026WXYZ", "stripeId": "cus_1"}


@pytest.mark.parametrize("name", ["password", "dbPasswd", "pwd", "passphrase", "pass", "passcode", "pin", "otp",
                                  "totpCode"])
def test_me_shows_nothing_of_a_password_however_long(xo_server, monkeypatch, name):
    monkeypatch.setitem(USERS["user_alice"], "private_metadata", {name: "correct-horse-battery-staple"})
    assert call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["privateMetadata"] == {name: "(hidden)"}


def test_me_private_keys_narrow_what_a_user_sees(xo_server, monkeypatch):
    monkeypatch.setitem(USERS["user_alice"], "private_metadata", {"stripeId": "cus_1", "fraudFlag": True})
    xo_server.app.cfg.me_private = frozenset({"stripeId"})
    out = call(xo_server, "GET", "/me", USER_KEY)[1]["data"]
    assert out["privateMetadata"] == {"stripeId": "cus_1"} and out["publicMetadata"] == {"plan": "free"}


def test_me_private_keys_fail_closed_on_odd_metadata(xo_server, monkeypatch):
    monkeypatch.setitem(USERS["user_alice"], "private_metadata", ["fraud"])
    xo_server.app.cfg.me_private = frozenset({"stripeId"})
    assert call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["privateMetadata"] == {}


@pytest.mark.parametrize("raw, expected", [("", None), (" , ", None), ("stripeId, plan", {"stripeId", "plan"})])
def test_me_private_keys_come_from_the_environment(clerk, monkeypatch, raw, expected):
    monkeypatch.setenv("XO_ME_PRIVATE_KEYS", raw)
    assert server.Config().me_private == (frozenset(expected) if expected else None)


@pytest.mark.parametrize("addresses", [7, True, "a@b", {"id": "idn_2"}, None])
def test_me_survives_odd_email_shapes(xo_server, monkeypatch, addresses):
    monkeypatch.setitem(USERS["user_alice"], "email_addresses", addresses)
    status, out = call(xo_server, "GET", "/me", USER_KEY)
    assert status == 200 and out["data"]["email"] is None


def test_the_me_cache_drops_expired_entries(xo_server, monkeypatch):
    app = xo_server.app
    monkeypatch.setattr(server, "ME_CACHE_PRUNE_AT", 3)
    app._me_cache = {f"user_old{i}": (app.clock() - server.ME_CACHE_SECONDS - 1, {}) for i in range(3)}
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == 200
    assert list(app._me_cache) == ["user_alice"]


def test_the_deploy_scripts_me_check_accepts_what_xo_me_prints(xo_server, monkeypatch, capfd, tmp_path):
    """docs/test-xo-server.sh step 4 parses `xo me`: run that very parser on the real output."""
    script = (Path(__file__).parent.parent / "docs" / "test-xo-server.sh").read_text()
    step = script[script.index("# 4. xo me"):script.index("# 5. ")]
    parser = re.search(r"<<'PY'\n(.*?)\nPY\n", step, re.S).group(1)
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    capfd.readouterr()
    assert cli.main(["me"]) == 0
    (tmp_path / "me.json").write_text(capfd.readouterr().out)
    result = subprocess.run([sys.executable, "-I", "-", str(tmp_path / "me.json")], input=parser,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "id user_alice, username alice" in result.stdout
    (tmp_path / "old.json").write_text(json.dumps({"success": True, "data": {"id": "user_1"}}))
    result = subprocess.run([sys.executable, "-I", "-", str(tmp_path / "old.json")], input=parser,
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0


def test_me_metadata_never_reaches_the_audit_log_or_status(xo_server, clerk, capfd):
    call(xo_server, "GET", "/me", USER_KEY)
    status = json.dumps(call(xo_server, "GET", "/status")[1])
    err = capfd.readouterr().err
    for value in ("cus_1", "stripeId", "free", "alice@example.com"):
        assert value not in status and value not in err


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
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", ADMIN_KEY)
    # xo has no user-metadata command now; the server's admin route stays, reached directly.
    status, out = call(xo_server, "PATCH", "/user-metadata/user_suraj", ADMIN_KEY,
                       body={"publicMetadata": {"beta": True}})
    assert status == 200 and out["data"]["publicMetadata"]["beta"] is True
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    assert cli.main(["me"]) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    profile = json.loads(out)["xo"]
    assert (profile["username"], profile["email"]) == ("alice", "alice@example.com")
    assert (profile["public_data"], profile["private_data"]) == ({"plan": "free"}, {"stripeId": "cus_1"})


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


@pytest.mark.parametrize("status, expected", [(429, 503), (500, 502), (403, 503)])
def test_the_audit_line_records_what_the_caller_got(xo_server, clerk, status, expected):  # #6
    xo_server.app.clerk.verify_key = lambda secret: KEYS[secret]
    clerk.fail = status
    assert call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)[0] == expected
    entry = audits(xo_server)[-1]
    assert (entry["outcome"], entry["clerk"]) == (expected, status)


@pytest.mark.parametrize("status, expected", [(429, 503), (500, 502), (403, 503)])
def test_the_audit_line_names_clerk_when_the_key_check_fails(xo_server, clerk, status, expected):
    clerk.fail = status  # the key check itself fails
    assert call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)[0] == expected
    entry = audits(xo_server)[-1]
    assert (entry["outcome"], entry["clerk"], entry["keyId"]) == (expected, status, None)


def test_the_audit_line_says_when_clerk_never_answered(xo_server, monkeypatch):
    monkeypatch.setattr(xo_server.app.cfg, "clerk_url", "http://127.0.0.1:9")  # nothing listens there
    assert call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)[0] == 502
    assert (audits(xo_server)[-1]["outcome"], audits(xo_server)[-1]["clerk"]) == (502, "unreachable")


def test_a_clerk_answer_that_is_not_json_is_not_called_unreachable(xo_server, clerk):
    clerk.raw = b"<html>maintenance</html>"
    assert call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)[0] == 502
    assert (audits(xo_server)[-1]["outcome"], audits(xo_server)[-1]["clerk"]) == (502, "not JSON")


def test_a_refusal_before_clerk_has_no_clerk_field(xo_server):
    call(xo_server, "GET", "/user-metadata/user_alice", "ak_unknown_and_unlisted")
    assert "clerk" not in audits(xo_server)[-1]


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
        s.sendall(b"GET /a\x01b\x1bc\x85d\\x41e HTTP/1.1\r\nHost: x\r\n\r\n")  # C0, ESC, C1, backslash
        s.recv(100)
    err = capfd.readouterr().err
    assert '"GET /a\\x01b\\x1bc\\x85d\\\\x41e HTTP/1.1" 400' in err  # the access line
    assert not any(c in err for c in "\x01\x1b\x85")


def test_the_deadline_timer_is_cancelled_when_a_request_ends(clerk, monkeypatch):  # #6 SF3
    timers = []

    class Timer(threading.Timer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cancelled = False
            timers.append(self)

        def cancel(self):
            self.cancelled = True
            super().cancel()

    monkeypatch.setattr(server.threading, "Timer", Timer)
    httpd = server.make_server(server.App(server.Config(), audit=lambda line: None), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    port = httpd.server_address[1]
    import socket
    for ending in (b"Host: x\r\n\r\n", b""):  # answered, and closed before the request ended
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\n" + ending)
            if ending:
                s.recv(100)
    import time
    deadline = time.monotonic() + 3  # a handler finishes just after its client sees the close
    while not (len(timers) == 2 and all(t.cancelled for t in timers)):
        assert time.monotonic() < deadline, f"{sum(t.cancelled for t in timers)} of {len(timers)} cancelled"
        time.sleep(0.01)
    httpd.shutdown()


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


# --- the status page: health, counts and recent requests, with nothing secret in them -----------

def test_the_status_page_is_served_with_a_strict_policy(xo_server):
    import base64
    import hashlib
    from xo import status_page
    with urllib.request.urlopen(xo_server.url + "/", timeout=5) as resp:
        page, headers = resp.read().decode(), resp.headers
    assert headers["Content-Type"] == "text/html; charset=utf-8" and headers["Cache-Control"] == "no-store"
    csp = headers["Content-Security-Policy"]
    for rule in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
                 "form-action 'none'"):
        assert rule in csp, rule
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Referrer-Policy"] == "no-referrer"
    for part in (status_page.SCRIPT, status_page.STYLE):  # the hashes match what the page inlines
        assert part in page
        assert base64.b64encode(hashlib.sha256(part.encode()).digest()).decode() in csp
    assert "http" not in status_page.SCRIPT


STATUS_KEYS = {"status", "version", "recent", "instance", "activity"}
INSTANCE_KEYS = {"startedAt", "requests", "pings", "byStatus"}
ENTRY_KEYS = {"time", "method", "route", "status", "ms"}


def test_status_lists_routes_and_counts_but_never_ids_or_keys(xo_server):
    call(xo_server, "GET", "/healthz")
    call(xo_server, "GET", "/me?user=user_bob&q=secretquery", USER_KEY)
    call(xo_server, "GET", "/user-metadata/user_alice?x=secretquery", USER_KEY)
    call(xo_server, "GET", "/user-metadata/user_alice", ADMIN_KEY)
    call(xo_server, "POST", "/healthz")
    call(xo_server, "GET", "/nope/user_carol")
    urllib.request.urlopen(xo_server.url + "/?user_dave", timeout=5).read()
    first = call(xo_server, "GET", "/status?user_erin")
    status, out = call(xo_server, "GET", "/status")
    assert first[0] == status == 200
    data = out["data"]
    assert set(data) == STATUS_KEYS and set(data["instance"]) == INSTANCE_KEYS
    assert data["recent"] == {"entries": 3, "failed": 0} and data["status"] == "ok"
    assert all(set(a) == ENTRY_KEYS for a in data["activity"])
    assert [(a["method"], a["route"], a["status"]) for a in data["activity"]] == [
        ("GET", "other", 404), ("POST", "/healthz", 404), ("GET", "/user-metadata/{userId}", 200),
        ("GET", "/user-metadata/{userId}", 403), ("GET", "/me", 200), ("GET", "/healthz", 200),
    ]  # newest first; the page and /status are not listed, however they are asked for
    assert data["instance"]["requests"] == 6 and data["instance"]["pings"] == 1  # a 404 POST is no ping
    assert data["instance"]["byStatus"] == {"2xx": 3, "3xx": 0, "4xx": 3, "5xx": 0}
    text = json.dumps(out)
    for needle in ("user_", "secretquery", "nope", "ak_", USER_KEY, ADMIN_KEY, SECRET, "sk_", "127.0.0.1",
                   "plan", "beta", "stripeId", "XO_", "CLERK", "api.clerk", "http"):
        assert needle not in text, needle


def test_an_unknown_method_is_listed_as_other(clerk):
    app = server.App(server.Config(), audit=lambda line: None)
    app.handle("BREW", "/healthz", {}, None)
    assert [(a["method"], a["route"]) for a in app.status()["activity"]] == [("OTHER", "/healthz")]


def test_status_says_degraded_when_recent_requests_fail(xo_server, clerk):
    clerk.fail = 401  # Clerk refuses the server's own secret: every /me is a 503
    for _ in range(3):
        assert call(xo_server, "GET", "/me", USER_KEY)[0] == 503
    data = call(xo_server, "GET", "/status")[1]["data"]
    assert data["status"] == "degraded" and data["recent"] == {"entries": 3, "failed": 3}
    for _ in range(40):  # other traffic, anonymous or refused, does not push the failures out (#6 D2)
        call(xo_server, "GET", "/nope")
        call(xo_server, "GET", "/healthz")
        call(xo_server, "POST", "/me")
    data = call(xo_server, "GET", "/status")[1]["data"]
    assert data["status"] == "degraded" and data["recent"] == {"entries": 43, "failed": 3}


def test_a_failure_heals_after_five_minutes(clerk):  # #6 D2
    now = [1000.0]
    app = server.App(server.Config(), audit=lambda line: None, clock=lambda: now[0])
    app.record("GET", "/me", 503, now[0])
    app.record("GET", "/healthz", 500, now[0])  # a route that does not call Clerk never counts
    assert app.status()["status"] == "degraded" and app.status()["recent"] == {"entries": 1, "failed": 1}
    now[0] += server.DEGRADED_SECONDS - 1
    assert app.status()["status"] == "degraded"
    now[0] += 1
    assert app.status()["status"] == "ok" and app.status()["recent"] == {"entries": 0, "failed": 0}


def test_the_degraded_counts_are_bounded(clerk, monkeypatch):  # #6 audit nit 4
    monkeypatch.setattr(server, "DEGRADED_KEPT", 5)
    app = server.App(server.Config(), audit=lambda line: None, clock=lambda: 1000.0)
    for _ in range(20):
        app.record("GET", "/me", 503, 0)
    assert (len(app._seen), len(app._failed)) == (5, 5)
    assert app.status()["recent"] == {"entries": 5, "failed": 5}


def _raw_head(srv, path: str, extra: bytes = b"") -> tuple[bytes, bytes]:
    """A HEAD request over a raw socket, read to the end: urllib never reads a body on HEAD (#6 D1)."""
    import socket
    with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5) as s:
        s.sendall(b"HEAD " + path.encode() + b" HTTP/1.0\r\nHost: x\r\n" + extra + b"\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


@pytest.mark.parametrize("path, status", [("/", 200), ("/healthz", 200), ("/status", 200), ("/me", 405),
                                          ("/user-metadata/user_alice", 405), ("/nope", 404)])
def test_head_is_answered_without_a_body(xo_server, clerk, path, status):
    head, body = _raw_head(xo_server, path, f"Authorization: Bearer {ADMIN_KEY}\r\n".encode())
    assert head.startswith(f"HTTP/1.0 {status}".encode()) and b"Content-Length: " in head and body == b""
    assert clerk.calls == []  # HEAD never reaches the key check


@pytest.mark.parametrize("path, status", [("/", 200), ("/healthz", 200), ("/status", 200), ("/me", 405),
                                          ("/user-metadata/user_alice", 405), ("/nope", 404)])
def test_wsgi_head_is_answered_without_a_body(clerk, path, status):
    import io
    application = server.wsgi_app()
    seen = {}
    environ = {"REQUEST_METHOD": "HEAD", "PATH_INFO": path, "QUERY_STRING": "", "wsgi.input": io.BytesIO(b""),
               "CONTENT_LENGTH": "", "HTTP_AUTHORIZATION": f"Bearer {ADMIN_KEY}"}
    out = b"".join(application(environ, lambda s, headers: seen.update(status=s, headers=dict(headers))))
    assert seen["status"].startswith(str(status)) and int(seen["headers"]["Content-Length"]) > 0 and out == b""
    if path != "/":  # the page is served before the body is looked at
        environ.update({"wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": str(server.MAX_BODY + 1)})
        assert b"".join(application(environ, lambda s, headers: seen.update(status=s))) == b""
        assert seen["status"].startswith("413")
    assert clerk.calls == []


def test_the_page_never_writes_html_from_data():
    from xo import status_page
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function("):
        assert sink not in status_page.SCRIPT, sink


def test_status_keeps_only_the_latest_requests(clerk, monkeypatch):
    monkeypatch.setattr(server, "ACTIVITY_KEPT", 3)
    app = server.App(server.Config(), audit=lambda line: None)
    for _ in range(5):
        app.handle("GET", "/healthz", {}, None)
    status = app.status()
    assert len(status["activity"]) == 3 and status["instance"]["pings"] == 5


def test_a_body_refused_as_too_big_is_counted(xo_server):
    call(xo_server, "PATCH", "/user-metadata/user_alice", ADMIN_KEY, raw=b" " * (server.MAX_BODY + 1))
    activity = call(xo_server, "GET", "/status")[1]["data"]["activity"]
    assert (activity[0]["route"], activity[0]["status"]) == ("/user-metadata/{userId}", 413)


def test_wsgi_serves_the_page_even_when_misconfigured(monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    application = server.wsgi_app()
    seen = {}
    out = b"".join(application({"REQUEST_METHOD": "GET", "PATH_INFO": "/"},
                               lambda status, headers: seen.update(status=status, headers=dict(headers))))
    assert seen["status"] == "200 OK" and out.startswith(b"<!doctype html>")
    assert "Content-Security-Policy" in seen["headers"]
    assert wsgi_call(application, "GET", "/status")[0] == 503  # so the page shows "Unreachable"
    head = {}
    assert b"".join(application({"REQUEST_METHOD": "HEAD", "PATH_INFO": "/"},
                                lambda status, headers: head.update(status=status))) == b""
    assert head["status"] == "200 OK"


# --- the core API, passed on to XO -----------------------------------------------------------------

def test_a_create_is_passed_on_as_sent(xo_server, clerk):
    body = {"name": "my-codex", "volumeSize": 20, "providers": {"openai": {"enabled": True, "apiKey": "sk-x"}}}
    status, out = call(xo_server, "POST", "/api/core/v1/codex?x=1", USER_KEY, body)
    assert (status, out) == (200, {"success": True, "data": {"id": "space_1"}})
    sent = clerk.core[0]
    assert (sent["method"], sent["path"]) == ("POST", "/core/codex")  # no query passed on
    assert sent["headers"]["Authorization"] == f"Bearer {USER_KEY}"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert json.loads(sent["body"]) == body
    assert not [c for c in clerk.calls if c["path"].startswith("/v1/")]  # Clerk is not asked


def test_only_the_callers_key_and_body_go_on(xo_server, clerk):
    req = urllib.request.Request(xo_server.url + "/api/core/v1/spaces/abc-123", method="GET", headers={
        "Authorization": f"Bearer {USER_KEY}", "Cookie": "session=1", "X-Forwarded-For": "1.2.3.4",
        "X-Custom": "y"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    sent = clerk.core[0]
    assert sent["path"] == "/core/spaces/abc-123" and sent["body"] == b""
    assert {k.lower() for k in sent["headers"]} <= {"accept", "user-agent", "authorization", "host",
                                                    "connection", "accept-encoding"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/core/v1/codex"), ("POST", "/api/core/v1/spaces/x"), ("GET", "/api/core/v1/me"),
    ("POST", "/api/core/v1/unknown"), ("DELETE", "/api/core/v1/spaces/x"), ("PUT", "/api/core/v1/codex"),
    ("GET", "/api/core/v1/spaces/a%2Fb"), ("GET", "/api/core/v1/spaces/..%2Fadmin"),
    ("POST", "/api/core/v1/codex/extra"), ("GET", "/api/core/v1/spaces/" + "a" * 65), ("POST", "/api/core/v1/"),
])
def test_nothing_else_under_the_core_path_leaves_the_server(xo_server, clerk, method, path):
    assert call(xo_server, method, path, USER_KEY, {} if method != "GET" else None)[0] in (404, 405)
    assert clerk.core == []


@pytest.mark.parametrize("key,error", [(None, "Missing API key"), ("not-a-key", "Invalid API key")])
def test_a_core_request_without_an_api_key_stays_here(xo_server, clerk, key, error):
    assert call(xo_server, "POST", "/api/core/v1/codex", key, {}) == (401, {"success": False, "error": error})
    assert clerk.core == []


def test_xos_answer_comes_back_as_it_was(xo_server, clerk):
    clerk.core_answer = (422, b'{"success": false, "errors": ["name: too long"]}', {})
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == \
        (422, {"success": False, "errors": ["name: too long"]})
    clerk.core_answer = (500, b"<html>oops</html>", {})
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == \
        (500, {"success": False, "error": "XO answered 500 without a JSON object"})
    clerk.core_answer = (599, b"{}", {})
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY)[0] == 502


@pytest.mark.parametrize("method,status", [("POST", 307), ("GET", 302), ("GET", 301), ("POST", 308)])
def test_a_redirect_from_xo_is_not_followed(xo_server, clerk, method, status):
    # The Location is the fake's own /core path, so a followed redirect would show up as a second request.
    clerk.core_answer = (status, b"", {"Location": os.environ["XO_CORE_API_URL"] + "/spaces/elsewhere"})
    path = "/api/core/v1/codex" if method == "POST" else "/api/core/v1/spaces/s1"
    assert call(xo_server, method, path, USER_KEY, {} if method == "POST" else None) == \
        (502, {"success": False, "error": f"XO redirected the request ({status}); not followed"})
    assert len(clerk.core) == 1


def test_a_get_body_is_not_passed_on(xo_server, clerk):
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY, raw=b'{"x": 1}')[0] == 200
    assert clerk.core[0]["body"] == b"" and "Content-Type" not in clerk.core[0]["headers"]


def test_an_answer_that_is_not_json_says_whether_it_worked(xo_server, clerk):
    clerk.core_answer = (201, b"created", {})
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == \
        (201, {"success": True, "error": "XO answered 201 without a JSON object"})


def test_a_too_large_answer_is_not_passed_on(xo_server, clerk, monkeypatch):
    monkeypatch.setattr(server, "MAX_CORE_ANSWER", 16)
    clerk.core_answer = (200, b'{"success": true, "data": {"id": "space_1"}}', {})
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == \
        (200, {"success": True, "error": "XO answered 200 with more than 16 bytes, too large to pass on"})
    clerk.core_answer = (200, b'{"success": true}', {})  # 17 bytes
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {})[1]["error"].startswith("XO answered 200 with")
    clerk.core_answer = (200, b'{"a": "bcdefgh"}', {})  # exactly 16 bytes
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == (200, {"a": "bcdefgh"})


def test_xo_failing_does_not_make_the_server_degraded(xo_server, clerk):
    clerk.core_answer = (500, b'{"success": false}', {})
    for _ in range(3):
        assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {})[0] == 500
    assert call(xo_server, "GET", "/status")[1]["data"]["status"] == "ok"


def test_forwards_have_a_budget_for_the_whole_process(clerk, monkeypatch):
    monkeypatch.setenv("XO_CORE_PER_SECOND", "2")
    app = server.App(server.Config())
    keys = [f"ak_key_{n}" for n in range(3)]  # different keys: the per-key limit does not apply
    answers = [app.handle("POST", "/api/core/v1/codex", {"authorization": f"Bearer {k}"}, b"{}")[0] for k in keys]
    assert answers == [200, 200, 503]
    assert len(clerk.core) == 2


def test_xo_unreachable_or_slow(xo_server, clerk, monkeypatch):
    core = xo_server.app.cfg.core_url
    monkeypatch.setattr(xo_server.app.cfg, "core_url", "http://127.0.0.1:9")
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == (502, {
        "success": False, "error": "XO (127.0.0.1) did not answer; it may have been received, so check before you retry"})
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY) == (502, {
        "success": False, "error": "XO (127.0.0.1) did not answer"})
    monkeypatch.setattr(xo_server.app.cfg, "core_url", core)
    monkeypatch.setattr(server, "CORE_TIMEOUT", 0.2)
    clerk.core_stall = 0.5
    assert call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {}) == (504, {
        "success": False, "error": "XO (127.0.0.1) did not answer in time; it may have been received, so check before you retry"})
    clerk.core_stall = 0.0
    clerk.core_trickle = 0.15  # each wait is short, but the whole answer takes about 7 seconds
    started = time.monotonic()
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY) == (504, {
        "success": False, "error": "XO (127.0.0.1) did not answer in time"})
    assert time.monotonic() - started < 2


def test_a_slow_connect_still_ends_at_the_deadline(xo_server, clerk, monkeypatch):
    # The timer fires while connecting, when there is no socket yet to cut: the check after the
    # connect is then all that stops the answer from taking as long as XO likes.
    monkeypatch.setattr(server, "CORE_TIMEOUT", 0.2)
    real = http.client.HTTPConnection.connect

    def slow_connect(self):
        time.sleep(0.4)
        real(self)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY) == (504, {
        "success": False, "error": "XO (127.0.0.1) did not answer in time"})
    assert clerk.core == []


def test_core_requests_are_rate_limited_per_key(xo_server, clerk):
    for _ in range(server.LIMITS["core"]):
        assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY)[0] == 200
    assert call(xo_server, "GET", "/api/core/v1/spaces/s1", USER_KEY)[0] == 429
    assert len(clerk.core) == server.LIMITS["core"]
    assert call(xo_server, "GET", "/me", USER_KEY)[0] == 200  # a separate budget


def test_the_status_page_names_core_routes_not_ids(xo_server, clerk):
    call(xo_server, "GET", "/api/core/v1/spaces/secret-space-id", USER_KEY)
    call(xo_server, "POST", "/api/core/v1/codex", USER_KEY, {})
    call(xo_server, "GET", "/api/core/v1/nope/secret-path", USER_KEY)
    routes = [a["route"] for a in call(xo_server, "GET", "/status")[1]["data"]["activity"]]
    assert routes == ["other", "/api/core/v1/codex", "/api/core/v1/spaces/{spaceId}"]


def test_the_core_routes_are_the_specs(clerk):
    from xo import spec
    ops = [o for o in spec.operations(spec.load()) if not o.server]
    assert {(o.method, o.path) for o in ops} == {
        (m, "/spaces/{spaceId}" if name else "/" + n)
        for m, pattern, name in server.CORE_ROUTES
        for n in (pattern.pattern[2:-1].split("|") if not name else ["x"])}


def test_the_core_url_must_be_https(clerk, monkeypatch):
    monkeypatch.setenv("XO_CORE_API_URL", "http://app.xo.example/api")
    with pytest.raises(server.XoError, match="XO_CORE_API_URL must be https://"):
        server.Config()


@pytest.mark.parametrize("url", ["http://localhost:abc/api", "https://app.xo.example:99999/api",
                                 "https://app.xo.example:0/api", "https:///api"])
def test_a_bad_core_url_stops_the_server_at_start(clerk, monkeypatch, url):
    monkeypatch.setenv("XO_CORE_API_URL", url)
    with pytest.raises(server.XoError, match="XO_CORE_API_URL (has a port|needs a host)"):
        server.Config()


@pytest.mark.parametrize("name", ["XO_CORE_API_URL", "CLERK_API_URL"])
@pytest.mark.parametrize("url", ["https://[::1/api", "https://[::1]@evil.example/api", "https://app.xo.example/a\nb",
                                 "https://app.xo.example/a b"])
def test_a_malformed_upstream_address_stops_the_server_with_a_clear_message(clerk, monkeypatch, name, url):
    monkeypatch.setenv(name, url)
    with pytest.raises(server.XoError, match=f"{name} is not a valid address"):
        server.Config()


@pytest.mark.parametrize("name, value", [
    ("XO_CORE_API_URL", " https://app.xo.example/api"), ("XO_CORE_API_URL", "\nhttps://app.xo.example/api"),
    ("XO_CORE_API_URL", "https://app.xo.example/api\n"), ("XO_CORE_API_URL", "https://app.xo.example/api\r\n"),
    ("XO_CORE_API_URL", "https://app.xo.example/api\t"), ("CLERK_API_URL", " https://clerk.example"),
    ("CLERK_API_URL", "https://clerk.example\n")])
def test_whitespace_at_either_end_of_an_upstream_address_is_ignored(clerk, monkeypatch, name, value):
    """A value pasted into the deploy settings often carries a trailing newline: it must not take the API down."""
    monkeypatch.setenv(name, value)
    cfg = server.Config()
    assert (cfg.core_url if name == "XO_CORE_API_URL" else cfg.clerk_url) == value.strip()


def test_config_names_no_platform_for_a_malformed_core_address(monkeypatch):
    monkeypatch.setenv("XO_CORE_API_URL", "https://[::1/api/core/v1")
    data = server.services_list()
    assert data["xo"]["url"] is None and data["xo"]["about"] == services.SHIPPED["xo"]["about"]


def test_a_cli_core_create_goes_through_the_server(xo_server, clerk, monkeypatch, capfd):
    # `xo create space` records through /me/spaces; `xo call` still sends a bare create through /api/core.
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    assert cli.main(["call", "create_codex", "--data", '{"name": "my-codex", "volumeSize": 20}']) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out)["data"]["id"] == "space_1"
    assert clerk.core[0]["path"] == "/core/codex"
    assert json.loads(clerk.core[0]["body"]) == {"name": "my-codex", "volumeSize": 20}
    assert cli.main(["spaces", "--space-id", "space_1"]) == 0
    assert clerk.core[1]["path"] == "/core/spaces/space_1"


# --- /config: the services list ---------------------------------------------------------------

def test_config_lists_the_services_without_a_key_or_clerk(xo_server, clerk, monkeypatch):
    monkeypatch.setenv("XO_CORE_API_URL", "https://dev.xo.builders/api/core/v1")
    status, out = call(xo_server, "GET", "/config")
    assert status == 200 and out["data"] == services.SHIPPED
    monkeypatch.setenv("XO_CORE_API_URL", "https://platform.example/api/core/v1")
    assert call(xo_server, "GET", "/config")[1]["data"]["xo"]["url"] == "https://platform.example"
    monkeypatch.setenv("XO_CORE_API_URL", "http://127.0.0.1:9/core")  # not an address the list may name
    listed = call(xo_server, "GET", "/config")[1]["data"]
    assert listed["xo"]["url"] is None and listed["xo"]["about"] == services.SHIPPED["xo"]["about"]


def test_config_answers_even_without_the_clerk_secret(monkeypatch):
    monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
    monkeypatch.delenv("XO_CORE_API_URL", raising=False)
    status, out, headers = wsgi_call(server.wsgi_app(), "GET", "/config")
    assert status == 200 and out == {"success": True, "data": services.SHIPPED}
    assert headers["Cache-Control"] == "no-store"
    assert "ak_" not in json.dumps(out) and "sk_" not in json.dumps(out)


def test_the_platform_default_is_the_listed_one():
    assert server.DEFAULT_CORE_URL == "https://dev.xo.builders/api/core/v1"
    assert services.SHIPPED["xo"]["url"] == "https://dev.xo.builders"


# --- /me/projects: the one write a normal key can make ------------------------------------------

@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(server, "PROJECT_SETTLE", 0)


@pytest.fixture
def alice(monkeypatch):
    """A copy of alice the fake Clerk may change, so no test leaks into another."""
    user = json.loads(json.dumps(USERS["user_alice"]))
    user["private_metadata"] = {"stripeId": "cus_1", "notes": {"a": [1, 2]}, "flag": True}
    monkeypatch.setitem(USERS, "user_alice", user)
    return user


def test_projects_starts_empty_and_reads_only_the_callers_user(xo_server, clerk, alice):
    assert call(xo_server, "GET", "/me/projects", USER_KEY) == (200, {"success": True, "data": {"projects": []}})
    assert [c["path"] for c in clerk.calls[1:]] == ["/v1/users/user_alice"]


def test_add_project_writes_only_projects_and_keeps_every_other_key(xo_server, clerk, alice):
    before = json.dumps({k: v for k, v in alice.items() if k != "private_metadata"}, sort_keys=True)
    others = json.loads(json.dumps(alice["private_metadata"]))
    assert call(xo_server, "POST", "/me/projects", USER_KEY, {"name": "alpha"}) == \
        (200, {"success": True, "data": {"projects": ["alpha"]}})
    assert call(xo_server, "POST", "/me/projects", USER_KEY, {"name": " beta 2 "})[1]["data"] == \
        {"projects": ["alpha", "beta 2"]}
    patches = [c for c in clerk.calls if c["method"] == "PATCH"]
    assert [p["body"] for p in patches] == [{"private_metadata": {"projects": ["alpha"]}},
                                            {"private_metadata": {"projects": ["alpha", "beta 2"]}}]
    assert all(p["path"] == "/v1/users/user_alice/metadata" for p in patches)
    assert {k: v for k, v in alice["private_metadata"].items() if k != "projects"} == others
    assert json.dumps({k: v for k, v in alice.items() if k != "private_metadata"}, sort_keys=True) == before
    assert call(xo_server, "GET", "/me/projects", USER_KEY)[1]["data"] == {"projects": ["alpha", "beta 2"]}
    adds = [json.loads(line) for line in xo_server.audit if '"/me/projects"' in line]
    assert [(a["outcome"], a["count"], a["keyId"], a["caller"]) for a in adds] == \
        [(200, 1, "ak_user", "user_alice"), (200, 2, "ak_user", "user_alice")]
    assert not any("alpha" in line or "cus_1" in line for line in xo_server.audit)


@pytest.mark.parametrize("body, error", [
    ({"name": "alpha", "privateMetadata": {"x": 1}}, 'Body must be {"name": "..."} and nothing else'),
    ({"projects": ["a"]}, 'Body must be {"name": "..."} and nothing else'),
    ({"name": ["a"]}, 'Body must be {"name": "..."} and nothing else'),
    (["alpha"], 'Body must be {"name": "..."} and nothing else'),
    ({"name": ""}, None), ({"name": "-lead"}, None), ({"name": "a/b/c"}, None), ({"name": "a/.."}, None),
    ({"name": "-a/b"}, None), ({"name": "a/b.git"}, None), ({"name": "a/"}, None),
    ({"name": "x" * 65}, None),
    ({"name": "café"}, None), ({"name": "a\nb"}, None),
    ({"name": "sk-learn-demo"}, "That name looks like a credential (sk-, ghp_, ak_ and the like); choose another"),
    ({"name": "ghp_tools"}, None), ({"name": "ak_app"}, None), ({"name": "AKIA tools"}, None),
    ({"name": "Bearer app"}, None)])
def test_add_project_refuses_anything_but_one_plain_name_and_writes_nothing(xo_server, clerk, alice, body, error):
    status, out = call(xo_server, "POST", "/me/projects", USER_KEY, body)
    assert status == 422 and (error is None or out["error"] == error)
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_add_project_refuses_a_duplicate_ignoring_case(xo_server, clerk, alice):
    alice["private_metadata"]["projects"] = ["Alpha"]
    status, out = call(xo_server, "POST", "/me/projects", USER_KEY, {"name": "alpha"})
    assert (status, out["error"]) == (409, "alpha is already in your projects; nothing was changed")
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


@pytest.mark.parametrize("value", [{"a": 1}, "alpha", 3, True])
def test_add_project_never_overwrites_projects_that_is_not_a_list(xo_server, clerk, alice, value):
    alice["private_metadata"]["projects"] = value
    status, _ = call(xo_server, "POST", "/me/projects", USER_KEY, {"name": "alpha"})
    assert status == 409 and alice["private_metadata"]["projects"] == value
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_connect_repo_renames_a_plain_project_in_place_and_writes_only_projects(xo_server, clerk, alice):
    alice["private_metadata"]["projects"] = ["alpha", "notes", "beta"]
    others = json.loads(json.dumps({k: v for k, v in alice["private_metadata"].items() if k != "projects"}))
    status, out = call(xo_server, "POST", "/me/projects/connect", USER_KEY, {"name": "Notes", "repo": "o/notes"})
    assert (status, out["data"]) == (200, {"projects": ["alpha", "o/notes", "beta"]})
    patches = [c["body"] for c in clerk.calls if c["method"] == "PATCH"]
    assert patches == [{"private_metadata": {"projects": ["alpha", "o/notes", "beta"]}}]
    assert {k: v for k, v in alice["private_metadata"].items() if k != "projects"} == others
    audits = [json.loads(line) for line in xo_server.audit if '"/me/projects/connect"' in line]
    assert [(a["outcome"], a["count"]) for a in audits] == [(200, 3)]
    assert not any("notes" in line for line in xo_server.audit)


@pytest.mark.parametrize("projects, body, status", [
    (["alpha"], {"name": "notes", "repo": "o/notes"}, 404),
    (["notes", "O/Notes"], {"name": "notes", "repo": "o/notes"}, 409),
    (["o/notes"], {"name": "o/notes", "repo": "o/other"}, 422),
    (["notes"], {"name": "notes", "repo": "notes2"}, 422),
    (["notes"], {"name": "notes", "repo": "ghp_x/notes"}, 422),
    (["notes"], {"name": "notes"}, 422),
    (["notes"], {"name": "notes", "repo": "o/notes", "x": "1"}, 422)])
def test_connect_repo_refuses_and_writes_nothing(xo_server, clerk, alice, projects, body, status):
    alice["private_metadata"]["projects"] = projects
    assert call(xo_server, "POST", "/me/projects/connect", USER_KEY, body)[0] == status
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_connect_repo_refuses_a_project_already_in_a_space(xo_server, clerk, alice):
    alice["private_metadata"]["projects"] = ["notes"]
    alice["private_metadata"]["spaces"] = [{"name": "dev", "kind": "codex", "rev": 2, "projects": [{"notes": "main"}]}]
    status, out = call(xo_server, "POST", "/me/projects/connect", USER_KEY, {"name": "notes", "repo": "o/notes"})
    assert (status, out["error"]) == (409, "notes is in space dev, which would still name it notes; nothing was changed")
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_connect_repo_is_a_route_of_its_own(xo_server, clerk, alice):
    assert call(xo_server, "GET", "/me/projects/connect", USER_KEY)[0] == 405
    assert server._route_name("/me/projects/connect") == "/me/projects/connect"


def test_add_project_stops_at_the_cap(xo_server, clerk, alice):
    alice["private_metadata"]["projects"] = [f"p{i}" for i in range(server.MAX_PROJECTS)]
    assert call(xo_server, "POST", "/me/projects", USER_KEY, {"name": "one more"})[0] == 422
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_a_normal_key_still_cannot_write_other_metadata(xo_server, clerk, alice):
    assert call(xo_server, "PATCH", "/user-metadata/user_alice", USER_KEY, {"privateMetadata": {"x": 1}})[0] == 403
    assert call(xo_server, "PATCH", "/me/projects", USER_KEY, {"name": "a"})[0] == 405
    assert call(xo_server, "PUT", "/me/projects", USER_KEY, {"name": "a"})[0] == 405
    assert call(xo_server, "POST", "/me", USER_KEY, {"name": "a"})[0] == 405
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_add_project_needs_a_good_key_and_audits_the_refusal(xo_server, clerk, alice):
    assert call(xo_server, "POST", "/me/projects", "ak_unknown", {"name": "a"})[0] == 401
    assert call(xo_server, "POST", "/me/projects", None, {"name": "a"})[0] == 401
    assert [json.loads(line)["outcome"] for line in xo_server.audit] == [401, 401]
    assert not [c for c in clerk.calls if c["method"] == "PATCH"]


def test_add_project_clears_the_me_cache(xo_server, alice):
    assert call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["privateMetadata"].get("projects") is None
    call(xo_server, "POST", "/me/projects", USER_KEY, {"name": "alpha"})
    assert call(xo_server, "GET", "/me", USER_KEY)[1]["data"]["privateMetadata"]["projects"] == ["alpha"]


class _RacingClerk:
    """Stands in for Clerk where another writer replaces `projects` right after this add's write."""

    def __init__(self, stored: dict, races: int) -> None:
        self.stored, self.races, self.patches = stored, races, []

    def verify_key(self, secret: str) -> dict:
        return KEYS[secret]

    def user(self, user_id: str) -> dict:
        return json.loads(json.dumps(self.stored))

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        self.patches.append(changes)
        self.stored["private_metadata"].update(changes["privateMetadata"])
        if self.races:
            self.races -= 1
            self.stored["private_metadata"]["projects"] = ["theirs"]  # the other writer wins
        return json.loads(json.dumps(self.stored))


class _OnWriteClerk(_RacingClerk):
    """A Clerk where `race(private)` plays another writer once, right after this request's first write;
    a list of races plays one after each write in turn (None for none)."""

    def __init__(self, stored: dict, race) -> None:
        super().__init__(stored, races=0)
        self.queue = list(race) if isinstance(race, list) else [race]

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        out = super().update_metadata(user_id, changes)
        race = self.queue.pop(0) if self.queue else None
        if race:
            race(self.stored["private_metadata"])
        return out


def _connect_repo(fake, body=None, lines=None):
    app = server.App(server.Config(), clerk=fake, audit=(lambda line: None) if lines is None else lines.append)
    return app.handle("POST", "/me/projects/connect", {"authorization": f"Bearer {USER_KEY}"},
                      json.dumps(body or {"name": "foo", "repo": "o/r"}).encode())


@pytest.mark.parametrize("racing, want", [
    (["foo", "x", "o/r"], ["o/r", "x"]),  # a stale add of the repo: listed once
    (["x"], ["x", "o/r"]),  # a stale write dropped both: the repo is added again
])
def test_connect_repo_racing_a_projects_write_lists_the_repo_once(clerk, racing, want):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"]}}
    fake = _OnWriteClerk(user, lambda private: private.update(projects=list(racing)))
    status, out = _connect_repo(fake)
    assert status == 200 and user["private_metadata"]["projects"] == want and len(fake.patches) == 2


def test_connect_repo_puts_the_name_back_when_a_space_takes_it_meanwhile(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"], "spaces": [_space("dev")]}}

    def race(private):
        private["spaces"] = [_space("dev", projects=[{"foo": "main"}])]  # a racing xo connect
    fake = _OnWriteClerk(user, race)
    status, out = _connect_repo(fake)
    assert status == 409 and "dev" in out["error"]
    assert user["private_metadata"]["projects"] == ["foo", "x"] and len(fake.patches) == 2


def _in_space(private, space, project):
    private["spaces"] = [e for e in private.get("spaces", []) if e["name"] != space] + \
        [_space(space, projects=[{project: "main"}])]


def test_connect_repo_keeps_the_repo_when_another_space_names_it_too(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"], "spaces": []}}

    def race(private):  # two racing connects: foo into dev, the new o/r into dev2
        _in_space(private, "dev", "foo")
        _in_space(private, "dev2", "o/r")
    lines: list = []
    status, out = _connect_repo(_OnWriteClerk(user, race), lines=lines)
    assert status == 409 and "dev" in out["error"] and "nothing was changed" not in out["error"]
    assert user["private_metadata"]["projects"] == ["o/r", "x", "foo"]
    assert [json.loads(line)["outcome"] for line in lines] == [409]  # one audit line, no 200 before it


def test_connect_repo_writes_the_name_back_again_when_that_write_is_lost(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"], "spaces": []}}

    def lose(private):  # a stale write from before the put-back lands over it
        private["projects"] = ["o/r", "x"]
    fake = _OnWriteClerk(user, [lambda private: _in_space(private, "dev", "foo"), lose])
    status, out = _connect_repo(fake)
    assert status == 409 and user["private_metadata"]["projects"] == ["foo", "x"] and len(fake.patches) == 3


def test_connect_repo_names_the_space_when_the_put_back_never_settles(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"], "spaces": []}}

    def lose(private):
        private["projects"] = ["o/r", "x"]
    fake = _OnWriteClerk(user, [lambda private: _in_space(private, "dev", "foo")] + [lose] * 10)
    status, out = _connect_repo(fake)
    assert status == 503 and "space dev" in out["error"]


def test_connect_repo_does_not_say_nothing_changed_after_the_rename(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo", "x"], "spaces": []}}
    status, out = _connect_repo(_OnWriteClerk(user, lambda private: private.update(spaces=5)))
    assert status == 409 and "nothing was changed" not in out["error"] and "xo projects" in out["error"]


@pytest.mark.parametrize("spaces", [5, None, [{"no": "name"}]])
def test_connect_repo_refuses_spaces_it_cannot_read(clerk, spaces):
    user = {"id": "user_alice", "private_metadata": {"projects": ["foo"], "spaces": spaces}}
    fake = _OnWriteClerk(user, None)
    assert _connect_repo(fake)[0] == 409 and fake.patches == []


def test_add_project_tries_again_when_another_write_replaced_the_list(clerk):
    user = {"id": "user_alice", "private_metadata": {"stripeId": "cus_1", "projects": []}}
    fake = _RacingClerk(user, races=1)
    lines: list[str] = []
    app = server.App(server.Config(), clerk=fake, audit=lines.append)
    status, out = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "mine"}')
    assert (status, out["data"]["projects"]) == (200, ["theirs", "mine"])
    assert user["private_metadata"] == {"stripeId": "cus_1", "projects": ["theirs", "mine"]}
    assert len(fake.patches) == 2


class _DroppingClerk(_RacingClerk):
    """Another instance's add, started from an older list, lands just after this one and drops it."""

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        self.patches.append(changes)
        reply = json.loads(json.dumps({**self.stored, "private_metadata": {
            **self.stored["private_metadata"], **changes["privateMetadata"]}}))
        if self.races:
            self.races -= 1
            self.stored["private_metadata"]["projects"] = ["old", "theirs"]  # their list, without ours
        else:
            self.stored["private_metadata"].update(changes["privateMetadata"])
        return reply  # Clerk's reply to our write looks fine


def test_add_project_puts_back_names_a_racing_write_dropped(clerk):
    user = {"id": "user_alice", "private_metadata": {"stripeId": "cus_1", "projects": ["old"]}}
    fake = _DroppingClerk(user, races=1)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, out = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "mine"}')
    assert (status, out["data"]["projects"]) == (200, ["old", "theirs", "mine"])
    assert user["private_metadata"] == {"stripeId": "cus_1", "projects": ["old", "theirs", "mine"]}


def test_add_project_never_trusts_a_reply_clerk_did_not_store(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": []}}
    fake = _DroppingClerk(user, races=server.PROJECT_TRIES)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, out = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "mine"}')
    assert status == 503 and len(fake.patches) == server.PROJECT_TRIES


def test_add_project_stops_retrying_once_clerk_has_been_slow(clerk, monkeypatch):
    monkeypatch.setattr(server, "CLERK_TIMEOUT", -1.0)  # as if the first write had used it all
    user = {"id": "user_alice", "private_metadata": {"projects": []}}
    fake = _DroppingClerk(user, races=1)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, out = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "mine"}')
    assert status == 503 and "run xo projects" in out["error"] and len(fake.patches) == 1


def test_parallel_adds_on_two_instances_never_report_a_lost_name(clerk, monkeypatch):
    """Two servers (separate locks) adding at once for one user, against one Clerk that replaces lists."""
    monkeypatch.setattr(server, "PROJECT_SETTLE", 0.05)
    stored = {"id": "user_alice", "private_metadata": {"keep": {"a": 1}, "projects": []}}
    guard = threading.Lock()

    class SharedClerk:
        def verify_key(self, secret):
            return KEYS[secret]

        def user(self, user_id):
            with guard:
                snapshot = json.loads(json.dumps(stored))
            time.sleep(0.01)
            return snapshot

        def update_metadata(self, user_id, changes):
            time.sleep(0.01)
            with guard:
                stored["private_metadata"].update(changes["privateMetadata"])
                return json.loads(json.dumps(stored))

    apps = [server.App(server.Config(), clerk=SharedClerk(), audit=lambda line: None) for _ in range(2)]
    results: dict[str, int] = {}

    def add(i: int) -> None:
        body = json.dumps({"name": f"p{i}"}).encode()
        results[f"p{i}"] = apps[i % 2].handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"},
                                              body)[0]

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = stored["private_metadata"]["projects"]
    assert stored["private_metadata"]["keep"] == {"a": 1}
    assert len(names) == len(set(names))  # no duplicates
    assert set(results.values()) <= {200, 503}
    assert {n for n, status in results.items() if status == 200} <= set(names)  # a 200 is never lost


@pytest.mark.parametrize("where", ["user", "update_metadata"])
def test_clerks_own_limit_is_a_429_not_a_degraded_server(clerk, where):
    class Limited(_RacingClerk):
        pass

    def over(*args):
        raise server.ClerkError(429, "too_many_requests")

    fake = Limited({"id": "user_alice", "private_metadata": {}}, races=0)
    setattr(fake, where, over)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, _ = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "a"}')
    assert status == 429 and app.status()["status"] == "ok"


def test_the_servers_own_clerk_budget_is_still_a_503(clerk):
    fake = _RacingClerk({"id": "user_alice", "private_metadata": {}}, races=0)
    fake.update_metadata = lambda *args: (_ for _ in ()).throw(server.BUSY)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, _ = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "a"}')
    assert status == 503


def test_add_project_gives_up_and_says_so_when_the_list_keeps_changing(clerk):
    user = {"id": "user_alice", "private_metadata": {"projects": []}}
    fake = _RacingClerk(user, races=server.PROJECT_TRIES)
    app = server.App(server.Config(), clerk=fake, audit=lambda line: None)
    status, out = app.handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"}, b'{"name": "mine"}')
    assert status == 503 and "run xo projects" in out["error"] and len(fake.patches) == server.PROJECT_TRIES


def test_projects_shows_up_on_the_status_page_as_its_route(xo_server, alice):
    call(xo_server, "GET", "/me/projects", USER_KEY)
    activity = call(xo_server, "GET", "/status")[1]["data"]["activity"]
    assert activity[0]["route"] == "/me/projects"


def test_create_space_with_no_kind_end_to_end(xo_server, clerk, alice, monkeypatch, capfd, tmp_path):
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["create", "space", "dev"]) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out)["name"] == "dev" and json.loads(out)["projects"] == []
    assert json.loads((tmp_path / "dev/.xo/state.json").read_text())["rev"] == 1
    assert [s["name"] for s in alice["private_metadata"]["spaces"]] == ["dev"]
    (tmp_path / "dev").rename(tmp_path / "gone")
    assert cli.main(["create", "space", "dev"]) == 1
    assert "xo install space:dev makes it" in capfd.readouterr().err


def test_xo_projects_end_to_end(xo_server, clerk, alice, monkeypatch, capfd):
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    assert cli.main(["projects"]) == 0
    assert json.loads(capfd.readouterr().out) == []
    assert cli.main(["projects", "add", "my app"]) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out) == [{"name": "my app"}] and "POST" in err
    assert cli.main(["projects", "add", "My App"]) == 1
    assert "already in your projects" in capfd.readouterr().err
    assert alice["private_metadata"] == {"stripeId": "cus_1", "notes": {"a": [1, 2]}, "flag": True,
                                         "projects": ["my app"]}
    assert [c["body"] for c in clerk.calls if c["method"] == "PATCH"] == \
        [{"private_metadata": {"projects": ["my app"]}}]


# --- /me/spaces: spaces recorded in the key's own private metadata ------------------------------

class _Store:
    """A Clerk that keeps one user and merges writes the way Clerk does (a list is replaced whole).
    `after_write` runs after each write, to play another writer."""

    def __init__(self, private: dict, after_write=None) -> None:
        self.stored = {"id": "user_alice", "private_metadata": private}
        self.after_write, self.patches = after_write, []

    def verify_key(self, secret: str) -> dict:
        return KEYS[secret]

    def user(self, user_id: str) -> dict:
        return json.loads(json.dumps(self.stored))

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        self.patches.append(json.loads(json.dumps(changes)))
        self.stored["private_metadata"].update(json.loads(json.dumps(changes["privateMetadata"])))
        if self.after_write:
            self.after_write(self.stored["private_metadata"])
        return json.loads(json.dumps(self.stored))


class _Core:
    def __init__(self, status: int = 201, answer: dict | None = None) -> None:
        self.status = status
        self.answer = answer if answer is not None else {"success": True, "data": {"id": "sp-1", "type": "x"}}
        self.calls: list[tuple] = []

    def forward(self, method, path, token, body):
        self.calls.append((method, path, token, json.loads(body) if body else None))
        return self.status, self.answer


def _app(store, core=None, lines=None):
    return server.App(server.Config(), clerk=store, core=core or _Core(),
                      audit=(lines.append if lines is not None else lambda line: None))


def _post(app, path, body):
    return app.handle("POST", path, {"authorization": f"Bearer {USER_KEY}"}, json.dumps(body).encode())


def _space(name, rev=1, projects=(), **extra):
    return {"name": name, "id": f"id-{name}", "rev": rev, "projects": list(projects), **extra}


def test_create_space_creates_through_xo_with_the_callers_key_and_records_it(clerk):
    store, core, lines = _Store({"stripeId": "cus_1", "projects": ["a"]}), _Core(), []
    status, out = _post(_app(store, core, lines), "/me/spaces",
                        {"kind": "claude-code", "name": "dev", "body": {"name": "my-cc"}})
    assert status == 200
    assert core.calls == [("POST", "/claude-code", USER_KEY, {"name": "my-cc"})]
    entry = {"name": "dev", "kind": "claude-code", "id": "sp-1", "rev": 1, "projects": []}
    assert out["data"] == {"space": entry, "xo": {"id": "sp-1", "type": "x"}}
    assert store.patches == [{"privateMetadata": {"spaces": [entry]}}]
    assert store.stored["private_metadata"] == {"stripeId": "cus_1", "projects": ["a"], "spaces": [entry]}
    assert [json.loads(line)["outcome"] for line in lines] == [200]
    assert not any("dev" in line or "sp-1" in line for line in lines)


@pytest.mark.parametrize("body, status", [
    ({"kind": "rm-rf", "name": "dev"}, 422), ({"kind": "codex", "name": "-dev"}, 422),
    ({"kind": "codex", "name": "a b"}, 422), ({"kind": "codex", "name": "../x"}, 422),
    ({"kind": "codex", "name": "dev", "body": []}, 422), ({"kind": "codex", "name": "dev", "extra": 1}, 422),
    ({"kind": "codex", "name": "Dev"}, 409)])
def test_create_space_refuses_before_xo_creates_anything(clerk, body, status):
    store, core = _Store({"spaces": [_space("dev")]}), _Core()
    assert _post(_app(store, core), "/me/spaces", body)[0] == status
    assert core.calls == [] and store.patches == []


def test_create_space_stops_at_the_cap_before_xo_creates_anything(clerk):
    store, core = _Store({"spaces": [_space(f"s{i}") for i in range(server.MAX_SPACES)]}), _Core()
    assert _post(_app(store, core), "/me/spaces", {"kind": "codex", "name": "one"})[0] == 422
    assert core.calls == [] and store.patches == []


def test_create_space_passes_xos_refusal_back_and_records_nothing(clerk):
    store = _Store({})
    core = _Core(422, {"success": False, "error": "name taken"})
    assert _post(_app(store, core), "/me/spaces", {"kind": "codex", "name": "dev"}) == \
        (422, {"success": False, "error": "name taken"})
    assert store.patches == []


def test_create_space_says_how_to_record_a_space_it_could_not_record(clerk):
    store = _Store({})

    def busy(*args):
        raise server.ClerkError(429, "too_many_requests", seen=429)

    store.update_metadata = busy
    status, out = _post(_app(store), "/me/spaces", {"kind": "codex", "name": "dev"})
    assert status == 429 and "XO created the space (id sp-1)" in out["error"]
    assert "xo connect space:dev --id sp-1" in out["error"]


def test_create_space_gives_no_recovery_hint_that_could_not_work(clerk):
    store = _Store({})

    def full(*args):
        raise server.ClerkError(422, "form_param_value_invalid")

    store.update_metadata = full
    status, out = _post(_app(store), "/me/spaces", {"kind": "codex", "name": "dev"})
    assert status == 422 and "XO created the space (id sp-1)" in out["error"]
    assert "xo connect" not in out["error"]


def test_create_space_checks_what_recording_needs_before_xo_creates_anything(clerk):
    for private, status in (({"xo": "x" * 6080}, 422), ({"spaces": {}}, 409), ({"spaces": ["dev"]}, 409),
                            ({"spaces": [{"name": "a"}, {"name": "A"}]}, 409), ("nope", 409)):
        store, core = _Store(private), _Core(200, {"success": True, "data": {"id": "sp-1"}})
        got, out = _post(_app(store, core), "/me/spaces", {"kind": "codex", "name": "dev"})
        assert got == status and "nothing was created" in out["error"], (private, out)
        assert core.calls == [] and store.patches == []


def test_create_space_refuses_an_answer_without_a_usable_id(clerk):
    store = _Store({})
    status, _ = _post(_app(store, _Core(201, {"success": True, "data": {"id": "../x"}})), "/me/spaces",
                      {"kind": "codex", "name": "dev"})
    assert status == 502 and store.patches == []


def test_connect_space_without_an_id_makes_an_entry_and_asks_xo_nothing(clerk):
    store, core = _Store({"keep": 1}), _Core()
    status, out = _post(_app(store, core), "/me/spaces/connect", {"name": "dev"})
    assert status == 200 and core.calls == []
    assert out["data"]["space"] == {"name": "dev", "rev": 1, "projects": []}
    assert store.stored["private_metadata"] == {"keep": 1, "spaces": [{"name": "dev", "rev": 1, "projects": []}]}
    assert _post(_app(store, core), "/me/spaces/connect", {"name": "DEV"})[0] == 409


def test_connect_space_records_a_space_the_key_can_read(clerk):
    store, core = _Store({}), _Core(200, {"success": True, "data": {"spaceName": "x"}})
    status, out = _post(_app(store, core), "/me/spaces/connect", {"id": "sp-9", "name": "old"})
    assert status == 200 and core.calls == [("GET", "/spaces/sp-9", USER_KEY, None)]
    assert store.stored["private_metadata"]["spaces"] == [{"name": "old", "id": "sp-9", "rev": 1, "projects": []}]


@pytest.mark.parametrize("xo_status", [403, 404])
def test_connect_space_refuses_a_space_the_key_cannot_read(clerk, xo_status):
    store = _Store({})
    assert _post(_app(store, _Core(xo_status, {"success": False})), "/me/spaces/connect",
                 {"id": "sp-9", "name": "old"})[0] == 404
    assert store.patches == []


def test_connect_space_refuses_one_already_recorded(clerk):
    store = _Store({"spaces": [{**_space("dev"), "id": "sp-9"}]})
    assert _post(_app(store, _Core(200, {})), "/me/spaces/connect", {"id": "sp-9", "name": "other"})[0] == 409
    assert _post(_app(store, _Core(200, {})), "/me/spaces/connect", {"id": "sp-8", "name": "DEV"})[0] == 409
    assert _post(_app(store, _Core(200, {})), "/me/spaces/connect", {"id": "../x", "name": "z"})[0] == 422
    assert store.patches == []


def test_connect_project_sets_a_ref_once_per_project_and_counts_the_rev(clerk):
    store = _Store({"projects": ["quirq-ai/xo", "notes"], "spaces": [_space("dev"), _space("ops", rev=4)]})
    app = _app(store)
    assert _post(app, "/me/spaces/dev/projects", {"project": "quirq-ai/xo"})[1]["data"]["space"] == \
        _space("dev", rev=2, projects=[{"quirq-ai/xo": "main"}])
    _post(app, "/me/spaces/dev/projects", {"project": "notes", "ref": "v1.2"})
    _post(app, "/me/spaces/DEV/projects", {"project": "quirq-ai/xo", "ref": "feature/x"})
    spaces = store.stored["private_metadata"]["spaces"]
    assert spaces == [_space("dev", rev=4, projects=[{"quirq-ai/xo": "feature/x"}, {"notes": "v1.2"}]),
                      _space("ops", rev=4)]
    assert all(set(p["privateMetadata"]) == {"spaces"} for p in store.patches)


@pytest.mark.parametrize("path, body, status", [
    ("/me/spaces/nope/projects", {"project": "notes"}, 404),
    ("/me/spaces/dev/projects", {"project": "other"}, 404),
    ("/me/spaces/dev/projects", {"project": "notes", "ref": "--upload-pack=x"}, 422),
    ("/me/spaces/dev/projects", {"project": "notes", "ref": "a..b"}, 422),
    ("/me/spaces/dev/projects", {"project": "notes", "ref": "x.lock"}, 422),
    ("/me/spaces/dev/projects", {"project": "../notes"}, 422),
    ("/me/spaces/dev/projects", {"project": "notes", "rev": 9}, 422),
    ("/me/spaces/a%2Fb/projects", {"project": "notes"}, 422)])
def test_connect_project_refuses_and_writes_nothing(clerk, path, body, status):
    store = _Store({"projects": ["notes"], "spaces": [_space("dev")]})
    assert _post(_app(store), path, body)[0] == status
    assert store.patches == []


def test_connect_project_stops_at_the_cap(clerk):
    full = [{f"p{i}": "main"} for i in range(server.MAX_SPACE_PROJECTS)]
    store = _Store({"projects": ["notes", "p0"], "spaces": [_space("dev", projects=full)]})
    assert _post(_app(store), "/me/spaces/dev/projects", {"project": "notes"})[0] == 422
    assert _post(_app(store), "/me/spaces/dev/projects", {"project": "p0", "ref": "v2"})[0] == 200  # a replace


def test_a_write_past_the_size_budget_is_refused(clerk):
    store = _Store({"blob": "x" * (server.PRIVATE_BUDGET - 20)})
    status, out = _post(_app(store), "/me/projects", {"name": "alpha"})
    assert status == 422 and "past 6KB" in out["error"] and store.patches == []


def test_a_racing_write_that_reverts_another_space_is_undone(clerk):
    before = _space("ops", rev=5, projects=[{"notes": "v2"}])

    def other_writer(private):  # an older list, written by another instance just after ours
        if len(store.patches) == 1:
            private["spaces"] = [_space("dev"), _space("ops", rev=4)]

    store = _Store({"projects": ["notes"], "spaces": [_space("dev"), before]}, other_writer)
    status, out = _post(_app(store), "/me/spaces/dev/projects", {"project": "notes"})
    assert status == 200 and len(store.patches) == 2
    assert store.stored["private_metadata"]["spaces"] == [_space("dev", rev=2, projects=[{"notes": "main"}]), before]


def test_a_space_changed_meanwhile_is_a_409_not_an_overwrite(clerk):
    def other_writer(private):
        private["spaces"] = [_space("dev", rev=9)]

    store = _Store({"projects": ["notes"], "spaces": [_space("dev")]}, other_writer)
    status, out = _post(_app(store), "/me/spaces/dev/projects", {"project": "notes"})
    assert status == 409 and "changed while this ran" in out["error"] and len(store.patches) == 1


def test_a_space_dropped_by_a_racing_write_is_written_again(clerk):
    def other_writer(private):
        if len(store.patches) == 1:
            private["spaces"] = []

    store = _Store({}, other_writer)
    assert _post(_app(store), "/me/spaces", {"kind": "codex", "name": "dev"})[0] == 200
    assert [s["name"] for s in store.stored["private_metadata"]["spaces"]] == ["dev"] and len(store.patches) == 2


def test_spaces_that_are_not_a_list_of_entries_are_never_overwritten(clerk):
    for value in ({"dev": 1}, ["dev"], [{"id": "x"}]):
        store = _Store({"projects": ["notes"], "spaces": value})
        assert _post(_app(store), "/me/spaces/connect", {"id": "sp-1", "name": "a"})[0] == 409
        assert store.patches == []


def test_spaces_with_a_case_duplicate_or_a_project_entry_xo_did_not_write_are_never_overwritten(clerk):
    store = _Store({"projects": ["notes"], "spaces": [_space("dev"), _space("Dev")]})
    assert _post(_app(store), "/me/spaces/connect", {"name": "a"})[0] == 409
    store = _Store({"projects": ["notes", "p2"], "spaces": [_space("dev", projects=[{"p2": "main", "notes": "v1"}])]})
    assert _post(_app(store), "/me/spaces/dev/projects", {"project": "notes"})[0] == 409
    assert store.patches == []


def test_refs_and_ids_that_look_like_credentials_are_refused(clerk):
    store = _Store({"projects": ["notes"], "spaces": [_space("dev")]})
    assert _post(_app(store), "/me/spaces/dev/projects", {"project": "notes", "ref": "ghp_" + "a" * 36})[0] == 422
    assert _post(_app(store, _Core(200, {})), "/me/spaces/connect", {"name": "b", "id": "sk-abc"})[0] == 422
    store2, core = _Store({}), _Core(200, {"success": True, "data": {"id": "ak_" + "b" * 20}})
    assert _post(_app(store2, core), "/me/spaces", {"kind": "codex", "name": "dev"})[0] == 502
    assert store.patches == [] and store2.patches == []


def test_branch_names_that_only_start_like_a_token_are_refs(clerk):
    store = _Store({"projects": ["notes"], "spaces": [_space("dev")]})
    app = _app(store)
    for ref in ("npm_bump", "sk-rewrite", "hf_integration", "pypi-release", "sk-rewrite-auth-flow",
                "npm_bump-lodash-4-17-21", "pypi-release-2026-10", "AKIA-rotation", "ghp_fix/login-page"):
        status, out = _post(app, "/me/spaces/dev/projects", {"project": "notes", "ref": ref})
        assert status == 200 and out["data"]["space"]["projects"] == [{"notes": ref}], ref
    status, out = app.handle("GET", "/me/spaces", {"authorization": f"Bearer {USER_KEY}"}, None)
    assert out["data"]["spaces"][0]["projects"] == [{"notes": "ghp_fix/login-page"}]


# A made-up GitLab token, built here so no scanner reads the file as holding one.
GLPAT = "glpat" + "-2O76UMFxFkM_R5Kjp1vR"


@pytest.mark.parametrize("ref", [GLPAT, "AIzaSy-abc", "ya29.fix", "eyJhbGciOi.eyJzdWIi.x",
                                 "ghp_aB3dE5gH7j/K9mN1pQ3rS5", "sk_live_aB3dE5gH7jK9-mN1pQ3r"])
def test_refs_that_are_tokens_are_refused_however_they_are_cut(clerk, ref):
    store = _Store({"projects": ["notes"], "spaces": [_space("dev")]})
    assert _post(_app(store), "/me/spaces/dev/projects", {"project": "notes", "ref": ref})[0] == 422
    assert store.patches == []


def test_values_someone_else_stored_in_a_spaces_projects_stay_masked(clerk):
    stored = ["https://user:hunter2pass@git.example/x", "postgres://admin:s3cret@db:5432/x", "Bearer abc.def.ghi",
              GLPAT]
    store = _Store({"spaces": [_space("dev", projects=[{f"p{i}": v} for i, v in enumerate(stored)]
                                      + [{"q": "npm_bump"}, {"r": "sk-1234"}])]})
    app = _app(store)
    for path, pick in (("/me/spaces", lambda d: d["spaces"]), ("/me", lambda d: d["privateMetadata"]["spaces"])):
        shown = pick(app.handle("GET", path, {"authorization": f"Bearer {USER_KEY}"}, None)[1]["data"])[0]["projects"]
        assert all(list(p.values())[0] != v for p, v in zip(shown, stored)), shown
        assert shown[-2:] == [{"q": "npm_bump"}, {"r": "sk-1234"}]


def test_create_space_hints_at_connect_when_clerk_fails_or_cannot_be_reached(clerk):
    store = _Store({})

    def down(*args):
        raise server.ClerkError(500, "", seen="timeout")

    store.update_metadata = down
    status, out = _post(_app(store), "/me/spaces", {"kind": "codex", "name": "dev"})
    assert status == 502 and "xo connect space:dev --id sp-1" in out["error"]


def test_project_refs_are_shown_as_stored_whatever_the_project_is_called(clerk):
    sha = "0123456789abcdef0123456789abcdef01234567"
    projects = [{"huggingface/tokenizers": "main"}, {"acme/auth-service": sha}, {"next-auth": "v4"}]
    store = _Store({"projects": ["huggingface/tokenizers", "acme/auth-service", "next-auth"],
                    "spaces": [_space("dev", projects=projects, apiToken="tok-12345678901234")]})
    app = _app(store)
    status, out = app.handle("GET", "/me/spaces", {"authorization": f"Bearer {USER_KEY}"}, None)
    shown = out["data"]["spaces"][0]
    assert status == 200 and shown["projects"] == projects and shown["apiToken"] == "\u20261234"
    status, out = app.handle("GET", "/me", {"authorization": f"Bearer {USER_KEY}"}, None)
    assert out["data"]["privateMetadata"]["spaces"][0]["projects"] == projects
    status, out = _post(app, "/me/spaces/dev/projects", {"project": "next-auth", "ref": "v5"})
    assert out["data"]["space"]["projects"][1] == {"acme/auth-service": sha}


def test_connect_space_with_an_id_gives_an_entry_made_without_one_its_id(clerk):
    store, core = _Store({"spaces": [{"name": "dev", "rev": 1, "projects": [{"a": "main"}]}]}), _Core(200, {})
    status, out = _post(_app(store, core), "/me/spaces/connect", {"name": "DEV", "id": "sp-9"})
    assert status == 200 and core.calls == [("GET", "/spaces/sp-9", USER_KEY, None)]
    assert store.stored["private_metadata"]["spaces"] == [
        {"name": "dev", "rev": 2, "projects": [{"a": "main"}], "id": "sp-9"}]
    assert _post(_app(store, core), "/me/spaces/connect", {"name": "dev", "id": "sp-8"})[0] == 409  # once only
    assert _post(_app(store, core), "/me/spaces/connect", {"name": "dev"})[0] == 409


def test_spaces_lists_and_routes_show_on_the_status_page(xo_server, alice):
    alice["private_metadata"]["spaces"] = [_space("dev")]
    assert call(xo_server, "GET", "/me/spaces", USER_KEY)[1]["data"] == {"spaces": [_space("dev")]}
    assert call(xo_server, "PUT", "/me/spaces", USER_KEY, {})[0] == 405
    assert call(xo_server, "GET", "/me/spaces/connect", USER_KEY)[0] == 405
    call(xo_server, "POST", "/me/spaces/dev/projects", USER_KEY, {"project": "x"})
    routes = [a["route"] for a in call(xo_server, "GET", "/status")[1]["data"]["activity"]]
    assert routes[:2] == ["/me/spaces/{name}/projects", "/me/spaces/connect"]
    assert not any("dev" in line for line in xo_server.audit)


def test_xo_create_connect_and_list_spaces_end_to_end(xo_server, clerk, alice, monkeypatch, capfd):
    monkeypatch.setenv("XO_URL", xo_server.url)
    monkeypatch.setenv("XO_API_KEY", USER_KEY)
    monkeypatch.delenv("XO_SPEC", raising=False)
    alice["private_metadata"]["projects"] = ["notes"]
    assert cli.main(["create", "space", "dev", "claude-code", "--volume-size", "20"]) == 0
    assert [(c["method"], c["path"]) for c in clerk.core] == [("POST", "/core/claude-code")]
    assert json.loads(clerk.core[0]["body"]) == {"name": "dev", "volumeSize": 20}
    assert cli.main(["connect", "project:notes", "space:dev", "--ref", "v1"]) == 0
    capfd.readouterr()
    assert cli.main(["list", "spaces"]) == 0
    out, err = capfd.readouterr()
    _no_secret(out, err)
    assert json.loads(out) == [{"name": "dev", "kind": "claude-code", "id": "space_1", "rev": 2,
                                "projects": [{"notes": "v1"}]}]
    assert {k: v for k, v in alice["private_metadata"].items() if k != "spaces"} == \
        {"stripeId": "cus_1", "notes": {"a": [1, 2]}, "flag": True, "projects": ["notes"]}


def _put(app, path, body):
    return app.handle("PUT", path, {"authorization": f"Bearer {USER_KEY}"}, json.dumps(body).encode())


def test_update_space_replaces_its_projects_only_at_the_rev_sent(clerk):
    store = _Store({"projects": ["a", "b"], "spaces": [_space("dev", rev=3, projects=[{"a": "main"}]),
                                                       _space("ops")]})
    app = _app(store)
    status, out = _put(app, "/me/spaces/dev", {"rev": 3, "projects": [{"b": "v2"}, {"a": "feature"}],
                                               "base": [{"a": "main"}]})
    assert status == 200 and out["data"]["space"] == _space("dev", rev=4, projects=[{"b": "v2"}, {"a": "feature"}])
    assert store.stored["private_metadata"]["spaces"][1] == _space("ops")
    status, out = _put(app, "/me/spaces/dev", {"rev": 3, "projects": [], "base": [{"a": "main"}]})
    assert status == 409 and "run xo sync space:dev" in out["error"] and len(store.patches) == 1


def test_update_space_is_refused_when_the_space_holds_other_projects_at_the_same_rev(clerk):
    # Two writes can both get 200 at one rev; the loser's next update must not overwrite the winner.
    store = _Store({"projects": ["a", "b"], "spaces": [_space("dev", rev=4, projects=[{"b": "v2"}])]})
    status, out = _put(_app(store), "/me/spaces/dev", {"rev": 4, "projects": [{"b": "v3"}], "base": [{"b": "v1"}]})
    assert status == 409 and "changed since you read it" in out["error"] and store.patches == []


def test_update_space_with_nothing_changed_keeps_the_rev(clerk):
    store = _Store({"projects": ["a"], "spaces": [_space("dev", rev=4, projects=[{"a": "main"}])]})
    status, out = _put(_app(store), "/me/spaces/dev", {"rev": 4, "projects": [{"a": "main"}],
                                                        "base": [{"a": "main"}]})
    assert status == 200 and out["data"]["space"]["rev"] == 4


def test_a_space_named_connect_can_be_updated(clerk):
    store = _Store({"projects": ["a"], "spaces": [_space("connect")]})
    status, out = _put(_app(store), "/me/spaces/connect", {"rev": 1, "projects": [{"a": "main"}], "base": []})
    assert status == 200 and out["data"]["space"]["rev"] == 2


def test_a_json_number_too_long_to_read_is_refused_not_a_500(clerk):
    store = _Store({"spaces": [_space("dev")]})
    body = b'{"rev": ' + b"9" * 5000 + b', "projects": [], "base": []}'
    status, _ = _app(store).handle("PUT", "/me/spaces/dev", {"authorization": f"Bearer {USER_KEY}"}, body)
    assert status == 422
    status, _ = _app(store).handle("POST", "/me/projects", {"authorization": f"Bearer {USER_KEY}"},
                                   b'{"name": ' + b"9" * 5000 + b"}")
    assert status == 422


@pytest.mark.parametrize("path, body, status", [
    ("/me/spaces/dev", {"rev": 1, "projects": [{"c": "main"}]}, 404),
    ("/me/spaces/nope", {"rev": 1, "projects": []}, 404),
    ("/me/spaces/dev", {"rev": 1, "projects": [{"a": "main"}, {"a": "v2"}]}, 422),
    ("/me/spaces/dev", {"rev": 1, "projects": [{"a": "--x"}]}, 422),
    ("/me/spaces/dev", {"rev": 1, "projects": [{"a": "ghp_" + "a" * 36}]}, 422),
    ("/me/spaces/dev", {"rev": 1, "projects": [{"a": "main", "b": "main"}]}, 422),
    ("/me/spaces/dev", {"rev": "1", "projects": []}, 422),
    ("/me/spaces/dev", {"rev": True, "projects": []}, 422),
    ("/me/spaces/dev", {"rev": 1, "projects": [], "name": "x"}, 422),
    ("/me/spaces/dev", {"rev": 1, "projects": [{f"p{i}": "main"} for i in range(11)]}, 422)])
def test_update_space_refuses_and_writes_nothing(clerk, path, body, status):
    store = _Store({"projects": ["a", "b"], "spaces": [_space("dev")]})
    assert _put(_app(store), path, {**body, "base": []})[0] == status
    assert store.patches == []


def test_update_space_route_shows_on_the_status_page(xo_server, alice):
    assert call(xo_server, "POST", "/me/spaces/dev", USER_KEY, {})[0] == 405
    call(xo_server, "PUT", "/me/spaces/dev", USER_KEY, {"rev": 1, "projects": []})
    assert call(xo_server, "GET", "/status")[1]["data"]["activity"][0]["route"] == "/me/spaces/{name}"


# --- sharing: invites in another user's `shared` -------------------------------------------------

BOB_KEY = "ak_bob_secret_never_printed"
MALLORY_KEY = "ak_mallory_secret_never_printed"
_MORE_KEYS = {BOB_KEY: {"id": "ak_bob", "subject": "user_bob", "claims": None, "revoked": False, "expired": False},
              MALLORY_KEY: {"id": "ak_mal", "subject": "user_mallory", "claims": None, "revoked": False,
                            "expired": False}}


class _Users:
    """A Clerk with several users, merging writes the way Clerk does. `after_write(user_id, private)`
    runs after each write, to play another writer."""

    def __init__(self, users: dict, after_write=None) -> None:
        self.users = {uid: {"id": uid, "username": name, "private_metadata": private}
                      for uid, (name, private) in users.items()}
        self.after_write, self.patches, self.lookups = after_write, [], []

    def verify_key(self, secret: str) -> dict:
        return {**KEYS, **_MORE_KEYS}[secret]

    def user(self, user_id: str) -> dict:
        if user_id not in self.users:
            raise server.ClerkError(404)
        return json.loads(json.dumps(self.users[user_id]))

    def users_named(self, username: str) -> list:
        return [json.loads(json.dumps(u)) for u in self.users.values() if u["username"] == username]

    def users_by_id(self, ids: list) -> list:
        self.lookups.append(list(ids))
        return [json.loads(json.dumps(u)) for uid, u in self.users.items() if uid in ids]

    def update_metadata(self, user_id: str, changes: dict) -> dict:
        self.patches.append((user_id, json.loads(json.dumps(changes))))
        self.users[user_id]["private_metadata"].update(json.loads(json.dumps(changes["privateMetadata"])))
        if self.after_write:
            self.after_write(user_id, self.users[user_id]["private_metadata"])
        return self.user(user_id)


@pytest.fixture
def no_floor(clerk, monkeypatch):
    monkeypatch.setattr(server, "SHARE_FLOOR", 0)


def _as(app, key, path, body=None, method="POST"):
    return app.handle(method, path, {"authorization": f"Bearer {key}"},
                      None if body is None else json.dumps(body).encode())


def _two(alice=None, bob=None, after_write=None):
    return _Users({"user_alice": ("alice", {"projects": ["o/a", "notes"],
                                            "spaces": [_space("dev", rev=3, projects=[{"o/a": "main"}, {"notes": "v1"}])],
                                            **(alice or {})}),
                   "user_bob": ("bob", {"stripeId": "cus_9", **(bob or {})}),
                   "user_mallory": ("mallory", {"projects": ["evil/repo"],
                                                "spaces": [_space("dev", projects=[{"evil/repo": "main"}])]})},
                  after_write)


def _ids(app, key=BOB_KEY):
    return {e["item"] + "/" + e["from"]: e["id"] for e in _as(app, key, "/me/shared", method="GET")[1]["data"]["shared"]}


def _invites(users, uid="user_bob"):
    return users.users[uid]["private_metadata"].get("shared", [])


def test_share_puts_one_invite_in_the_recipients_shared_and_nothing_else(no_floor):
    users, lines = _two(), []
    status, out = _as(_app(users, lines=lines), USER_KEY, "/me/share", {"item": "space:DEV", "to": "bob"})
    # The local project stays the sharer's: nobody else could install it.
    assert status == 200 and out["data"]["shared"] == {"to": "bob", "item": "space:dev", "projects": [{"o/a": "main"}],
                                                        "localProjectsLeftOut": 1}
    assert [uid for uid, _ in users.patches] == ["user_bob"]
    assert list(users.patches[0][1]["privateMetadata"]) == ["shared"]
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["stripeId"] == "cus_9"
    (invite,) = bob["shared"]
    assert {k: v for k, v in invite.items() if k != "at"} == \
        {"from": "user_alice", "fromName": "alice", "item": "space:dev", "projects": [{"o/a": "main"}]}
    assert "id-dev" not in json.dumps(bob) and "notes" not in json.dumps(bob)
    assert not any("user_bob" in line or "dev" in line for line in lines)


def test_sharing_again_unchanged_writes_nothing_and_a_change_waits_a_minute(no_floor, monkeypatch):
    other = {"from": "user_carol", "fromName": "carol", "item": "project:c/x", "at": "2026-01-01T00:00:00Z"}
    users = _two(bob={"shared": [other]})
    app = _app(users)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert len(users.patches) == 1
    users.users["user_alice"]["private_metadata"]["spaces"][0]["projects"] = [{"o/a": "v2"}]
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 404
    clock = time.time() + server.RESHARE_WAIT + 1
    monkeypatch.setattr(server.time, "time", lambda: clock)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert _as(app, USER_KEY, "/me/share", {"item": "project:o/a", "to": "bob"})[0] == 200
    assert [(e["item"], e.get("projects")) for e in _invites(users)] == \
        [("project:c/x", None), ("space:dev", [{"o/a": "v2"}]), ("project:o/a", None)]


def _invite(i, sender="user_x", size=0):
    return {"from": f"{sender}{i}", "fromName": f"u{i}", "item": f"project:o/x{i}", "at": "2026-01-01T00:00:00Z",
            **({"pad": "p" * size} if size else {})}


@pytest.mark.parametrize("to, bob", [
    ("nobody", None), ("bo b", None), ("alice", None),
    ("bob", {"shared": [_invite(i) for i in range(server.MAX_INVITES)]}),
    ("bob", {"shared": [{**_invite(i), "from": "user_alice"} for i in range(server.MAX_INVITES_FROM)]}),
    ("bob", {"shared": "not a list"}), ("bob", {"shared": [{"odd": 1}]}),
    ("bob", {"shared": [{**_invite(0), "item": "project:o/" + "y" * 99, "fromName": "f" * 60} for _ in range(1)]
             + [{**_invite(i), "item": "project:o/" + "z" * 99 + str(i)[:0]} for i in range(1, 9)]}),
    ("bob", {"filler": "x" * server.PRIVATE_BUDGET})])
def test_every_refusal_about_the_recipient_is_the_same_answer_and_writes_nothing(no_floor, to, bob):
    users = _two(bob=bob)
    status, out = _as(_app(users), USER_KEY, "/me/share", {"item": "space:dev", "to": to})
    assert (status, out["error"]) == (404, f"Could not share space:dev with {to}")
    assert users.patches == []


def test_invites_count_toward_the_recipients_6kb_and_answering_gives_it_back(no_floor):
    # bob is near his 6KB, so an invite cannot come in; with room for it, it fills his budget,
    # and his own writes are refused until he answers it.
    users = _two(bob={"filler": "x" * (server.PRIVATE_BUDGET - 60), "projects": []})
    app = _app(users)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 404
    bob = users.users["user_bob"]["private_metadata"]
    bob["filler"] = "x" * (server.PRIVATE_BUDGET - 400)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    bob = users.users["user_bob"]["private_metadata"]
    bob["filler"] += "x" * (server.PRIVATE_BUDGET - 2 - server._size(bob))  # 2 bytes of room left
    status, out = _as(app, BOB_KEY, "/me/projects", {"name": "mine"})
    assert status == 422 and "Invites waiting take" in out["error"] and "xo reject" in out["error"]
    (handle,) = _ids(app).values()
    assert _as(app, BOB_KEY, "/me/shared/reject", {"id": handle})[0] == 200
    assert _as(app, BOB_KEY, "/me/projects", {"name": "mine"})[0] == 200


def test_an_old_reject_mark_is_passed_over_and_dropped_on_the_next_write(no_floor):
    mark = {"block": "0123456789abcdef", "until": "2026-01-01T00:00:00Z"}
    users = _two(bob={"shared": [mark]})
    app = _app(users)
    assert _as(app, BOB_KEY, "/me/shared", method="GET")[1]["data"]["shared"] == []
    status, me = _as(app, BOB_KEY, "/me", method="GET")
    assert status == 200 and "0123456789abcdef" not in json.dumps(me)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert [e["from"] for e in _invites(users)] == ["user_alice"]


def test_reject_takes_off_every_invite_with_that_id(no_floor, monkeypatch):
    users = _two()
    app = _app(users)
    monkeypatch.setattr(server, "_handle", lambda e: "f" * server.INVITE_ID_LEN)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert _as(app, MALLORY_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    handle = "f" * server.INVITE_ID_LEN
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle})
    assert status == 409 and "xo reject" in out["error"] and len(_invites(users)) == 2
    assert _as(app, BOB_KEY, "/me/shared/reject", {"id": handle})[0] == 200
    assert _invites(users) == []


def test_shared_falls_back_to_the_names_shared_as_when_the_lookup_fails(no_floor, monkeypatch):
    users = _two()
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})

    def down(ids):
        raise server.ClerkError(503)
    monkeypatch.setattr(users, "users_by_id", down)
    status, out = _as(app, BOB_KEY, "/me/shared", method="GET")
    (e,) = out["data"]["shared"]
    assert status == 200 and e["from"] == "alice" and e["senderUnchecked"] is True and "senderGone" not in e


def test_an_invite_too_big_is_refused_before_looking_anyone_up(no_floor):
    users = _two(alice={"projects": [f"o/{'r' * 90}{i}" for i in range(10)],
                        "spaces": [_space("dev", projects=[{f"o/{'r' * 90}{i}": "b" * 60} for i in range(10)])]})
    users.users_named = lambda name: pytest.fail("looked up a user")
    status, out = _as(_app(users), USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    assert status == 422 and "too big" in out["error"]


def test_a_recipient_whose_clerk_write_limit_is_spent_gets_the_same_answer(no_floor):
    users = _two()

    def busy(user_id, changes):
        raise server.ClerkError(429)

    users.update_metadata = busy
    status, out = _as(_app(users), USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    assert (status, out["error"]) == (404, "Could not share space:dev with bob")


@pytest.mark.parametrize("body, status", [
    ({"item": "space:nope", "to": "bob"}, 404), ({"item": "project:zz/q", "to": "bob"}, 404),
    ({"item": "project:notes", "to": "bob"}, 422), ({"item": "dev", "to": "bob"}, 422),
    ({"item": "space:../x", "to": "bob"}, 422), ({"item": "space:dev"}, 422),
    ({"item": "space:dev", "to": "bob", "projects": []}, 422)])
def test_share_refuses_what_the_caller_does_not_have_before_looking_anyone_up(no_floor, body, status):
    users = _two()
    users.users_named = lambda name: pytest.fail("looked up a user")
    assert _as(_app(users), USER_KEY, "/me/share", body)[0] == status
    assert users.patches == []


def test_share_needs_a_username_to_say_who_it_is_from(no_floor):
    users = _two()
    users.users["user_alice"]["username"] = None
    status, out = _as(_app(users), USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    assert status == 409 and "no username" in out["error"]


def test_share_takes_the_same_time_whatever_the_answer_once_the_key_is_known(clerk, monkeypatch):
    monkeypatch.setattr(server, "SHARE_FLOOR", 0.2)
    app = _app(_two())
    for to in ("nobody", "bob"):
        started = time.monotonic()
        _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": to})
        assert time.monotonic() - started >= 0.2
    started = time.monotonic()
    assert app.handle("POST", "/me/share", {}, b"{}")[0] == 401
    assert time.monotonic() - started < 0.2


def test_a_share_racing_the_recipients_own_write_is_written_again(no_floor):
    def racer(user_id, private):
        if user_id == "user_bob" and racer.once:
            racer.once = False
            private["shared"] = []  # the recipient's own write, from an older read

    racer.once = True
    users = _two(after_write=racer)
    assert _as(_app(users), USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert [e["item"] for e in _invites(users)] == ["space:dev"]
    assert len(users.patches) == 2


def _spaces_racer(spaces):
    """Plays bob writing `spaces` from another instance, from a read older than accept's write."""
    def racer(user_id, private):
        if user_id == "user_bob" and "shared" in private and private["shared"] == [] and racer.once:
            racer.once = False
            private["spaces"] = json.loads(json.dumps(spaces))
    racer.once = True
    return racer


@pytest.mark.parametrize("item, body, want", [
    ("space:dev", {}, [_space("work"), {"name": "dev", "rev": 1, "projects": [{"o/a": "main"}]}]),
    ("project:o/a", {"space": "work"}, [_space("work", rev=2, projects=[{"o/a": "main"}])])])
def test_an_accept_that_loses_its_space_to_a_racing_write_puts_it_back(no_floor, item, body, want):
    users = _two(bob={"spaces": [_space("work")]}, after_write=_spaces_racer([_space("work")]))
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": item, "to": "bob"})
    (handle,) = _ids(app).values()
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, **body})
    assert status == 200 and out["data"]["accepted"]["projectsAdded"] == ["o/a"]
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["spaces"] == want and bob["projects"] == ["o/a"] and bob["shared"] == []
    assert set(users.patches[-1][1]["privateMetadata"]) == {"spaces"}


def test_an_accept_whose_space_name_was_taken_meanwhile_says_so(no_floor):
    taken = [_space("dev")]
    users = _two(after_write=_spaces_racer(taken))
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle})
    assert status == 409 and "xo connect" in out["error"]
    assert users.users["user_bob"]["private_metadata"]["spaces"] == taken


def test_shared_lists_each_invite_with_an_id_and_the_senders_username_now(no_floor):
    users = _two()
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    users.users["user_alice"]["username"] = "alice2"
    status, out = _as(app, BOB_KEY, "/me/shared", method="GET")
    assert status == 200 and users.lookups == [["user_alice"]]
    (e,) = out["data"]["shared"]
    assert (e["from"], e["sharedAs"], e["item"], e["projects"]) == ("alice2", "alice", "space:dev", [{"o/a": "main"}])
    assert re.fullmatch(r"[0-9a-f]{16}", e["id"])
    _, me = _as(app, BOB_KEY, "/me", method="GET")
    assert "user_alice" not in json.dumps(out) and "user_alice" not in json.dumps(me)


def test_a_look_alike_sender_is_told_apart_by_id_and_current_name(no_floor, monkeypatch):
    """mallory shares while named alice, renames away, and the real alice shares: bob sees two
    invites with different ids, and mallory's under her name now."""
    users = _two()
    app = _app(users)
    users.users["user_mallory"]["username"] = "alice"
    users.users["user_alice"]["username"] = "alice-old"
    assert _as(app, MALLORY_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    users.users["user_mallory"]["username"] = "mallory"
    users.users["user_alice"]["username"] = "alice"
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    ids = _ids(app)
    assert set(ids) == {"space:dev/mallory", "space:dev/alice"} and len(set(ids.values())) == 2
    assert _as(app, BOB_KEY, "/me/shared/accept", {"id": ids["space:dev/alice"], "as": "dev"})[0] == 200
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["spaces"] == [{"name": "dev", "rev": 1, "projects": [{"o/a": "main"}]}]
    assert [e["from"] for e in _invites(users)] == ["user_mallory"]


def test_accept_is_one_write_of_spaces_projects_and_shared(no_floor):
    users = _two()
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle})
    assert status == 200 and out["data"]["accepted"] == {
        "id": handle, "item": "space:dev", "space": {"name": "dev", "rev": 1, "projects": [{"o/a": "main"}]},
        "projectsAdded": ["o/a"]}
    assert len(users.patches) == 2
    uid, patch = users.patches[1]
    assert uid == "user_bob" and set(patch["privateMetadata"]) == {"spaces", "projects", "shared"}
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["projects"] == ["o/a"] and bob["shared"] == [] and bob["stripeId"] == "cus_9"
    assert users.users["user_alice"]["private_metadata"]["spaces"][0]["rev"] == 3


@pytest.mark.parametrize("bob, body, status", [
    ({"spaces": [_space("dev")]}, {}, 409),
    ({"spaces": [_space(f"s{i}") for i in range(server.MAX_SPACES)]}, {}, 422),
    ({"projects": [f"p{i}" for i in range(server.MAX_PROJECTS)]}, {}, 422),
    ({"later": "x" * (server.PRIVATE_BUDGET - 60)}, {}, 422),
    ({}, {"as": "../x"}, 422)])
def test_a_refused_accept_leaves_nothing_behind(no_floor, bob, body, status):
    later = bob.pop("later", None)
    users = _two(bob=bob)
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    if later:  # bob's own metadata grew after the invite came
        users.users["user_bob"]["private_metadata"]["filler"] = later
    before = json.loads(json.dumps(users.users["user_bob"]["private_metadata"]))
    assert _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, **body})[0] == status
    assert users.users["user_bob"]["private_metadata"] == before


def test_accept_with_a_name_you_already_use_takes_as(no_floor):
    users = _two(bob={"spaces": [_space("dev")]})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    assert _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, "as": "dev2"})[0] == 200
    assert [s["name"] for s in users.users["user_bob"]["private_metadata"]["spaces"]] == ["dev", "dev2"]


def test_accept_puts_a_project_in_the_space_you_name_and_in_your_projects(no_floor):
    users = _two(bob={"projects": ["b/x"], "spaces": [_space("work", rev=4, projects=[{"b/x": "main"}])]})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "project:o/a", "to": "bob"})
    (handle,) = _ids(app).values()
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, "space": "work", "ref": "v2"})
    assert status == 200 and out["data"]["accepted"] == {
        "id": handle, "item": "project:o/a",
        "space": {"name": "work", "id": "id-work", "rev": 5, "projects": [{"b/x": "main"}, {"o/a": "v2"}]}, "projectsAdded": ["o/a"]}
    uid, patch = users.patches[-1]
    assert uid == "user_bob" and set(patch["privateMetadata"]) == {"spaces", "projects", "shared"}
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["projects"] == ["b/x", "o/a"] and bob["shared"] == []
    assert bob["spaces"] == [{"name": "work", "id": "id-work", "rev": 5, "projects": [{"b/x": "main"}, {"o/a": "v2"}]}]


def test_accept_a_project_already_in_that_space_replaces_its_ref_in_place(no_floor):
    users = _two(bob={"projects": ["o/a", "b/x"],
                      "spaces": [_space("work", rev=2, projects=[{"o/a": "old"}, {"b/x": "main"}])]})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "project:o/a", "to": "bob"})
    (handle,) = _ids(app).values()
    status, out = _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, "space": "WORK"})
    assert status == 200 and out["data"]["accepted"]["projectsAdded"] == []
    bob = users.users["user_bob"]["private_metadata"]
    assert bob["projects"] == ["o/a", "b/x"]
    assert bob["spaces"] == [{"name": "work", "id": "id-work", "rev": 3, "projects": [{"o/a": "main"}, {"b/x": "main"}]}]


@pytest.mark.parametrize("bob, body, status", [
    ({}, {}, 422),
    ({}, {"as": "x", "space": "work"}, 422),
    ({}, {"space": "nope"}, 404),
    ({}, {"space": "../x"}, 422),
    ({}, {"space": "work", "ref": "a..b"}, 422),
    ({"spaces": [_space("work", projects=[{f"o/p{i}": "main"} for i in range(server.MAX_SPACE_PROJECTS)])]},
     {"space": "work"}, 422),
    ({"projects": [f"p{i}" for i in range(server.MAX_PROJECTS)]}, {"space": "work"}, 422)])
def test_a_refused_project_accept_leaves_nothing_behind(no_floor, bob, body, status):
    users = _two(bob={"spaces": [_space("work")], **bob})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "project:o/a", "to": "bob"})
    (handle,) = _ids(app).values()
    before = json.loads(json.dumps(users.users["user_bob"]["private_metadata"]))
    assert _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, **body})[0] == status
    assert users.users["user_bob"]["private_metadata"] == before


def test_accept_of_a_space_takes_no_space_or_ref(no_floor):
    users = _two(bob={"spaces": [_space("work")]})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    for body in ({"space": "work"}, {"ref": "main"}):
        assert _as(app, BOB_KEY, "/me/shared/accept", {"id": handle, **body})[0] == 422
    assert len(_invites(users)) == 1


@pytest.mark.parametrize("projects", [[{"o/a": "--upload-pack=x"}], [{"notes": "main"}], [{"o/a": "main"}, {"o/a": "v1"}]])
def test_accept_refuses_an_invite_xo_cannot_use(no_floor, projects):
    bad = {"from": "user_alice", "fromName": "alice", "item": "space:dev", "at": "2026-01-01T00:00:00Z",
           "projects": projects}
    users = _two(bob={"shared": [bad]})
    status, out = _as(_app(users), BOB_KEY, "/me/shared/accept", {"id": server._handle(bad)})
    assert status == 409 and "reject it" in out["error"] and users.patches == []


def test_an_invite_changed_after_listing_is_not_accepted(no_floor, monkeypatch):
    users = _two()
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    (handle,) = _ids(app).values()
    users.users["user_alice"]["private_metadata"]["spaces"][0]["projects"] = [{"o/a": "evil"}]
    clock = time.time() + server.RESHARE_WAIT + 1
    monkeypatch.setattr(server.time, "time", lambda: clock)
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert _as(app, BOB_KEY, "/me/shared/accept", {"id": handle})[0] == 404


def test_reject_takes_only_that_invite_off_and_the_sender_can_share_again(no_floor):
    keep = {"from": "user_carol", "fromName": "carol", "item": "project:c/x", "at": "2026-01-01T00:00:00Z"}
    users = _two(bob={"shared": [keep]})
    app = _app(users)
    _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})
    handle = _ids(app)["space:dev/alice"]
    assert _as(app, BOB_KEY, "/me/shared/reject", {"id": handle}) == \
        (200, {"success": True, "data": {"rejected": {"id": handle, "item": "space:dev"}}})
    assert _invites(users) == [keep] and "spaces" not in users.users["user_bob"]["private_metadata"]
    assert _as(app, BOB_KEY, "/me/shared/reject", {"id": handle})[0] == 404
    # Nothing is kept of the reject: the same share goes straight back on bob's list.
    assert _as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "bob"})[0] == 200
    assert [e["item"] for e in _invites(users)] == ["project:c/x", "space:dev"]


def test_reject_takes_off_an_invite_xo_could_not_accept(no_floor):
    bad = {"from": "user_alice", "fromName": "alice", "item": "space:dev", "at": "2026-01-01T00:00:00Z",
           "projects": [{"o/a": "--upload-pack=x"}]}
    users = _two(bob={"shared": [bad]})
    assert _as(_app(users), BOB_KEY, "/me/shared/reject", {"id": server._handle(bad)})[0] == 200
    assert _invites(users) == []


def test_answers_take_an_id(no_floor):
    app = _app(_two())
    for body in ({"id": "nothex!!"}, {"item": "space:dev", "from": "alice"}, {"id": "abc"}):
        assert _as(app, BOB_KEY, "/me/shared/reject", body)[0] == 422


def test_share_is_limited_to_ten_a_minute_per_key(no_floor):
    app = _app(_two())
    answers = [_as(app, USER_KEY, "/me/share", {"item": "space:dev", "to": "nobody"})[0] for _ in range(11)]
    assert answers[:10] == [404] * 10 and answers[10] == 429


# Browser login: GET /login and POST /login/browser.
LOGIN = {"code": "code_1", "verifier": VERIFIER, "redirect": REDIRECT, "name": "xo CLI on mbp"}


def test_browser_login_is_off_unless_both_settings_are_set(xo_server, clerk):
    assert call(xo_server, "GET", "/login") == (200, {"success": True, "data": {"browser": None}})
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 404 and "xo login --key" in body["error"] and clerk.fapi == [] and clerk.calls == []


def test_get_login_lists_clerks_sign_in(browser_on, xo_server):
    status, body = call(xo_server, "GET", "/login")
    base = os.environ["CLERK_API_URL"]
    assert status == 200 and body["data"]["browser"] == {
        "authorize": base + "/fapi/oauth/authorize", "clientId": CLIENT_ID, "scope": "openid profile email",
        "redirects": list(server.LOGIN_REDIRECTS), "keyDays": 90}
    assert browser_on.fapi == [] and browser_on.calls == []  # no Clerk call to say where to sign in


def test_browser_login_swaps_the_code_and_makes_the_user_a_key(browser_on, xo_server, capfd):
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert (status, body) == (200, {"success": True, "data": {"key": MINTED, "days": 90, "reused": False}})
    token, info = browser_on.fapi
    assert token["form"] == {"grant_type": "authorization_code", "code": "code_1", "redirect_uri": REDIRECT,
                             "client_id": CLIENT_ID, "code_verifier": VERIFIER}
    assert "Authorization" not in token["headers"]  # a public client: no secret goes to the Frontend API
    assert info["headers"]["Authorization"] == "Bearer oat_user_alice"
    # One key, for the user who signed in, with no claims (never an admin key) and an expiry.
    assert browser_on.minted == [{"name": "xo CLI on mbp", "subject": "user_alice",
                                  "description": "made by xo login in a browser",
                                  "seconds_until_expiration": 90 * 86400}]
    assert audits(xo_server)[-1] | {"time": None} == {"time": None, "method": "POST", "route": "/login/browser",
                                                      "keyId": "ak_new_1", "caller": None, "target": "user_alice",
                                                      "outcome": 200, "reused": False}
    # The code works once: a second swap makes no key.
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 401 and "run xo login again" in body["error"] and len(browser_on.minted) == 1


def test_a_code_without_its_verifier_makes_no_key(browser_on, xo_server):
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN | {"verifier": "w" * 43})
    assert status == 401 and browser_on.minted == [] and len(browser_on.fapi) == 1
    assert audits(xo_server)[-1]["outcome"] == 401 and audits(xo_server)[-1]["clerk"] == 400


@pytest.mark.parametrize("change, status", [
    ({"code": ""}, 400), ({"code": "a b"}, 400), ({"code": 5}, 400), ({"verifier": "v" * 42}, 400),
    ({"verifier": "v" * 129}, 400), ({"redirect": "http://127.0.0.1:9999/callback"}, 400),
    ({"redirect": "https://evil.example/callback"}, 400), ({"name": " lead"}, 400), ({"name": "x" * 41}, 400),
    ({"extra": 1}, 422)])
def test_browser_login_refuses_bad_bodies_before_calling_clerk(browser_on, xo_server, change, status):
    assert call(xo_server, "POST", "/login/browser", body=LOGIN | change)[0] == status
    assert browser_on.fapi == [] and browser_on.minted == []


def test_browser_login_refuses_a_body_that_is_not_json(browser_on, xo_server):
    assert call(xo_server, "POST", "/login/browser", raw=b"{")[0] == 422
    assert call(xo_server, "POST", "/login/browser", body=[1])[0] == 422
    assert call(xo_server, "GET", "/login/browser")[0] == 405 and call(xo_server, "POST", "/login")[0] == 405
    assert browser_on.fapi == []


@pytest.mark.parametrize("answer", [(200, {"sub": "org_1"}), (200, {"sub": 5}), (200, {}), (500, {})])
def test_who_signed_in_must_be_a_user(browser_on, xo_server, answer):
    browser_on.userinfo_answer = answer
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 502 and body["error"] == "Clerk's sign-in did not answer" and browser_on.minted == []


@pytest.mark.parametrize("answer", [(200, {"token_type": "Bearer"}), (503, {}), (302, {})])
def test_a_token_answer_without_a_token_makes_no_key(browser_on, xo_server, answer):
    browser_on.token_answer = answer
    assert call(xo_server, "POST", "/login/browser", body=LOGIN)[0] == 502
    assert len(browser_on.fapi) == 1 and browser_on.minted == []


@pytest.mark.parametrize("answer", [
    (200, {"id": "ak_new_1", "subject": "user_bob", "secret": "ak_" + "for_bob"}),
    (200, {"id": "ak_new_1", "subject": "user_alice"}), (200, {"id": "ak_new_1", "subject": "user_alice",
                                                                "secret": "sk_" + "not_a_key"})])
def test_a_key_clerk_did_not_make_for_this_user_is_not_passed_on(browser_on, xo_server, answer):
    browser_on.mint_answer = answer
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 502 and "key" not in body.get("data", {}) and "for_bob" not in json.dumps(body)
    assert audits(xo_server)[-1]["outcome"] == 502 and audits(xo_server)[-1]["clerk"] == "no key"


def test_clerk_refusing_to_make_a_key_says_why_on_stderr(browser_on, xo_server, capfd):
    browser_on.mint_answer = (422, {"errors": [{"code": "feature_not_enabled"}]})
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 502 and body["error"] == "Clerk would not make an API key for this account"
    err = capfd.readouterr().err
    assert "Clerk would not make an API key (422, feature_not_enabled)" in err and "API keys are turned on" in err


@pytest.mark.parametrize("env, why", [
    ({"XO_OAUTH_CLIENT_ID": "cli_1"}, "XO_CLERK_FRONTEND_URL is not set"),
    ({"XO_CLERK_FRONTEND_URL": "https://clerk.example.test"}, "XO_OAUTH_CLIENT_ID is not set"),
    ({"XO_OAUTH_CLIENT_ID": "cli 1", "XO_CLERK_FRONTEND_URL": "https://clerk.example.test"}, "is not a client id"),
    ({"XO_OAUTH_CLIENT_ID": "cli_1", "XO_CLERK_FRONTEND_URL": "http://clerk.example.test"}, "must be https://")])
def test_a_bad_browser_login_setting_turns_it_off_not_the_server(clerk, monkeypatch, capsys, env, why):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg = server.Config()
    assert (cfg.oauth_client_id, cfg.frontend_url) == (None, None)
    assert "xo serve: browser login is off: " in capsys.readouterr().err
    assert server.App(cfg).handle("GET", "/login", {}, None) == (200, {"success": True, "data": {"browser": None}})


def test_a_bad_browser_login_setting_names_the_problem(clerk, monkeypatch, capsys):
    monkeypatch.setenv("XO_OAUTH_CLIENT_ID", "cli_1")
    monkeypatch.setenv("XO_CLERK_FRONTEND_URL", "http://clerk.example.test")
    server.Config()
    assert "XO_CLERK_FRONTEND_URL must be https://" in capsys.readouterr().err


def test_login_routes_are_named_on_the_status_page():
    assert server._route_name("/login") == "/login" and server._route_name("/login/browser") == "/login/browser"


def _key(id_, **extra):
    return {"id": id_, "subject": "user_alice", "claims": None, "revoked": False, "expired": False,
            "created_at": 1, **extra}


def test_browser_login_hands_back_a_key_the_user_already_has(browser_on, xo_server):
    browser_on.user_keys = [_key("ak_old_1", description=server.LOGIN_KEY_NOTE)]
    browser_on.secrets = {"ak_old_1": OLD}
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert (status, body) == (200, {"success": True, "data": {"key": OLD, "days": None, "reused": True}})
    assert browser_on.minted == []
    listed = [c["path"] for c in browser_on.calls]
    assert listed == ["/v1/api_keys?subject=user_alice&limit=100", "/v1/api_keys/ak_old_1/secret"]
    assert audits(xo_server)[-1]["keyId"] == "ak_old_1" and audits(xo_server)[-1]["reused"] is True


def test_an_xo_made_key_comes_first_then_the_newest(browser_on, xo_server):
    browser_on.user_keys = [_key("ak_a", created_at=5), _key("ak_xo", description=server.LOGIN_KEY_NOTE, created_at=1),
                            _key("ak_b", created_at=9)]
    browser_on.secrets = {"ak_a": "ak_" + "a1", "ak_xo": OLD, "ak_b": "ak_" + "b1"}
    assert call(xo_server, "POST", "/login/browser", body=LOGIN)[1]["data"]["key"] == OLD
    browser_on.codes = {"code_1": "user_alice"}
    browser_on.user_keys = [_key("ak_a", created_at=5), _key("ak_b", created_at=9)]
    assert call(xo_server, "POST", "/login/browser", body=LOGIN)[1]["data"]["key"] == "ak_" + "b1"


@pytest.mark.parametrize("key", [_key("ak_x", claims={"xo_admin": True}), _key("ak_x", revoked=True),
                                 _key("ak_x", expired=True), _key("ak_x", subject="user_bob"), _key(5)])
def test_a_key_that_is_not_safe_to_hand_back_is_skipped(browser_on, xo_server, key):
    browser_on.user_keys = [key]
    browser_on.secrets = {"ak_x": OLD}
    assert call(xo_server, "POST", "/login/browser", body=LOGIN)[1]["data"]["key"] == MINTED
    assert not any(c["path"].endswith("/secret") for c in browser_on.calls)


@pytest.mark.parametrize("secret", [None, (403, {"errors": [{"code": "forbidden"}]}), "sk_" + "not_a_key"])
def test_a_key_whose_secret_clerk_will_not_give_means_a_new_key(browser_on, xo_server, secret):
    browser_on.user_keys = [_key("ak_old_1")]
    browser_on.secrets = {"ak_old_1": secret} if secret else {}
    status, body = call(xo_server, "POST", "/login/browser", body=LOGIN)
    assert status == 200 and body["data"] == {"key": MINTED, "days": 90, "reused": False}
    assert len(browser_on.minted) == 1
