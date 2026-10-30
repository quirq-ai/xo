"""xo serve with the hub on (XO_HUB_URL and XO_HUB_KEY): what xo keeps for a user lives in the hub's
record, not in Clerk's private metadata. SYNTHETIC: the hub here is a stand-in shaped like
space-hub's /-/api/xo/v1/ (cli/src/xo.ts there)."""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xo import server
from xo.client import XoError
from tests.test_server import BOB_KEY, USER_KEY, _Core, _Users, _as, clerk, no_floor  # noqa: F401 - fixtures

HUB_KEY = "hub_key_never_printed_" + "x" * 20
TOKEN = "hub_token_never_printed"


class FakeHub:
    """The hub's records and agents, compare-and-swap like the real one. `race(subject)` runs just
    before a write lands, to play another writer."""

    def __init__(self, race=None, down: bool = False) -> None:
        self.records: dict[str, tuple[int, dict]] = {}
        self.agents: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.race, self.down = race, down

    def _check(self, what, *args):
        self.calls.append((what, *args))
        if self.down:
            raise server.HubError(502, "unreachable")

    def record(self, subject):
        self._check("record", subject)
        version, doc = self.records.get(subject, (0, None))
        return version, json.loads(json.dumps(doc))

    def put(self, subject, version, doc):
        self._check("put", subject, version)
        if self.race:
            self.race, race = None, self.race
            race(self, subject)
        if self.records.get(subject, (0, None))[0] != version:
            raise server.HubConflict()
        self.records[subject] = (version + 1, json.loads(json.dumps(doc)))
        return version + 1

    def agent(self, subject, username):
        self._check("agent", subject, username)
        if subject not in self.agents:
            if username == "admin":
                raise server.HubConflict('agent name "admin" is reserved')
            if any(a["agent"] == username for a in self.agents.values()):
                raise server.HubConflict()
            self.agents[subject] = {"agent": username, "token": TOKEN}
        return dict(self.agents[subject])


@pytest.fixture
def hub_env(clerk, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XO_HUB_URL", "https://hub.quirq.dev")
    monkeypatch.setenv("XO_HUB_KEY", HUB_KEY)
    monkeypatch.setattr(server, "SHARE_FLOOR", 0)


def _app(users, hub, lines=None):
    return server.App(server.Config(), clerk=users, core=_Core(), hub=hub,
                      audit=(lines.append if lines is not None else lambda line: None))


def _alice(**private):
    return _Users({"user_alice": ("alice", {"stripeId": "cus_1", "projects": ["o/a"], **private}),
                   "user_bob": ("bob", {"stripeId": "cus_9", "projects": ["bobs"]})})


def test_the_hub_is_off_unless_both_variables_are_set(clerk, monkeypatch):  # noqa: F811
    assert server.App(server.Config(), clerk=_alice()).hub is None
    monkeypatch.setenv("XO_HUB_URL", "https://hub.quirq.dev")
    with pytest.raises(XoError, match="both"):
        server.Config()
    monkeypatch.setenv("XO_HUB_KEY", HUB_KEY)
    monkeypatch.setenv("XO_HUB_URL", "http://hub.example.com")
    with pytest.raises(XoError, match="https"):
        server.Config()


def test_a_first_read_copies_xos_keys_out_of_clerk_once_and_leaves_clerk_as_it_was(hub_env):
    users, hub = _alice(spaces=[{"name": "dev", "rev": 1, "projects": []}]), FakeHub()
    app = _app(users, hub)
    assert _as(app, USER_KEY, "/me/projects", method="GET") == (200, {"success": True, "data": {"projects": ["o/a"]}})
    assert hub.records["user_alice"] == (1, {"projects": ["o/a"], "spaces": [{"name": "dev", "rev": 1, "projects": []}]})
    assert _as(app, USER_KEY, "/me/spaces", method="GET")[0] == 200
    assert [c[0] for c in hub.calls] == ["record", "put", "record"]  # copied once
    assert users.patches == []
    assert users.users["user_alice"]["private_metadata"]["projects"] == ["o/a"]


def test_writes_go_to_the_hub_only(hub_env):
    users, hub, lines = _alice(), FakeHub(), []
    status, out = _as(_app(users, hub, lines), USER_KEY, "/me/projects", {"name": "notes"})
    assert status == 200 and out["data"]["projects"] == ["o/a", "notes"]
    assert hub.records["user_alice"][1]["projects"] == ["o/a", "notes"]
    assert users.patches == []
    assert json.loads(lines[-1])["outcome"] == 200


def test_a_racing_write_is_never_lost(hub_env):
    def other_writer(hub, subject):  # another instance adds a project between our read and our write
        version, doc = hub.records[subject]
        hub.records[subject] = (version + 1, {**doc, "projects": doc["projects"] + ["theirs"]})

    users, hub = _alice(), FakeHub()
    app = _app(users, hub)
    _as(app, USER_KEY, "/me/projects", method="GET")  # copied: version 1
    hub.race = other_writer
    status, out = _as(app, USER_KEY, "/me/projects", {"name": "mine"})
    assert status == 200
    assert hub.records["user_alice"][1]["projects"] == ["o/a", "theirs", "mine"]


def test_a_share_copies_the_recipient_first_and_writes_only_their_shared(hub_env):
    users, hub = _alice(), FakeHub()
    app = _app(users, hub)
    status, _ = _as(app, USER_KEY, "/me/share", {"item": "project:o/a", "to": "bob"})
    assert status == 200
    version, bob = hub.records["user_bob"]
    assert bob["projects"] == ["bobs"] and len(bob["shared"]) == 1 and "stripeId" not in bob
    assert users.patches == []
    status, out = _as(app, BOB_KEY, "/me/shared", method="GET")
    assert status == 200 and [e["item"] for e in out["data"]["shared"]] == ["project:o/a"]


def test_me_shows_xos_keys_from_the_hub_and_the_rest_from_clerk(hub_env):
    users, hub = _alice(), FakeHub()
    hub.records["user_alice"] = (4, {"projects": ["from-hub"]})
    status, out = _as(_app(users, hub), USER_KEY, "/me", method="GET")
    assert status == 200
    assert out["data"]["privateMetadata"] == {"stripeId": "cus_1", "projects": ["from-hub"]}


def test_the_hub_down_is_a_502_and_says_so_in_the_audit(hub_env):
    users, lines = _alice(), []
    status, out = _as(_app(users, FakeHub(down=True), lines), USER_KEY, "/me/projects", {"name": "notes"})
    assert status == 502 and out["error"] == "The hub did not answer"
    assert json.loads(lines[-1])["hub"] == "unreachable"
    assert users.patches == []


def test_me_hub_gives_the_users_own_agent_under_their_username(hub_env):
    users, hub, lines = _Users({"user_alice": ("Alice", {})}), FakeHub(), []
    status, out = _as(_app(users, hub, lines), USER_KEY, "/me/hub")
    assert (status, out["data"]) == (200, {"server": "https://hub.quirq.dev", "agent": "alice", "token": TOKEN})
    assert hub.calls[-1] == ("agent", "user_alice", "alice")
    assert all(TOKEN not in line for line in lines)


def test_me_hub_refuses_a_username_that_cannot_name_an_agent_and_a_taken_name(hub_env):
    hub = FakeHub()
    assert _as(_app(_Users({"user_alice": ("al_ice", {})}), hub), USER_KEY, "/me/hub")[0] == 409
    hub.agents["user_other"] = {"agent": "alice", "token": "t"}
    status, out = _as(_app(_Users({"user_alice": ("alice", {})}), hub), USER_KEY, "/me/hub")
    assert status == 409 and "belongs to another account" in out["error"]
    status, out = _as(_app(_Users({"user_alice": ("admin", {})}), hub), USER_KEY, "/me/hub")
    assert status == 409 and "keeps the name admin for itself" in out["error"]


def test_the_hub_client_passes_on_the_reason_of_a_409(hub_http):
    hub, _, answers = hub_http
    answers.append(_json(409, {"error": 'agent name "admin" is\nreserved'}))
    with pytest.raises(server.HubConflict) as e:
        hub.agent("user_alice", "admin")
    assert e.value.reason == 'agent name "admin" is reserved'
    answers.append((409, b"not json", {}))
    with pytest.raises(server.HubConflict) as e:
        hub.agent("user_alice", "admin")
    assert e.value.reason == ""


def test_me_with_the_hub_down_is_a_502_and_audited(hub_env):
    lines = []
    status, out = _as(_app(_alice(), FakeHub(down=True), lines), USER_KEY, "/me", method="GET")
    assert status == 502 and out["error"] == "The hub did not answer"
    line = json.loads(lines[-1])
    assert (line["route"], line["method"], line["outcome"], line["hub"]) == ("/me", "GET", 502, "unreachable")


def test_a_write_racing_a_slow_hub_stops_retrying_once_it_has_used_the_timeout(hub_env, monkeypatch):
    clock = iter(range(0, 1000, 6))  # every reading of the clock is 6 seconds on
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))

    class AlwaysRaced(FakeHub):
        def put(self, subject, version, doc):
            self.race = lambda hub, s: hub.records.__setitem__(s, (hub.records[s][0] + 1, hub.records[s][1]))
            return super().put(subject, version, doc)

    users, hub = _alice(), AlwaysRaced()
    hub.records["user_alice"] = (1, {"projects": ["o/a"]})
    status, _ = _as(_app(users, hub), USER_KEY, "/me/projects", {"name": "mine"})
    assert status == 503
    assert [c[0] for c in hub.calls].count("put") == 2  # not PROJECT_TRIES + 1


def test_admin_metadata_shows_xos_keys_from_the_hub(hub_env):
    from tests.test_server import ADMIN_KEY
    users, hub = _Users({"user_alice": ("alice", {"stripeId": "cus_1", "projects": ["old"]})}), FakeHub()
    hub.records["user_alice"] = (3, {"projects": ["from-hub"], "spaces": []})
    status, out = _as(_app(users, hub), ADMIN_KEY, "/user-metadata/user_alice", method="GET")
    assert status == 200
    assert out["data"]["privateMetadata"] == {"stripeId": "cus_1", "projects": ["from-hub"], "spaces": []}


def test_admin_metadata_refuses_to_edit_xos_keys_in_clerk_with_the_hub_on(hub_env, monkeypatch):
    from tests.test_server import ADMIN_KEY
    monkeypatch.setenv("XO_EDITABLE_PRIVATE_KEYS", "stripeId,projects")
    users, hub, lines = _alice(), FakeHub(), []
    status, out = _as(_app(users, hub, lines), ADMIN_KEY, "/user-metadata/user_alice",
                      {"privateMetadata": {"projects": ["x"]}}, method="PATCH")
    assert status == 422 and "kept in the hub" in out["error"]
    assert users.patches == [] and hub.calls == []
    assert json.loads(lines[-1])["keys"] == ["projects"]
    status, out = _as(_app(users, hub), ADMIN_KEY, "/user-metadata/user_alice",
                      {"privateMetadata": {"stripeId": "cus_2"}}, method="PATCH")
    assert status == 200 and out["data"]["privateMetadata"]["stripeId"] == "cus_2"


def test_admin_metadata_with_the_hub_down_leaves_clerk_unchanged(hub_env):
    from tests.test_server import ADMIN_KEY
    users, lines = _alice(), []
    status, _ = _as(_app(users, FakeHub(down=True), lines), ADMIN_KEY, "/user-metadata/user_alice",
                    {"privateMetadata": {"stripeId": "cus_2"}}, method="PATCH")
    assert status == 502 and users.patches == []
    assert json.loads(lines[-1])["route"] == "/user-metadata"


def test_me_hub_is_a_404_with_the_hub_off(clerk):  # noqa: F811
    status, _ = _as(server.App(server.Config(), clerk=_alice(), core=_Core(), audit=lambda line: None), USER_KEY, "/me/hub")
    assert status == 404


# ---------------------------------------------------------------- the Hub client itself


@pytest.fixture
def hub_http(monkeypatch):
    """A stand-in hub over HTTP: every request it got, and what it answers next."""
    seen: list[dict] = []
    answers: list[tuple[int, bytes, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                         "body": json.loads(raw) if raw else None})
            status, body, extra = answers.pop(0)
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = do_POST = _serve

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("XO_HUB_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("XO_HUB_KEY", HUB_KEY)
    yield server.Hub(server.Config()), seen, answers
    httpd.shutdown()


def _json(status, body, **extra):
    return status, json.dumps(body).encode(), extra


def test_the_hub_client_sends_the_key_and_reads_records(hub_http):
    hub, seen, answers = hub_http
    answers += [_json(200, {"version": 0, "doc": None}), _json(200, {"version": 1}), _json(409, {"error": "x"})]
    assert hub.record("user_alice") == (0, None)
    assert hub.put("user_alice", 0, {"projects": []}) == 1
    with pytest.raises(server.HubConflict):
        hub.put("user_alice", 0, {})
    assert [(s["method"], s["path"]) for s in seen] == [("GET", "/-/api/xo/v1/records/user_alice"),
                                                         ("PUT", "/-/api/xo/v1/records/user_alice"),
                                                         ("PUT", "/-/api/xo/v1/records/user_alice")]
    assert all(s["auth"] == f"Bearer {HUB_KEY}" for s in seen)
    assert seen[1]["body"] == {"version": 0, "doc": {"projects": []}}


@pytest.mark.parametrize("answer", [_json(200, {"version": 2, "doc": None}), _json(200, {"version": 0, "doc": {}}),
                                    (200, b"not json", {}), _json(200, [1]), _json(500, {})])
def test_the_hub_client_refuses_odd_answers(hub_http, answer):
    hub, _, answers = hub_http
    answers.append(answer)
    with pytest.raises(server.HubError):
        hub.record("user_alice")


def test_the_hub_client_never_follows_a_redirect(hub_http):
    hub, seen, answers = hub_http
    answers.append((307, b"", {"Location": "https://evil.example/steal"}))
    with pytest.raises(server.HubError):
        hub.record("user_alice")
    assert len(seen) == 1


def test_the_hub_client_quotes_the_user_id(hub_http):
    hub, seen, answers = hub_http
    answers.append(_json(200, {"version": 0, "doc": None}))
    hub.record("user_a/../b")
    assert seen[0]["path"] == "/-/api/xo/v1/records/" + urllib.parse.quote("user_a/../b", safe="")


def test_admin_metadata_of_an_unknown_user_reads_as_with_the_hub_off(hub_env):
    from tests.test_server import ADMIN_KEY
    status, out = _as(_app(_alice(), FakeHub()), ADMIN_KEY, "/user-metadata/user_nobody", method="GET")
    assert (status, out["error"]) == (404, "User not found")


# ---------------------------------------------------------------- signing in to the hub's website

HUB_REDIRECT = "https://hub.quirq.dev/-/login/xo/callback"


@pytest.fixture
def hub_login(hub_env, clerk, monkeypatch):  # noqa: F811
    """The hub and browser login on, and the hub's callback registered with the fake Clerk."""
    from tests.test_server import CLIENT_ID
    monkeypatch.setenv("XO_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("XO_CLERK_FRONTEND_URL", os.environ["CLERK_API_URL"] + "/")
    monkeypatch.setenv("XO_HUB_LOGIN_REDIRECT", HUB_REDIRECT)
    clerk.redirect_uris.add(HUB_REDIRECT)
    return clerk


def _hub_signin(app, body=None, key=HUB_KEY):
    from tests.test_server import VERIFIER
    sent = {"code": "code_1", "verifier": VERIFIER, "redirect": HUB_REDIRECT} if body is None else body
    return app.handle("POST", "/login/hub", {"authorization": f"Bearer {key}"} if key else {}, json.dumps(sent).encode())


def test_hub_sign_in_is_off_until_its_callback_is_set(hub_env, clerk, monkeypatch, capfd):  # noqa: F811
    from tests.test_server import CLIENT_ID
    monkeypatch.setenv("XO_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("XO_CLERK_FRONTEND_URL", os.environ["CLERK_API_URL"] + "/")
    app = _app(_alice(), FakeHub())
    assert "hub" not in app.handle("GET", "/login", {}, None)[1]["data"]
    assert _hub_signin(app)[0] == 404 and clerk.fapi == []
    # Only the hub's own callback turns it on; anything else turns it off and says why.
    for wrong in ("https://evil.example/-/login/xo/callback", "https://hub.quirq.dev/callback"):
        monkeypatch.setenv("XO_HUB_LOGIN_REDIRECT", wrong)
        assert server.Config().hub_login is None
        assert "hub sign-in is off: XO_HUB_LOGIN_REDIRECT must be https://hub.quirq.dev/-/login/xo/callback" \
            in capfd.readouterr().err
    monkeypatch.setenv("XO_HUB_LOGIN_REDIRECT", HUB_REDIRECT)
    monkeypatch.delenv("XO_OAUTH_CLIENT_ID")
    assert server.Config().hub_login is None and "browser login is off" in capfd.readouterr().err


def test_hub_sign_in_is_off_with_the_hub_off(clerk, monkeypatch, capfd):  # noqa: F811
    monkeypatch.setenv("XO_HUB_LOGIN_REDIRECT", HUB_REDIRECT)
    assert server.Config().hub_login is None and "the hub is off" in capfd.readouterr().err


def test_get_login_offers_the_hubs_callback(hub_login):
    status, out = _app(_alice(), FakeHub()).handle("GET", "/login", {}, None)
    assert status == 200 and out["data"]["hub"] == {"redirect": HUB_REDIRECT}
    assert out["data"]["browser"]["redirects"] == list(server.LOGIN_REDIRECTS)  # the CLI's are unchanged
    assert hub_login.fapi == []


def test_hub_sign_in_answers_the_users_agent_and_makes_no_key(hub_login):
    users, hub, lines = _Users({"user_alice": ("Alice", {})}), FakeHub(), []
    app = _app(users, hub, lines)
    assert _hub_signin(app) == (200, {"success": True, "data": {"subject": "user_alice", "agent": "alice"}})
    assert hub.calls[-1] == ("agent", "user_alice", "alice")
    token, _ = hub_login.fapi
    assert token["form"]["redirect_uri"] == HUB_REDIRECT and "Authorization" not in token["headers"]
    assert hub_login.minted == [] and not any(c["path"].startswith("/v1/api_keys") for c in hub_login.calls)
    assert json.loads(lines[-1]) | {"time": None} == {"time": None, "method": "POST", "route": "/login/hub",
                                                    "keyId": None, "caller": None, "target": "user_alice",
                                                    "outcome": 200}
    assert all(TOKEN not in line and HUB_KEY not in line for line in lines)
    # The code works once.
    status, out = _hub_signin(app)
    assert status == 401 and out["error"].endswith("sign in again")


def test_a_returning_user_gets_the_same_agent(hub_login):
    hub = FakeHub()
    hub.agents["user_alice"] = {"agent": "alice", "token": TOKEN}
    assert _hub_signin(_app(_Users({"user_alice": ("alice", {})}), hub))[1]["data"]["agent"] == "alice"
    assert len(hub.agents) == 1


@pytest.mark.parametrize("key", [None, "wrong", HUB_KEY + "x"])
def test_hub_sign_in_needs_the_hubs_key(hub_login, key):
    lines = []
    status, out = _hub_signin(_app(_alice(), FakeHub(), lines), key=key)
    assert status == 401 and out["error"] == "Missing or wrong hub key"
    assert hub_login.fapi == [] and json.loads(lines[-1])["outcome"] == 401


@pytest.mark.parametrize("change, status", [
    ({"redirect": "http://127.0.0.1:8976/callback"}, 400), ({"redirect": "https://evil.example/-/login/xo/callback"}, 400),
    ({"code": "a b"}, 400), ({"verifier": "v" * 42}, 400), ({"extra": 1}, 422), ({"redirect": None}, 400)])
def test_hub_sign_in_refuses_bad_bodies_before_calling_clerk(hub_login, change, status):
    from tests.test_server import VERIFIER
    body = {"code": "code_1", "verifier": VERIFIER, "redirect": HUB_REDIRECT} | change
    assert _hub_signin(_app(_alice(), FakeHub()), body)[0] == status
    assert _hub_signin(_app(_alice(), FakeHub()), {"code": "code_1"})[0] == 422
    assert hub_login.fapi == []


def test_hub_sign_in_refuses_a_username_that_cannot_name_an_agent(hub_login):
    hub = FakeHub()
    status, out = _hub_signin(_app(_Users({"user_alice": ("al_ice", {})}), hub))
    assert status == 409 and "change it first" in out["error"] and hub.agents == {}


def test_hub_sign_in_with_the_hub_down_is_a_502(hub_login):
    status, out = _hub_signin(_app(_Users({"user_alice": ("alice", {})}), FakeHub(down=True)))
    assert status == 502 and out["error"] == "The hub did not answer"
