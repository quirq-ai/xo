"""The services list: what xo accepts from a server, and what it falls back to."""
from __future__ import annotations

import json

import pytest

from xo import services
from xo.client import XoError

GOOD = {"schema": "xo-services/2",
        "xo": {"url": "https://platform.example", "about": "XO",
               "coder": {"url": "https://coder.example/", "about": "Coder"}},
        "me": {"url": "https://me.example", "about": "the xo server"}}


def test_the_shipped_list_passes_its_own_checks():
    from xo.client import DEFAULT_URL
    found = services.builtin()
    assert list(found) == ["me", "xo", "coder"]
    assert found["xo"].url == "https://dev.xo.builders"
    assert found["coder"].url == "https://dev-coder.xo.builders"
    assert found["me"].url == DEFAULT_URL == "https://xo.quirq.dev"  # the xo server xo uses by default


def test_the_shipped_file_has_the_command_names():
    assert set(services.SHIPPED) == {"schema", "xo", "me"} and "coder" in services.SHIPPED["xo"]


@pytest.mark.parametrize("url", ["http://coder.example", "https://u:p@coder.example", "https://coder.example?x=1",
                                 "https://coder.example/path", "https://coder.example#f", "https://coder.example:0",
                                 "https://coder.example:abc", "https://co'der.example", "https://-x.example",
                                 "https://coder.example\n", "ftp://coder.example", 7, None, "https://" + "a" * 200,
                                 "http://127.0.0.1:3000"])
def test_a_listed_address_must_be_plain_https(url):
    with pytest.raises(XoError, match="not a plain https"):
        services.listed_url("coder", url)


def test_unknown_services_are_skipped_with_a_line():
    data = json.loads(json.dumps(GOOD))
    data["mail"] = {"url": "https://mail.example"}
    data["Bad Name\x1b"] = {"url": "https://x.example"}
    data["xo"]["spaces"] = {"url": "https://spaces.example"}
    found, notes = services.parse(data)
    assert set(found) == {"me", "xo", "coder"} and found["coder"].url == "https://coder.example"
    assert notes == ["skipped the mail service: this xo does not know it (an update may)",
                     "skipped the one service: this xo does not know it (an update may)",
                     "skipped the xo spaces service: this xo does not know it (an update may)"]


@pytest.mark.parametrize("data", [None, [], {"schema": "xo-services/1", "services": {}},
                                  {"schema": "xo-services/2", "xo": []},
                                  {"schema": "xo-services/2", "xo": {"coder": "https://c.example"}},
                                  {"schema": "xo-services/2", "me": "https://me.example"}])
def test_a_list_of_another_shape_is_refused(data):
    with pytest.raises(XoError):
        services.parse(data)


def test_fetch_takes_a_good_list_without_sending_a_key(fake):
    fake.route("GET", "/config", body={"success": True, "data": GOOD})
    found, notes = services.fetch(fake.base, 5)
    assert found["coder"].url == "https://coder.example" and notes == []
    assert "Authorization" not in fake.requests[0]["headers"]


@pytest.mark.parametrize("status, body, why", [
    (404, {"error": "Not found"}, "it answered HTTP 404"),
    (302, None, "it answered HTTP 302"),
    (200, "not json", "its answer is not JSON"),
    (200, {"success": True, "data": {"schema": "other"}}, "it is not an xo-services/2 list"),
    (200, {"success": True, "data": {"schema": "xo-services/1", "services": {}}}, "it is not an xo-services/2 list"),
    (200, {"success": True, "data": {**GOOD, "xo": {"coder": {"url": "http://evil.example"}}}},
     "the address for coder in the list is not a plain https:// address"),
    (200, {"success": True, "data": {**GOOD, "xo": {"url": "http://platform.example",
                                                    "coder": {"url": "https://coder.example"}}}},
     "the address for xo in the list is not a plain https:// address")])
def test_fetch_falls_back_to_the_shipped_list_and_says_why(fake, status, body, why):
    fake.route("GET", "/config", status=status, body=body, headers={"Location": "https://evil.example/config"})
    found, notes = services.fetch(fake.base, 5)
    assert found == services.builtin()
    assert notes == [f"using xo's own list of services: the server's list could not be used ({why})"]
    assert len(fake.requests) == 1  # the redirect was not followed


def test_fetch_caps_the_answer(fake):
    fake.route("GET", "/config", body={"success": True, "data": GOOD, "pad": "x" * services.MAX_LIST})
    assert "over 65536 bytes" in services.fetch(fake.base, 5)[1][0]


def test_fetch_fills_a_missing_service_from_the_shipped_list(fake):
    fake.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "url": "https://platform.example"}}})
    found, notes = services.fetch(fake.base, 5)
    assert found["coder"] == services.builtin()["coder"] and found["me"] == services.builtin()["me"]
    assert found["xo"].url == "https://platform.example"
    assert notes == ["the server's list has no me; using xo's own address for it",
                     "the server's list has no coder; using xo's own address for it"]


def test_a_list_without_an_address_for_xo_keeps_coder_and_says_so(fake):
    fake.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "url": None, "coder": {"url": "https://coder.example"}}, "me": {"url": "https://me.example"}}})
    found, notes = services.fetch(fake.base, 5)
    assert found["coder"].url == "https://coder.example" and found["xo"] == services.builtin()["xo"]
    assert notes == ["the server's list gives no address for xo; showing xo's own, which may not be where it "
                     "sends creates"]


def test_a_bad_me_address_is_skipped_and_the_rest_of_the_list_kept(fake):
    # xo never uses a listed me, so it must not cost the server's xo and coder addresses.
    fake.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "url": "https://platform.example", "coder": {"url": "https://coder.example"}},
        "me": {"url": "http://127.0.0.1:8787"}}})
    found, notes = services.fetch(fake.base, 5)
    assert found["xo"].url == "https://platform.example" and found["coder"].url == "https://coder.example"
    assert found["me"] == services.builtin()["me"]
    assert notes == ["the address for me in the list is not a plain https:// address; xo does not use it, "
                     "so it is skipped"]


def test_tests_reach_loopback_only(monkeypatch):
    import socket
    import urllib.request
    monkeypatch.undo()  # a test's own undo must not remove the guard
    with pytest.raises(socket.gaierror, match="tests reach loopback only"):
        socket.getaddrinfo("canary.invalid", 443)
    assert urllib.request.getproxies() == {}


def test_unreachable_server_uses_the_shipped_list():
    found, notes = services.fetch("http://127.0.0.1:9", 2)
    assert found == services.builtin() and "it could not be reached" in notes[0]


def test_saved_addresses_round_trip_and_ignore_junk(config_home):
    import os
    import stat
    assert services.saved_addresses() == {}
    services.save_addresses({"coder": "https://coder.example", "me": "https://xo.example"})
    assert services.saved_addresses() == {"coder": "https://coder.example", "me": "https://xo.example"}
    # A file from before xo-services/2 is read under the new names.
    services.save_addresses({"xo-server": "http://127.0.0.1:8080", "xo-platform": "https://p.example"})
    assert services.saved_addresses() == {"me": "http://127.0.0.1:8080", "xo": "https://p.example"}
    # A file holding both names: the new one wins.
    services.save_addresses({"xo-platform": "https://old.example", "xo": "https://new.example"})
    assert services.saved_addresses()["xo"] == "https://new.example"
    assert stat.S_IMODE(os.stat(services.addresses_path()).st_mode) == 0o600
    services.addresses_path().write_text('{"coder": "http://evil.example", "xo": 3}')
    assert services.saved_addresses() == {}
    services.addresses_path().write_text("not json")
    assert services.saved_addresses() == {}
    assert services.coder_default() == "https://dev-coder.xo.builders"


def test_saved_addresses_are_read_like_the_credentials_file(config_home, tmp_path):
    import os
    import stat
    services.save_addresses({"coder": "https://coder.example"})
    path = services.addresses_path()
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    os.chmod(path, 0o644)  # others could have changed it
    assert services.saved_addresses() == {}
    os.chmod(path, 0o600)
    assert services.saved_addresses() == {"coder": "https://coder.example"}
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text('{"coder": "https://coder.example"}')
    os.chmod(elsewhere, 0o600)
    path.unlink()
    path.symlink_to(elsewhere)
    assert services.saved_addresses() == {}
    path.unlink()
    os.mkfifo(path)  # must not hang
    assert services.saved_addresses() == {}
    assert services.coder_default() == "https://dev-coder.xo.builders"
