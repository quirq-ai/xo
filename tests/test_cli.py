from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import KEY
from xo import cli
from xo.client import coder_url, xo_url


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    assert KEY not in out and KEY not in err, "the API key must never be printed"
    return code, out, err


def test_no_key_is_one_actionable_line(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    code, out, err = run(capsys, "call", "health")
    assert code == 1
    assert "run xo login, set XO_API_KEY, or pass --api-key" in err
    assert "from the XO team" in err
    assert fake.requests == []


def test_key_goes_as_a_bearer_token(fake, capsys):
    fake.route("GET", "/health", body={"status": "ok"})
    code, out, _ = run(capsys, "call", "health")
    assert code == 0
    assert json.loads(out) == {"status": "ok"}
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_flag_key_wins_over_the_variable(fake, capsys):
    fake.route("GET", "/health", body={})
    run(capsys, "--api-key", "ak_from_flag", "call", "health")
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_from_flag"


def test_an_echoed_key_is_redacted(fake, capsys):
    fake.route("GET", "/health", body=lambda req: {"you_sent": req["headers"]["Authorization"]})
    code, out, _ = run(capsys, "call", "health")
    assert code == 0 and "<redacted>" in out


def test_an_error_body_echoing_the_key_is_redacted(fake, capsys):
    fake.route("GET", "/health", status=401, body={"detail": f"bad key {KEY}"})
    code, _, err = run(capsys, "call", "health")
    assert code == 1
    assert "HTTP 401 (check the API key): bad key <redacted>" in err


def test_redirects_are_never_followed(fake, capsys):
    fake.route("GET", "/health", status=307, headers={"Location": "https://evil.example/steal"})
    code, _, err = run(capsys, "call", "health")
    assert code == 1
    assert "never follows redirects" in err
    assert len(fake.requests) == 1


def test_a_sign_in_redirect_says_the_route_wants_a_browser(fake, capsys):
    fake.route("GET", "/health", status=307, headers={"Location": "https://app.xo.builders/sign-in?x=1"})
    code, _, err = run(capsys, "call", "health")
    assert code == 1 and "does not accept an API key" in err


@pytest.mark.parametrize("url", ["http://app.xo.builders", "ftp://x", "app.xo.builders", "https://x.dev/?a=1",
                                 "https://x.dev:99999", "https://x.dev:0", "https://x.dev:abc"])
def test_plain_http_off_this_machine_is_refused(fake, capsys, url):
    code, _, err = run(capsys, "--url", url, "call", "health")
    assert code == 1 and "--url must be an https:// address" in err
    assert fake.requests == []


def test_unreachable_api(fake, capsys):
    code, _, err = run(capsys, "--url", "http://127.0.0.1:1", "call", "health")
    assert code == 1 and "could not reach" in err


def test_ops_lists_and_filters(fake, capsys):
    code, out, _ = run(capsys, "ops", "template")
    assert code == 0
    lines = out.splitlines()
    assert [line.split("\t")[0] for line in lines] == [
        "createOrgTemplate", "listTemplates", "createTemplate", "delete_api_v1_templates_template_id", "getTemplate"]
    assert lines[3].endswith("\tDelete a template")


def test_ops_needs_no_key_for_a_local_spec(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    code, out, _ = run(capsys, "ops", "--json")
    assert code == 0
    ops = {o["id"]: o for o in json.loads(out)}
    assert ops["getTemplate"]["params"][0] == {"name": "template_id", "where": "path",
                                               "required": True, "description": ""}


def test_call_fills_path_query_and_body(fake, capsys):
    fake.route("POST", "/api/v1/templates", status=201, body={"id": "t1"})
    code, out, _ = run(capsys, "call", "createTemplate", "--data", '{"name": "demo"}',
                       "--field", "public=false", "--field", "tags=[\"a\"]", "--field", "note=hi there")
    assert code == 0 and json.loads(out) == {"id": "t1"}
    assert fake.requests[0]["body"] == {"name": "demo", "public": False, "tags": ["a"], "note": "hi there"}

    fake.route("GET", "/api/v1/templates/a%2Fb", body={"id": "a/b"})
    assert run(capsys, "call", "getTemplate", "--param", "template_id=a/b")[0] == 0

    fake.route("GET", "/api/v1/templates", body=[])
    assert run(capsys, "call", "listtemplates", "--param", "limit=5")[0] == 0
    assert fake.requests[-1]["query"] == "limit=5"


def test_call_checks_its_inputs_before_sending(fake, capsys, tmp_path):
    assert "needs --param template_id=" in run(capsys, "call", "getTemplate")[2]
    assert "takes no parameter nope" in run(capsys, "call", "health", "--param", "nope=1")[2]
    assert "needs a JSON body" in run(capsys, "call", "createTemplate")[2]
    assert "takes no body" in run(capsys, "call", "health", "--field", "a=1")[2]
    assert "did you mean createTemplate" in run(capsys, "call", "createTempl")[2]
    assert "--data is not JSON" in run(capsys, "call", "createTemplate", "--data", "{nope")[2]
    assert "--field wants NAME=VALUE" in run(capsys, "call", "createTemplate", "--field", "x")[2]
    assert fake.requests == []

    body = tmp_path / "t.json"
    body.write_text('{"name": "from-file"}')
    fake.route("POST", "/api/v1/templates", body={"ok": True})
    assert run(capsys, "call", "createTemplate", "--data", f"@{body}")[0] == 0
    assert fake.requests[0]["body"] == {"name": "from-file"}


def test_api_sends_any_request(fake, capsys):
    fake.route("PATCH", "/anything", body="plain text")
    code, out, _ = run(capsys, "api", "patch", "/anything", "--query", "a=1", "--field", "x=2")
    assert code == 0 and out.strip() == "plain text"
    assert fake.requests[0]["method"] == "PATCH" and fake.requests[0]["query"] == "a=1"
    assert fake.requests[0]["body"] == {"x": 2}


def test_a_missing_bundled_spec_is_never_fetched_with_the_key(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_SPEC")
    monkeypatch.setattr("xo.spec._bundled", lambda: None)
    for argv in (["ops"], ["call", "health"], ["call", "createTemplate", "--field", "a=1", "--dry-run"]):
        code, _, err = run(capsys, *argv)
        assert code == 1 and "bundled with xo is missing" in err
    assert fake.requests == []


# --- audit round 1 -------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["call", "createTemplate", "--api-key", KEY, "--bogus"],
    ["call", "createTemplate", f"--api-key={KEY}", "--bogus"],
    ["--bogus", "--api-key", KEY],
])
def test_an_argparse_error_never_echoes_the_key(fake, capsys, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    assert e.value.code == 2
    assert KEY not in out + err


def test_global_options_work_after_the_command(fake, capsys):
    fake.route("GET", "/health", body={})
    assert run(capsys, "call", "health", "--api-key", "ak_after_command")[0] == 0
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_after_command"


def test_api_key_from_stdin(fake, capsys, monkeypatch):
    import io
    fake.route("GET", "/health", body={})
    monkeypatch.setattr("sys.stdin", io.StringIO("ak_from_stdin\n"))
    assert run(capsys, "--api-key", "-", "call", "health")[0] == 0
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_from_stdin"
    assert "all read stdin" in run(capsys, "--api-key", "-", "api", "POST", "/x", "--data", "-")[2]


@pytest.mark.parametrize("bad", [f"{KEY}\nX-Evil: 1", f"{KEY}\rX", "ak_with space", "ak_ünicode",
                                 "ak_FAKE%r4enc0123456789", 'ak_"quote\\0123456789'])
def test_a_key_with_bad_characters_is_refused_unechoed(fake, capsys, monkeypatch, bad):
    monkeypatch.setenv("XO_API_KEY", bad)
    code, out, err = run(capsys, "call", "health")
    assert code == 1 and "characters an API key cannot have" in err
    assert bad.strip() not in out + err and "Traceback" not in err
    assert fake.requests == []


def test_a_base64_key_is_sent_and_redacted_in_every_echoed_form(fake, capsys, monkeypatch):
    import urllib.parse
    key = "ak_FAKE+r4/enc=0123456789"  # the shape of Clerk's own test key, with + / and =
    monkeypatch.setenv("XO_API_KEY", key)
    echoes = [key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key), key.replace("/", "\\/"),
              urllib.parse.quote(key), "ak_FAKE%2br4%2fenc%3d0123456789", "ak_FAKE+r4%2fenc%3d0123456789",
              "ak_FAKE+r4&#x2F;enc=0123456789", "ak_FAKE+r4&#47;enc=0123456789",
              "ak_FAKE\\u002Br4\\u002fenc\\u003D0123456789", "ak_FAKE&#43;r4&#X2F;enc&#61;0123456789",
              "ak_FAKE+r4&#x002F;enc=0123456789", "ak_FAKE+r4&#047;enc=0123456789", "ak_FAKE+r4&sol;enc=0123456789",
              "ak_FAKE%252Br4%252Fenc%253D0123456789", "ak_FAKE r4/enc=0123456789", "ak_%46AKE+r4/enc=0123456789"]
    fake.route("GET", "/health", status=500, body=" ".join(echoes))
    code, out, err = run(capsys, "call", "health")
    assert code == 1 and fake.requests[0]["headers"]["Authorization"] == f"Bearer {key}"
    assert err.count("<redacted>") == len(echoes)
    assert not any(e in out + err for e in echoes) and "FAKE" not in out + err


def test_redaction_survives_truncation_and_json_escapes(fake, capsys):
    fake.route("GET", "/health", status=500, body="x" * 180 + KEY + " tail")
    assert KEY[:10] not in run(capsys, "call", "health")[2]
    escaped = "".join(f"\\u{ord(c):04x}" for c in KEY)
    fake.route("GET", "/health", body='{"echo": "' + escaped + '"}')
    code, out, _ = run(capsys, "call", "health")
    assert code == 0 and json.loads(out) == {"echo": "<redacted>"}


def test_a_broken_response_is_one_line_not_a_traceback(fake, capsys, monkeypatch):
    import http.client

    def boom(*a, **k):
        raise http.client.BadStatusLine(f"junk {KEY}\r\n")
    monkeypatch.setattr("xo.client.Client._parse", boom)
    fake.route("GET", "/health", body={})
    code, _, err = run(capsys, "call", "health")
    assert code == 1 and err.count("\n") == 1 and "\r" not in err and err.endswith("<redacted>)\n")


def test_an_unexpected_error_is_one_redacted_line(fake, capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"surprise {KEY}")
    monkeypatch.setattr("xo.client.Client.request", boom)
    code, _, err = run(capsys, "call", "health")
    assert code == 1 and err == "xo: unexpected RuntimeError: surprise <redacted>\n"


def test_mutations_are_labelled_delete_needs_yes_and_dry_run_sends_nothing(fake, capsys):
    code, out, err = run(capsys, "api", "delete", "/api/v1/templates/t1")
    assert code == 1 and "needs --yes" in err and fake.requests == []

    code, out, err = run(capsys, "call", "createTemplate", "--field", "name=demo", "--dry-run")
    assert code == 0 and fake.requests == [] and "dry run" in err
    plan = json.loads(out)
    assert plan["method"] == "POST" and plan["body"] == {"name": "demo"} and "Authorization" not in out

    fake.route("DELETE", "/api/v1/templates/t1", status=204)
    code, out, err = run(capsys, "api", "delete", "/api/v1/templates/t1", "--yes")
    assert code == 0 and err.startswith("xo: DELETE ")
    fake.route("GET", "/health", body={})
    assert run(capsys, "call", "health")[2] == ""


def test_header_parameters_are_sent_and_authorization_is_not_settable(fake, capsys):
    fake.route("POST", "/api/v1/orgs/o1/templates", body={})
    assert "needs --param X-Org-Token=" in run(capsys, "call", "createOrgTemplate", "--param", "org=o1")[2]
    assert "authorization cannot be set" in run(capsys, "call", "createOrgTemplate", "--param", "org=o1",
                                             "--param", "authorization=Bearer x")[2]
    code, _, _ = run(capsys, "call", "createOrgTemplate", "--param", "org=o1", "--param", "X-Org-Token=t")
    assert code == 0
    assert fake.requests[0]["headers"]["X-Org-Token"] == "t"
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_blank_summary_and_nan(fake, capsys):
    code, out, _ = run(capsys, "ops", "createOrg")
    assert code == 0 and out.endswith("/api/v1/orgs/{org}/templates\t\n")
    assert "not valid JSON" in run(capsys, "api", "POST", "/x", "--data", '{"a": NaN}')[2]
    fake.route("POST", "/x", body={})
    assert run(capsys, "api", "POST", "/x", "--field", "a=NaN")[0] == 0
    assert fake.requests[0]["body"] == {"a": "NaN"}


def test_ops_json_with_no_match_exits_1(fake, capsys):
    code, out, err = run(capsys, "ops", "nothing-like-this", "--json")
    assert code == 1 and json.loads(out) == [] and "no operations match" in err


def test_no_spec_says_pass_spec(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_SPEC")
    monkeypatch.setattr("xo.spec._bundled", lambda: None)
    err = run(capsys, "ops")[2]
    assert "pass --spec FILE" in err and err.count("\n") == 1 and fake.requests == []


# --- one command per endpoint --------------------------------------------------------------------

def test_endpoint_commands_are_listed_in_help(fake, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("claude-code", "templates", "orgs-templates", "health"):
        assert f"\n    {name}" in out


def test_a_single_operation_endpoint_is_one_command(fake, capsys):
    fake.route("POST", "/claude-code", body={"ok": True})
    code, out, err = run(capsys, "claude-code", "--prompt", "hello", "--project-id", "p1")
    assert code == 0 and json.loads(out) == {"ok": True}
    assert err == f"xo: POST {fake.base}/api/core/v1/claude-code\n"
    assert fake.requests[0]["body"] == {"prompt": "hello", "project_id": "p1"}
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_shared_paths_get_method_actions(fake, capsys):
    fake.route("GET", "/api/v1/templates", body=[{"id": "t1"}])
    assert run(capsys, "templates", "list", "--limit", "5")[0] == 0
    assert fake.requests[-1]["query"] == "limit=5"

    fake.route("GET", "/api/v1/templates/t1", body={"id": "t1"})
    assert run(capsys, "templates", "get", "--template-id", "t1")[0] == 0

    fake.route("POST", "/api/v1/templates", body={"id": "t2"})
    assert run(capsys, "templates", "create", "--name", "demo", "--public", "false")[0] == 0
    assert fake.requests[-1]["body"] == {"name": "demo", "public": False}

    n = len(fake.requests)
    assert "needs --yes" in run(capsys, "templates", "delete", "--template-id", "t1")[2]
    assert len(fake.requests) == n


def test_endpoint_commands_check_required_inputs(fake, capsys):
    assert "needs --name" in run(capsys, "templates", "create", "--public", "true")[2]
    assert "needs --prompt" in run(capsys, "claude-code", "--project-id", "p1")[2]
    assert run(capsys, "templates", "get")[2].endswith("needs --template-id\n")
    code, out, _ = run(capsys, "templates", "create", "--name", "x", "--dry-run")
    assert code == 0 and json.loads(out)["body"] == {"name": "x"}
    assert fake.requests == []


def test_endpoint_flags_take_the_global_options_too(fake, capsys):
    fake.route("GET", "/health", body={})
    assert run(capsys, "health", "--api-key", "ak_endpoint_flag")[0] == 0
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_endpoint_flag"


def test_without_a_local_spec_only_the_builtins_exist(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_SPEC")
    monkeypatch.setattr("xo.spec._bundled", lambda: None)
    with pytest.raises(SystemExit):
        cli.main(["claude-code"])
    assert "invalid choice" in capsys.readouterr().err
    assert fake.requests == []


# --- audit round 2 -------------------------------------------------------------------------------

def _argparse_err(capsys, *argv: str) -> str:
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    out, err = capsys.readouterr()
    assert e.value.code == 2
    return out + err


@pytest.mark.parametrize("argv", [
    ["call", "health", "--api_key", "SECRET"],
    ["call", "health", "--token=SECRET"],
    ["call", "health", "SECRET"],
    ["SECRET"],
    ["health", "--apikey", "SECRET"],
    ["templates", "list", "--key", "SECRET"],
    ["--timeout", "SECRET", "ops"],
    ["api", "SECRET", "/x"],
])
def test_no_argparse_error_echoes_what_was_typed(fake, capsys, monkeypatch, argv):
    monkeypatch.delenv("XO_API_KEY")
    secret = "ak_FAKE_typed_0123456789abcdef"
    argv = [secret if a == "SECRET" else a.replace("SECRET", secret) for a in argv]
    err = _argparse_err(capsys, *argv)
    assert secret not in err
    assert fake.requests == []


def test_a_grouped_command_takes_the_global_options_before_the_action(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    fake.route("GET", "/api/v1/templates", body=[])
    code, out, err = run(capsys, "templates", "--api-key", "ak_before_action_0123456789", "list")
    assert code == 0 and "ak_before_action" not in out + err
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_before_action_0123456789"


def test_a_typed_key_on_a_terminal_is_not_echoed(fake, capsys, monkeypatch):
    fake.route("GET", "/health", body={})
    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or "ak_typed\n")
    assert run(capsys, "--api-key", "-", "call", "health")[0] == 0
    assert asked == ["XO API key (hidden: paste it, then press Enter): "]
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer ak_typed"


def test_a_dry_run_needs_no_key_and_never_fetches_the_spec(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    code, out, _ = run(capsys, "templates", "create", "--name", "x", "--dry-run")
    assert code == 0 and json.loads(out)["body"] == {"name": "x"}
    assert run(capsys, "api", "GET", "/health", "--dry-run")[0] == 0
    monkeypatch.delenv("XO_SPEC")
    monkeypatch.setattr("xo.spec._bundled", lambda: None)
    monkeypatch.setenv("XO_API_KEY", KEY)
    code, _, err = run(capsys, "call", "createTemplate", "--field", "a=1", "--dry-run")
    assert code == 1 and "bundled with xo is missing" in err
    assert fake.requests == []


def _spec(tmp_path, paths: object) -> str:
    f = tmp_path / "spec.json"
    f.write_text(json.dumps({"openapi": "3.1.0", "paths": paths}))
    return str(f)


def test_a_percent_in_the_spec_is_not_a_format(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/rate": {"get": {
        "summary": "100%(x)s of %d", "parameters": [{"name": "q", "in": "query", "description": "50%"}]}}}))
    with pytest.raises(SystemExit):
        cli.main(["rate", "--help"])
    out = capsys.readouterr().out
    assert "100%(x)s of %d" in out and "50%" in out and "%%" not in out
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "100%(x)s of %d" in capsys.readouterr().out


@pytest.mark.parametrize("content,code", [
    ("[1, 2]", 1),
    ('{"paths": {"/x": {"get": {"parameters": ["q"]}}}}', 2),  # readable: the bad node is skipped
    ('{"paths": {"/x": {"post": {"requestBody": {"content": {"application/json": {"schema": true}}}}}}}', 2),
    ("[" * 100000, 1),
    ("\udcff", 1),
])
def test_a_bad_spec_leaves_the_builtins_working(fake, capsys, monkeypatch, tmp_path, content, code):
    f = tmp_path / "bad.json"
    f.write_bytes(content.encode("utf-8", "surrogateescape") if content == "\udcff" else content.encode())
    monkeypatch.setenv("XO_SPEC", str(f))
    fake.route("GET", "/health", body={"ok": 1})
    assert run(capsys, "api", "GET", "/health")[0] == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["claude-code", "--prompt", "x"])
    err = capsys.readouterr().err
    # A spec that cannot be read says so in one line; one that reads but lacks the command is a usage error.
    assert e.value.code == code
    if code == 1:
        assert err.startswith("xo: cannot use spec") and err.count("\n") == 1
    else:
        assert "invalid choice" in err


def test_an_unreadable_named_spec_says_so(fake, capsys, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["--spec", str(tmp_path / "missing.json"), "claude-code", "--prompt", "x"])
    assert e.value.code == 1
    assert "cannot use spec" in capsys.readouterr().err


def test_spec_flags_are_never_taken_for_global_abbreviations(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/params": {"get": {"parameters": [
        {"name": "s", "in": "query"}, {"name": "t", "in": "query"}]}}}))
    fake.route("GET", "/params", body={})
    assert run(capsys, "params", "--s", "Q5", "--t", "Q7")[0] == 0
    assert fake.requests[0]["query"] == "s=Q5&t=Q7"


def test_undeclared_placeholders_and_clashing_names_get_working_flags(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/items/{id}/sub": {"post": {
        "parameters": [{"name": "data", "in": "query", "required": True}],
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["data"], "properties": {"data": {"type": "string"}}}}}}}}}))
    err = run(capsys, "items-sub")[2]
    assert "xo: items-sub needs --data-param, --id\n" == err
    err = run(capsys, "items-sub", "--id", "i/1", "--data-param", "q")[2]
    assert "needs --data-param-param" in err
    fake.route("POST", "/items/i%2F1/sub", body={})
    assert run(capsys, "items-sub", "--id", "i/1", "--data-param", "q", "--data-param-param", "true")[0] == 0
    assert fake.requests[0]["body"] == {"data": "true"}


@pytest.mark.parametrize("name, flag", [("url", "url"), ("showSecrets", "show-secrets"), ("coderUrl", "coder-url")])
def test_a_spec_field_named_like_a_global_flag_does_not_clash(fake, capsys, monkeypatch, tmp_path, name, flag):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/links": {"post": {
        "parameters": [{"name": name, "in": "query"}],
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {name: {"type": "string"}}}}}}}}}))
    fake.route("POST", "/links", body={})
    assert run(capsys, "links", f"--{flag}-param", "q", f"--{flag}-param-param", "b")[0] == 0
    assert fake.requests[0]["query"] == f"{name}=q" and fake.requests[0]["body"] == {name: "b"}


def test_own_headers_get_no_flag_and_cannot_be_passed(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/h": {"get": {"parameters": [
        {"name": "Host", "in": "header"}, {"name": "authorization ", "in": "header"},
        {"name": "Proxy-Authorization", "in": "header"}, {"name": "X-Ok", "in": "header"}]}}}))
    for flag in ("--host", "--authorization", "--proxy-authorization"):
        assert "unrecognized arguments" in _argparse_err(capsys, "h", flag, "evil.example")
    for name in ("Host", "Cookie", "Proxy-Thing", "Transfer-Encoding"):
        assert "cannot be set" in run(capsys, "call", "get_h", "--param", f"{name}=x")[2]
    fake.route("GET", "/h", body={})
    assert run(capsys, "call", "get_h", "--param", "x-ok=1")[0] == 0
    assert fake.requests[0]["headers"]["X-Ok"] == "1"


def test_a_string_field_keeps_its_text(fake, capsys):
    fake.route("POST", "/claude-code", body={})
    assert run(capsys, "claude-code", "--prompt", "true")[0] == 0
    assert fake.requests[0]["body"] == {"prompt": "true"}


def test_help_and_version_paths_are_not_bare_commands(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/help": {"get": {}}, "/version": {"get": {}}}))
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "help-api" in out and "version-api" in out


def test_control_characters_from_the_spec_are_not_printed(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/x": {"get": {
        "operationId": "op\u001b]0;pwn\u0007", "summary": "s\u001b[31mred\u009b"}}}))
    assert "\x1b" not in run(capsys, "ops")[1]
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "\x1b" not in capsys.readouterr().out


def test_a_reader_that_leaves_early_is_not_an_error(fake, capsys, monkeypatch):
    def gone(*args, **kwargs):
        raise BrokenPipeError
    monkeypatch.setattr("os.dup2", lambda *a: None)
    monkeypatch.setattr(cli, "print", gone, raising=False)
    assert cli.main(["ops"]) == 0


# --- audit round 3 -------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["api", "GET", "/x", "--yes=SECRET"],
    ["api", "GET", "/x", "--dry-run=SECRET"],
    ["ops", "--json=SECRET"],
    ["--version=SECRET"],
    ["call", "health", "-kSECRET"],
])
def test_an_ignored_or_joined_value_is_not_echoed(fake, capsys, monkeypatch, argv):
    monkeypatch.delenv("XO_API_KEY")
    secret = "ak_FAKE_joined_0123456789abcdef"
    err = _argparse_err(capsys, *[a.replace("SECRET", secret) for a in argv])
    assert secret not in err and ("<hidden>" in err or err.endswith("arguments: -k\n"))


@pytest.mark.parametrize("argv", [
    ["call", "KEY"],
    ["call", "health", "--param", "KEY"],
    ["call", "createTemplate", "--field", "KEY"],
    ["api", "GET", "/x", "--query", "KEY"],
    ["api", "POST", "/x", "--data", "@KEY"],
    ["--spec", "KEY", "templates", "list"],
])
def test_xo_errors_never_repeat_a_key(fake, capsys, argv):
    with_key = [a.replace("KEY", KEY) for a in argv]
    if argv[0] == "--spec":
        with pytest.raises(SystemExit):
            cli.main(with_key)
        out, err = capsys.readouterr()
        assert KEY not in out + err and "<redacted>" in err
        return
    code, out, err = run(capsys, *with_key)
    # Redacted where xo names the value, or not repeated at all (R4-N3: --field K, --data @K).
    assert code == 1 and ("<redacted>" in err or "wants NAME=VALUE;" in err or "cannot read the file" in err)


def test_a_typed_key_in_a_value_slot_is_redacted_too(fake, capsys):
    other = "ak_FAKE_typed_twice_0123456789"
    code, _, err = run(capsys, "--api-key", other, "call", "health", "--param", other)
    assert code == 1 and other not in err


def test_a_query_parameter_named_like_a_header_is_fine(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/search": {"get": {"parameters": [
        {"name": "host", "in": "query", "required": True}]}}, "/v/{te}": {"get": {}}}))
    fake.route("GET", "/search", body={})
    assert run(capsys, "search", "--host", "example.com")[0] == 0
    assert fake.requests[-1]["query"] == "host=example.com"
    assert run(capsys, "call", "get_search", "--param", "host=example.org")[0] == 0
    fake.route("GET", "/v/x", body={})
    assert run(capsys, "v", "--te", "x")[0] == 0


def test_optional_string_fields_keep_their_text(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/e": {"post": {"requestBody": {"content": {
        "application/json": {"schema": {"type": "object", "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "b": {"type": ["string", "null"]},
            "c": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}}}}}}))
    code, out, _ = run(capsys, "e", "--a", "true", "--b", "5", "--c", "5", "--dry-run")
    assert code == 0 and json.loads(out)["body"] == {"a": "true", "b": "5", "c": 5}


def test_control_characters_never_reach_the_terminal(fake, capsys, monkeypatch, tmp_path):
    path = "/e\u001b[2Jx\u009b1m"
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {path: {"post": {"operationId": "e", "parameters": [
        {"name": "q\u001b[31m", "in": "query"}]}}}))
    for argv in (["ops"], ["ops", "--json"], ["call", "e", "--dry-run"], ["call", "e", "--param", "zz=1"]):
        _, out, err = run(capsys, *argv)
        assert not any(c in out + err for c in "\x1b\x9b"), argv


def test_an_unreadable_spec_is_named_in_help_too(fake, capsys, monkeypatch, tmp_path):
    (tmp_path / "bad.json").write_text("[1]")
    monkeypatch.setenv("XO_SPEC", str(tmp_path / "bad.json"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    out, err = capsys.readouterr()
    assert e.value.code == 0 and "ops" in out
    assert err.startswith("xo: cannot use spec")


def test_a_prog_placeholder_in_the_spec_is_not_formatted(fake, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/p": {"get": {"summary": "%(prog)s at 100%"}}}))
    with pytest.raises(SystemExit):
        cli.main(["p", "--help"])
    assert "%(prog)s at 100%" in capsys.readouterr().out


def test_a_spec_that_breaks_argparse_leaves_the_builtins(fake, capsys, monkeypatch):
    def boom(*a, **k):
        raise TypeError("odd spec")
    monkeypatch.setattr("xo.commands.register", boom)
    fake.route("GET", "/health", body={})
    assert run(capsys, "api", "GET", "/health")[0] == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["claude-code"])
    assert e.value.code == 1 and "TypeError: odd spec" in capsys.readouterr().err


# --- audit round 4 -------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["call", "health", "--api-keySECRET"],
    ["--api-keySECRET", "call", "health"],
    ["templates", "--api-keySECRET", "list"],
    ["call", "health", "--dry-runSECRET"],
    ["call", "health", "--yesSECRET"],
    ["call", "health", "--SECRET"],
    ["call", "health", "--api-key:SECRET"],
])
def test_a_key_glued_to_an_option_name_is_not_echoed(fake, capsys, monkeypatch, argv):
    monkeypatch.delenv("XO_API_KEY")
    secret = "ak_FAKE_r4_0123456789abcdef"
    err = _argparse_err(capsys, *[a.replace("SECRET", secret) for a in argv])
    assert secret not in err and "<hidden>" in err


def test_the_notice_before_a_change_is_redacted(fake, capsys):
    fake.route("POST", f"/x/{KEY}", body={})
    code, _, err = run(capsys, "api", "POST", f"/x/{KEY}", "--data", "{}")
    assert code == 0 and err == f"xo: POST {fake.base}/api/core/v1/x/<redacted>\n"


def test_a_typo_still_lists_the_real_commands(fake, capsys):
    err = _argparse_err(capsys, "template", "list")
    assert "'templates'" in err and "'orgs-templates'" in err
    assert "'template'" not in err


def test_a_typed_choose_from_cannot_carry_a_value_past_the_hiding(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    secret = "ak_FAKE_r5_0123456789abcdef"
    err = _argparse_err(capsys, f"x (choose from {secret}")
    assert secret not in err and "'templates'" in err


# --- the bundled core API spec -------------------------------------------------------------------

@pytest.fixture
def core(fake, monkeypatch):
    """The spec bundled with xo, against the fake API under the core API's base path."""
    monkeypatch.delenv("XO_SPEC")
    fake.strip = False
    return fake


def test_the_default_url_is_the_xo_server():
    from xo.client import CORE_PATH, DEFAULT_URL
    assert DEFAULT_URL == "https://xo.quirq.dev" and CORE_PATH == "/api/core/v1"


def test_every_command_goes_to_one_url(core, capsys, monkeypatch):
    monkeypatch.delenv("XO_URL")
    out = run(capsys, "codex", "--name", "a", "--volume-size", "20", "--dry-run")[1]
    assert json.loads(out)["url"] == "https://xo.quirq.dev/api/core/v1/codex"
    out = run(capsys, "me", "--dry-run")[1]
    assert json.loads(out)["url"] == "https://xo.quirq.dev/me"
    monkeypatch.setenv("XO_URL", "https://other.example/")
    out = run(capsys, "spaces", "--space-id", "s1", "--dry-run")[1]
    assert json.loads(out)["url"] == "https://other.example/api/core/v1/spaces/s1"
    out = run(capsys, "--url", "http://localhost:8787", "me", "--dry-run")[1]
    assert json.loads(out)["url"] == "http://localhost:8787/me"


def test_every_core_endpoint_is_a_command(core, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("claude-code", "codex", "antigravity", "hermes", "openclaw", "xo-cowork",
                 "nebius-openclaw", "spaces", "me", "user-metadata"):
        assert f"\n    {name}" in out


def test_a_create_sends_the_body_under_the_base_path(core, capsys):
    core.route("POST", "/api/core/v1/codex", status=201,
               body={"success": True, "data": {"id": "s1", "name": "my-codex", "type": "codex"}})
    code, out, err = run(capsys, "codex", "--name", "my-codex", "--volume-size", "20")
    assert code == 0 and json.loads(out)["data"]["id"] == "s1"
    assert err == (f"xo: POST {core.base}/api/core/v1/codex\n"
                   "xo: the new space gets a copy of this API key (as xo_api_key)\n")
    assert core.requests[0]["body"] == {"name": "my-codex", "volumeSize": 20}


def test_nested_flags_build_enabled_objects(core, capsys):
    code, out, _ = run(capsys, "openclaw", "--name", "c", "--volume-size", "20",
                       "--providers-anthropic-api-key", "sk-ant-x",
                       "--notification-channels-slack-bot-token", "xoxb-1",
                       "--notification-channels-slack-app-token", "xapp-1", "--dry-run", "--show-secrets")
    assert code == 0
    assert json.loads(out)["body"] == {
        "name": "c", "volumeSize": 20,
        "providers": {"anthropic": {"apiKey": "sk-ant-x", "enabled": True}},
        "notificationChannels": {"slack": {"botToken": "xoxb-1", "appToken": "xapp-1", "enabled": True}}}
    assert core.requests == []


def test_an_explicit_enabled_wins_over_the_default(core, capsys):
    code, out, _ = run(capsys, "xo-cowork", "--name", "c", "--volume-size", "20",
                       "--providers-openai-enabled", "false", "--providers-openai-api-key", "k", "--dry-run",
                       "--show-secrets")
    assert json.loads(out)["body"]["providers"] == {"openai": {"enabled": False, "apiKey": "k"}}


def test_required_objects_name_their_flags(core, capsys):
    err = run(capsys, "openclaw", "--name", "c", "--volume-size", "20")[2]
    # The key and token flags, since an object with only "enabled" is refused by the server (R6-N3).
    assert err.startswith("xo: openclaw needs one of --providers-anthropic-api-key, --providers-openai-api-key, "
                          "--providers-openrouter-api-key, one of --notification-channels-telegram-bot-token,")
    assert core.requests == []


def test_a_nested_flag_needs_its_parent_to_be_an_object(core, capsys):
    err = run(capsys, "hermes", "--name", "c", "--volume-size", "20", "--data", '{"providers": 1}',
              "--providers-openai-api-key", "k")[2]
    assert "needs providers to be a JSON object" in err


def test_hermes_has_no_whatsapp_flag(core, capsys):
    assert "unrecognized arguments" in _argparse_err(
        capsys, "hermes", "--name", "c", "--volume-size", "20", "--notification-channels-whatsapp-creds", "x")


def test_get_a_space(core, capsys):
    core.route("GET", "/api/core/v1/spaces/s1", body={"success": True, "data": {"spaceName": "c"}})
    code, out, _ = run(capsys, "spaces", "--space-id", "s1")
    assert code == 0 and json.loads(out)["data"]["spaceName"] == "c"


def test_a_422_shows_the_servers_error(core, capsys):
    core.route("POST", "/api/core/v1/codex", status=422,
               body={"success": False, "error": "name: Invalid input"})
    err = run(capsys, "codex", "--name=-x", "--volume-size", "2")[2]
    assert err.endswith("HTTP 422: name: Invalid input\n")


# --- audit round 6 -------------------------------------------------------------------------------
ANTHROPIC = "sk-ant-FAKE-r6-Anthropic-0123456789abcdef"
TELEGRAM = "123456789:AAFAKEr6TelegramToken_0123456789ab"
COWORK_SECRETS = {
    "--providers-anthropic-api-key": ANTHROPIC,
    "--providers-openai-api-key": "sk-proj-FAKE-r6-openai-0123456789",
    "--providers-openrouter-api-key": "sk-or-v1-" + "ab" * 32,
    "--notification-channels-telegram-bot-token": TELEGRAM,
    "--notification-channels-whatsapp-creds": "FAKE-r6-whatsapp-creds-0123456789",
    "--notification-channels-slack-bot-token": "xoxb-FAKE-r6-0123456789",
    "--notification-channels-slack-app-token": "xapp-FAKE-r6-0123456789",
}


def _cowork(*extra: str) -> list[str]:
    argv = ["xo-cowork", "--name", "c", "--volume-size", "20"]
    for flag, value in COWORK_SECRETS.items():
        argv += [flag, value]
    return argv + list(extra)


def test_a_dry_run_hides_provider_keys_and_bot_tokens(core, capsys):
    code, out, _ = run(capsys, *_cowork("--dry-run"))
    assert code == 0 and not any(v in out for v in COWORK_SECRETS.values())
    body = json.loads(out)["body"]
    assert body["providers"]["anthropic"] == {"apiKey": "<hidden>", "enabled": True}
    assert body["notificationChannels"]["slack"]["appToken"] == "<hidden>"
    out = run(capsys, *_cowork("--dry-run", "--show-secrets"))[1]
    assert all(v in out for v in COWORK_SECRETS.values())
    # An empty key (XO's default Anthropic key on claude-code) is not a secret to hide.
    out = run(capsys, "claude-code", "--name", "c", "--volume-size", "20", "--providers-anthropic-api-key", "",
              "--dry-run")[1]
    assert json.loads(out)["body"]["providers"]["anthropic"]["apiKey"] == ""
    assert core.requests == []


def test_secrets_from_data_and_field_are_hidden_too(core, capsys, tmp_path):
    f = tmp_path / "body.json"
    f.write_text(json.dumps({"name": "c", "volumeSize": 20, "providers": {"anthropic": {"enabled": True,
                                                                                        "apiKey": ANTHROPIC}}}))
    out = run(capsys, "hermes", "--data", f"@{f}", "--field",
              'notificationChannels={"telegram": {"enabled": true, "botToken": "%s"}}' % TELEGRAM, "--dry-run")[1]
    assert ANTHROPIC not in out and TELEGRAM not in out and out.count("<hidden>") == 2
    out = run(capsys, "call", "create_hermes", "--data", f"@{f}", "--dry-run")[1]
    assert ANTHROPIC not in out and "<hidden>" in out
    out = run(capsys, "api", "POST", "/hermes", "--data", f"@{f}", "--dry-run")[1]
    assert ANTHROPIC not in out and "<hidden>" in out


@pytest.mark.parametrize("status", [201, 401, 422, 500])
def test_a_response_that_echoes_a_secret_is_redacted(core, capsys, status):
    echo = f"rejected {ANTHROPIC}; telegram {TELEGRAM}; key {KEY}"
    body = {"success": True, "data": {"note": echo}} if status == 201 else {"success": False, "error": echo}
    core.route("POST", "/api/core/v1/openclaw", status=status, body=body)
    code, out, err = run(capsys, "openclaw", "--name", "c", "--volume-size", "20",
                         "--providers-anthropic-api-key", ANTHROPIC,
                         "--notification-channels-telegram-bot-token", TELEGRAM)
    assert ANTHROPIC not in out + err and TELEGRAM not in out + err
    assert (out if status == 201 else err).count("<redacted>") == 3
    # The same through `xo api`, which finds the operation in the spec.
    code, out, err = run(capsys, "api", "POST", "/openclaw", "--data", json.dumps(core.requests[0]["body"]))
    assert ANTHROPIC not in out + err and TELEGRAM not in out + err


def test_api_hides_the_secrets_of_the_operation_it_matches(core, capsys):
    core.route("POST", "/api/core/v1/openclaw", status=422, body={"success": False, "error": f"bad {ANTHROPIC}"})
    body = {"name": "c", "volumeSize": 20, "providers": {"anthropic": {"enabled": True, "apiKey": ANTHROPIC}}}
    code, out, err = run(capsys, "api", "POST", "/openclaw", "--data", json.dumps(body))
    assert code == 1 and ANTHROPIC not in err and "bad <redacted>" in err


def test_a_secret_in_an_argparse_error_is_hidden(core, capsys):
    err = _argparse_err(capsys, "hermes", "--name", "c", "--volume-size", "20",
                        "--notification-channels-whatsapp-creds", "FAKE-r6-whatsapp-creds-0123456789")
    assert "whatsapp-creds-0123456789" not in err and "<hidden>" in err


def test_creates_that_store_the_key_say_so(core, capsys):
    core.route("POST", "/api/core/v1/nebius-openclaw", status=201, body={"success": True, "data": {"id": "n"}})
    err = run(capsys, "nebius-openclaw", "--name", "c", "--token-factory-api-key", "tf-FAKE-0123456789")[2]
    assert "copy of this API key" not in err
    err = run(capsys, "codex", "--name", "c", "--volume-size", "20", "--dry-run")[2]
    assert "xo: the new space gets a copy of this API key (as xo_api_key)" in err
    for command in ("claude-code", "codex", "antigravity", "hermes", "openclaw", "xo-cowork"):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        assert "copy of this API key" in " ".join(capsys.readouterr().out.split())
    with pytest.raises(SystemExit):
        cli.main(["nebius-openclaw", "--help"])
    assert "copy of this API key" not in " ".join(capsys.readouterr().out.split())


@pytest.mark.parametrize("argv, hint", [
    (["--field", f"providers.anthropic.apiKey={ANTHROPIC}"], "did you mean --providers-anthropic-api-key?"),
    (["--field", 'notificationChannel={"telegram": {"enabled": true}}'], "did you mean 'notificationChannels'?"),
    (["--data", '{"volumesize": 30}'], "did you mean 'volumeSize'?"),
])
def test_an_unknown_top_level_field_is_refused(core, capsys, argv, hint):
    code, _, err = run(capsys, "hermes", "--name", "c", "--volume-size", "20", *argv)
    assert code == 1 and hint in err and ANTHROPIC not in err
    assert core.requests == []
    assert "xo api sends any body" in err


def test_a_secret_flag_reads_dash_from_stdin(core, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(ANTHROPIC + "\n"))
    out = run(capsys, "claude-code", "--name", "c", "--volume-size", "20", "--providers-anthropic-api-key", "-",
              "--dry-run", "--show-secrets")[1]
    assert json.loads(out)["body"]["providers"]["anthropic"]["apiKey"] == ANTHROPIC
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    err = run(capsys, "claude-code", "--name", "c", "--volume-size", "20", "--providers-anthropic-api-key", "-",
              "--dry-run")[2]
    assert "read nothing from stdin" in err
    err = run(capsys, "--api-key", "-", "claude-code", "--name", "c", "--volume-size", "20",
              "--providers-anthropic-api-key", "-")[2]
    assert "all read stdin" in err and core.requests == []


def test_a_create_that_times_out_may_have_happened(core, capsys):
    import threading
    import time
    done = threading.Event()
    core.route("POST", "/api/core/v1/codex", status=201,
               body=lambda req: time.sleep(1.5) or done.set() or {})
    err = run(capsys, "--timeout", "0.3", "codex", "--name", "c", "--volume-size", "20")[2]
    assert "the request may have been received, so check before you retry" in err
    assert len(core.requests) == 1
    assert done.wait(5)  # the slow handler finishes inside this test, not during a later one


def test_an_interrupted_create_may_have_happened(core, capsys, monkeypatch):
    def interrupt(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr("urllib.request.OpenerDirector.open", interrupt)
    code, _, err = run(capsys, "codex", "--name", "c", "--volume-size", "20")
    assert code == 130 and err.endswith("xo: interrupted; the request may have been received, so check before you retry\n")
    code, _, err = run(capsys, "spaces", "--space-id", "s1")
    assert code == 130 and err == "xo: interrupted\n"


def test_yes_exists_only_where_a_delete_can_be_sent(core, capsys):
    assert "unrecognized arguments: --yes" in _argparse_err(capsys, "codex", "--name", "c", "--volume-size", "20",
                                                            "--yes")
    assert "unrecognized arguments: --yes" in _argparse_err(capsys, "spaces", "--space-id", "s1", "--yes")


def test_a_flag_wins_over_the_same_field_in_data(core, capsys):  # M5
    data = {"providers": {"openai": {"enabled": True, "apiKey": "A-from-data"}, "anthropic": {"enabled": False}}}
    out = run(capsys, "xo-cowork", "--name", "c", "--volume-size", "20", "--data", json.dumps(data),
              "--providers-openai-api-key", "B-from-flag", "--dry-run", "--show-secrets")[1]
    assert json.loads(out)["body"]["providers"] == {"openai": {"enabled": True, "apiKey": "B-from-flag"},
                                                    "anthropic": {"enabled": False}}


def test_two_providers_keep_their_own_keys(core, capsys):  # M15
    out = run(capsys, "hermes", "--name", "c", "--volume-size", "20", "--providers-anthropic-api-key", "AAAA1",
              "--providers-openai-api-key", "BBBB2", "--dry-run", "--show-secrets")[1]
    assert json.loads(out)["body"]["providers"] == {"anthropic": {"apiKey": "AAAA1", "enabled": True},
                                                    "openai": {"apiKey": "BBBB2", "enabled": True}}


def test_bodies_nest_three_deep_and_a_dotted_name_is_its_own_field(fake, capsys, monkeypatch, tmp_path):
    deep = {"type": "object", "properties": {"b": {"type": "object", "properties": {"c": {
        "type": "object", "properties": {"d": {"type": "string"}, "e": {"type": "boolean", "default": True}}},
        "f": {"type": "boolean", "default": True}}}}}
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/e": {"post": {"requestBody": {"content": {
        "application/json": {"schema": {"type": "object", "properties": {
            "a": deep, "x.y": {"type": "string"},
            "x": {"type": "object", "properties": {"y": {"type": "string"}}},
            "top": {"type": "string", "default": "t"}}}}}}}}}))
    # a.b.c is the third level: one JSON flag, with its sibling's default; no top-level default.
    out = run(capsys, "e", "--a-b-c", '{"d": "1"}', "--x-y", "1", "--x-y-param", "2", "--dry-run")[1]
    assert json.loads(out)["body"] == {"a": {"b": {"c": {"d": "1"}, "f": True}}, "x.y": "1", "x": {"y": "2"}}
    assert "unrecognized" in _argparse_err(capsys, "e", "--a-b-c-d", "1")


def test_required_fields_come_from_all_of_too(fake, capsys, monkeypatch, tmp_path):  # M13
    monkeypatch.setenv("XO_SPEC", _spec(tmp_path, {"/e": {"post": {"requestBody": {"required": True, "content": {
        "application/json": {"schema": {"allOf": [
            {"type": "object", "required": ["n"], "properties": {"n": {"type": "string"}}},
            {"type": "object", "properties": {"m": {"type": "string"}}}]}}}}}}}))
    err = run(capsys, "e", "--m", "1", "--dry-run")[2]
    assert err == "xo: e needs --n\n"


def test_only_near_misses_of_a_flag_are_shown(core, capsys):  # R5-N1, R5-N3
    long_word = "--0123456789abcdef0123456789abcdef0123"
    err = _argparse_err(capsys, "codex", long_word)
    assert long_word not in err and "--<hidden>" in err
    err = _argparse_err(capsys, "openclaw", "--providers_anthropic_api_key", ANTHROPIC)
    assert "--providers_anthropic_api_key <hidden>" in err and ANTHROPIC not in err


# --- xo server routes (xo serve): me and user-metadata -------------------------------------------

@pytest.fixture
def xs(core, monkeypatch):
    """The fake API standing in for an xo server: /me and /user-metadata at its root."""
    return core


def test_me_goes_to_the_xo_server_and_prints_one_profile(xs, capsys):
    xs.route("GET", "/me", body={"success": True, "data": {
        "id": "user_1", "createdAt": 1, "privateMetadata": {"tier": "\x9b2J", "note": KEY},
        "publicMetadata": {"plan": "pro"}, "email": "sam@example.com", "imageUrl": "https://img.example/s",
        "username": "sam", "extra": True}})
    code, out, _ = run(capsys, "me")
    assert code == 0 and xs.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in out and "\x9b" not in out and "\\u009b2J" in out
    profile = json.loads(out)
    assert list(profile) == ["xo"]
    assert list(profile["xo"]) == ["username", "imageUrl", "email", "id", "createdAt", "extra",
                                   "public_data", "private_data"]
    assert profile["xo"]["public_data"] == {"plan": "pro"} and profile["xo"]["private_data"]["tier"] == "\x9b2J"


def test_me_from_an_older_server_has_no_empty_buckets(xs, capsys):
    xs.route("GET", "/me", body={"success": True, "data": {"id": "user_1", "username": "sam"}})
    code, out, _ = run(capsys, "me")
    assert code == 0 and json.loads(out) == {"xo": {"username": "sam", "id": "user_1"}}


def test_a_failed_me_is_shown_as_it_came(xs, capsys):
    xs.route("GET", "/me", status=404, body={"success": False, "error": "Account not found"})
    code, _, err = run(capsys, "me")
    assert code != 0 and "Account not found" in err


def test_projects_lists_from_the_xo_server(xs, capsys):
    xs.route("GET", "/me/projects", body={"success": True, "data": {"projects": ["a", "b"]}})
    code, out, _ = run(capsys, "projects")
    assert code == 0 and json.loads(out) == ["a", "b"]
    assert xs.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_projects_add_sends_only_the_name(xs, capsys):
    xs.route("POST", "/me/projects", body={"success": True, "data": {"projects": ["a", "new one"]}})
    code, out, _ = run(capsys, "projects", "add", "new one")
    assert code == 0 and json.loads(out) == ["a", "new one"]
    assert [(r["method"], r["path"]) for r in xs.requests] == [("POST", "/me/projects")]
    assert xs.requests[0]["body"] == {"name": "new one"}


def test_projects_from_an_older_server_says_so(xs, capsys):
    xs.route("GET", "/me/projects", status=404, body={"success": False, "error": "Not found"})
    code, _, err = run(capsys, "projects")
    assert code == 1 and "Not found" in err


def test_the_url_must_be_https(core, capsys, monkeypatch):
    monkeypatch.setenv("XO_URL", "http://xo-server.example")
    code, _, err = run(capsys, "me")
    assert code == 1 and core.requests == []
    assert err.startswith("xo: XO_URL must be an https:// address (or http:// to localhost)")
    code, _, err = run(capsys, "me", "--url", "http://10.1.1.1")
    assert code == 1 and err.startswith("xo: --url must be an https:// address")


@pytest.mark.parametrize("url", ["https://sam:hunter2@xo.example", "https://sam:hun?ter2@xo.example",
                                 "https://sam:hun/ter2@xo.example", "https://sam:hun#ter2@xo.example",
                                 "https://xo.example/?a=b@c", "//sam:hunter2@xo.example", "https://u:p@x.dev"])
def test_a_user_or_password_is_refused_and_nothing_of_the_url_is_repeated(core, capsys, url):
    code, _, err = run(capsys, "me", "--url", url)
    assert code == 1 and core.requests == []
    assert err.startswith("xo: --url must ") and err.endswith("it is not shown\n")
    assert "hunter2" not in err and "hun" not in err and "sam" not in err and "u:p" not in err


@pytest.mark.parametrize("url", ["https://[::1]@evil.example", "https://[::1", "https://[xo.example]"])
def test_a_malformed_bracketed_host_is_refused_cleanly(core, capsys, url):
    code, _, err = run(capsys, "me", "--url", url)
    assert code == 1 and core.requests == [] and "unexpected" not in err
    assert err.startswith("xo: --url is not a valid address") or err.startswith("xo: --url must ")


def test_an_at_sign_in_the_path_is_fine():
    # Coder's path-based app URLs carry @owner.
    app = "https://coder.example/@alice/ws.main/apps/xo-cowork"
    assert xo_url(app) == app and coder_url(app + "/") == app


@pytest.mark.parametrize("names", [("XO_BASE_URL",), ("XO_SERVER_URL",), ("XO_BASE_URL", "XO_SERVER_URL")])
def test_the_old_url_variables_are_refused_not_ignored(core, capsys, monkeypatch, names):
    monkeypatch.delenv("XO_URL", raising=False)
    for name in names:
        monkeypatch.setenv(name, "http://127.0.0.1:3000/api/core/v1")
    code, _, err = run(capsys, "codex", "--name", "x", "--volume-size", "20", "--dry-run")
    assert code == 1 and core.requests == []
    assert err.startswith(f"xo: {' and '.join(names)} ") and "no longer read; set XO_URL" in err
    monkeypatch.setenv("XO_URL", core.base)  # with XO_URL set, the old ones no longer matter
    assert run(capsys, "codex", "--name", "x", "--volume-size", "20", "--dry-run")[0] == 0


def test_core_commands_go_under_the_core_path(xs, capsys):
    xs.route("GET", "/api/core/v1/spaces/s1", body={"success": True, "data": {}})
    assert run(capsys, "spaces", "--space-id", "s1")[0] == 0
    assert xs.requests[0]["path"] == "/api/core/v1/spaces/s1"


def test_user_metadata_get(xs, capsys):
    xs.route("GET", "/user-metadata/user_1",
             body={"success": True, "data": {"publicMetadata": {"plan": "pro"}, "privateMetadata": {}}})
    code, out, _ = run(capsys, "user-metadata", "get", "--user-id", "user_1")
    assert code == 0 and json.loads(out)["data"]["publicMetadata"] == {"plan": "pro"}


def test_user_metadata_update_sends_both_buckets_as_json(xs, capsys):
    xs.route("PATCH", "/user-metadata/user_1", body={"success": True, "data": {}})
    code, _, err = run(capsys, "user-metadata", "update", "--user-id", "user_1",
                       "--public-metadata", '{"plan": "pro", "beta": null}',
                       "--private-metadata", '{"stripeId": "cus_1"}')
    assert code == 0
    assert err == f"xo: PATCH {xs.base}/user-metadata/user_1\n"
    assert xs.requests[0]["body"] == {"publicMetadata": {"plan": "pro", "beta": None},
                                      "privateMetadata": {"stripeId": "cus_1"}}


def test_user_metadata_update_dry_run_sends_nothing(xs, capsys):
    code, out, _ = run(capsys, "user-metadata", "update", "--user-id", "user_1",
                       "--private-metadata", '{"stripeId": "cus_1"}', "--dry-run")
    assert code == 0 and json.loads(out)["body"] == {"privateMetadata": {"stripeId": "cus_1"}}
    assert json.loads(out)["url"] == f"{xs.base}/user-metadata/user_1"
    assert xs.requests == []


@pytest.mark.parametrize("value", ["{plan: pro}", "null", "[]", '"pro"', "3"])
def test_user_metadata_update_refuses_a_bucket_that_is_not_an_object(xs, capsys, value):  # B-SF1
    code, _, err = run(capsys, "user-metadata", "update", "--user-id", "user_1", "--public-metadata", value)
    assert code != 0 and xs.requests == []
    assert err == "xo: --public-metadata must be a JSON object, like '{\"plan\": \"pro\"}'\n"


def test_user_metadata_update_refuses_a_non_object_bucket_in_data(xs, capsys):  # B-SF1
    code, _, err = run(capsys, "user-metadata", "update", "--user-id", "user_1",
                       "--data", '{"privateMetadata": null}')
    assert code != 0 and xs.requests == []
    assert "--private-metadata must be a JSON object" in err


def test_an_empty_path_parameter_is_refused(xs, capsys):  # B-N4
    code, _, err = run(capsys, "user-metadata", "get", "--user-id", "")
    assert code != 0 and xs.requests == []
    assert err == "xo: --user-id can't be empty\n"


def test_a_secret_with_a_space_is_redacted_in_its_form_encoded_form():  # #8 audit nit
    import urllib.parse
    from xo.client import redact
    secret = "bot token with spaces"
    for form in (secret, urllib.parse.quote_plus(secret), urllib.parse.quote(secret)):
        assert redact(f"x {form} y", secret) == "x <redacted> y", form


# --- xo login and xo logout ------------------------------------------------------------------------

ME = {"success": True, "data": {"id": "user_1", "username": "sam", "firstName": "Sam", "lastName": "Lee"}}
PROBE = "/api/core/v1/spaces/00000000-0000-0000-0000-000000000000"


def sent(fake) -> list[dict]:
    """What reached the fake xo server besides the keyless /config, which xo login always asks."""
    assert all("Authorization" not in r["headers"] for r in fake.requests if r["path"] == "/config")
    return [r for r in fake.requests if r["path"] != "/config"]


@pytest.fixture
def login(xs, monkeypatch):
    """xo login against the fake xo server, with no key in the environment and one typed in."""
    monkeypatch.delenv("XO_API_KEY")
    xs.route("GET", PROBE, status=404, body={"success": False, "error": "Space not found"})
    xs.route("GET", "/me", body=ME)

    def typed(text: str = KEY + "\n") -> None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
    typed()
    xs.typed = typed
    return xs


def test_login_checks_the_key_then_saves_it_private(login, capsys, config_home):
    import os
    import stat
    code, out, err = run(capsys, "login")
    assert code == 0 and out == ""
    assert [r["path"] for r in sent(login)] == [PROBE, "/me"]  # XO takes the key, then whose it is
    assert all(r["headers"]["Authorization"] == f"Bearer {KEY}" for r in sent(login))
    assert "xo: me: logged in as sam (user_1); key and URL saved to" in err
    assert f"xo: checking the key with XO through {login.base} ...\n" in err
    saved = config_home / "xo" / "credentials"
    assert f"key and URL saved to {saved}" in err
    assert json.loads(saved.read_text()) == {"url": login.base, "key": KEY}
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(saved.parent).st_mode) == 0o700
    assert sorted(p.name for p in saved.parent.iterdir()) == ["credentials", "services"]  # no temp file left
    assert stat.S_IMODE(os.stat(saved.parent / "services").st_mode) == 0o600


def test_later_commands_use_the_saved_key(login, capsys):
    assert run(capsys, "login")[0] == 0
    login.route("GET", "/me", body=ME)
    assert run(capsys, "me")[0] == 0
    assert sent(login)[-1]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_the_variable_wins_over_the_saved_key(login, capsys, monkeypatch):
    assert run(capsys, "login")[0] == 0
    monkeypatch.setenv("XO_API_KEY", "ak_from_variable")
    assert run(capsys, "me")[0] == 0
    assert sent(login)[-1]["headers"]["Authorization"] == "Bearer ak_from_variable"


def test_login_says_when_the_variable_hides_the_saved_key(login, capsys, monkeypatch):
    monkeypatch.setenv("XO_API_KEY", "ak_from_variable")
    code, _, err = run(capsys, "login")
    assert code == 0 and "XO_API_KEY is set in this terminal and is used instead" in err
    assert sent(login)[0]["headers"]["Authorization"] == f"Bearer {KEY}"  # the typed key is checked
    assert "ak_from_variable" not in err


def test_a_refused_key_is_not_saved(login, capsys, config_home):
    login.route("GET", PROBE, status=401, body={"error": "Invalid API key"})
    code, _, err = run(capsys, "login")
    assert code == 1 and [r["path"] for r in sent(login)] == [PROBE]
    assert f"xo: logging in to {login.base}; the key will only ever be sent there\n" in err
    assert f"xo: me: failed: GET {PROBE}: HTTP 401 (check the API key): Invalid API key; nothing saved\n" in err
    assert not (config_home / "xo" / "credentials").exists()


@pytest.mark.parametrize("status,body", [(404, {"error": "Not found"}), (403, {"error": "Space not found"}),
                                         (502, {"error": "XO did not answer"}), (404, None)])
def test_only_xos_space_not_found_counts_as_a_working_key(login, capsys, config_home, status, body):
    login.route("GET", PROBE, status=status, body=body)  # an xo server that does not pass it on, say
    code, _, err = run(capsys, "login")
    assert code == 1 and "nothing saved" in err
    assert not (config_home / "xo" / "credentials").exists()


def test_a_server_without_the_forwarding_is_named_at_login(login, capsys):
    login.route("GET", PROBE, status=404, body={"success": False, "error": "Not found"})
    code, _, err = run(capsys, "login")
    assert code == 1 and (f": HTTP 404: Not found; the xo server at {login.base} does not pass the "
                          "core API on (is it up to date?); nothing saved\n") in err


def test_a_refused_key_leaves_the_old_saved_key(login, capsys, config_home):
    assert run(capsys, "login")[0] == 0
    login.route("GET", PROBE, status=401, body={"error": "Invalid API key"})
    login.typed("ak_another_key_1234\n")
    assert run(capsys, "login")[0] == 1
    assert json.loads((config_home / "xo" / "credentials").read_text())["key"] == KEY


def test_an_unreachable_server_saves_nothing(login, capsys, config_home, monkeypatch):
    monkeypatch.setenv("XO_URL", "http://127.0.0.1:9")
    code, _, err = run(capsys, "login")
    assert code == 1 and "could not reach" in err and "nothing saved" in err
    assert not (config_home / "xo" / "credentials").exists()


@pytest.mark.parametrize("status,body", [(401, {"error": "Invalid API key"}), (200, {"success": True}),
                                         (200, {"success": True, "data": "x"})])
def test_a_working_key_the_server_cannot_name_is_still_saved(login, capsys, config_home, status, body):
    login.route("GET", "/me", status=status, body=body)  # a development Clerk that does not know XO's key
    code, _, err = run(capsys, "login")
    assert code == 0 and "xo: me: logged in; the key works with XO;" in err
    assert json.loads((config_home / "xo" / "credentials").read_text())["key"] == KEY


@pytest.mark.parametrize("typed", ["sk_test_clerk_secret_1234", "sk-ant-api03-provider", "pk_live_x",
                                   "AK_upper_1234"])
def test_login_refuses_what_is_not_an_xo_key_before_sending_it(login, capsys, config_home, typed):
    login.typed(typed + "\n")
    code, _, err = run(capsys, "login")
    assert code == 1 and sent(login) == [] and typed not in err
    assert ("xo: me: failed: that is not an XO API key (those start with ak_); nothing sent or saved, "
            "and it is not shown\n") in err
    assert not (config_home / "xo").exists()


@pytest.mark.parametrize("text", ["\n", "", "   \n"])
def test_login_with_nothing_typed_saves_nothing(login, capsys, config_home, text):
    login.typed(text)
    code, _, err = run(capsys, "login")
    assert code == 1 and "xo: me: failed: no key entered; nothing saved\n" in err and sent(login) == []
    assert not (config_home / "xo").exists()


def test_login_refuses_a_bad_key_without_showing_it(login, capsys, config_home):
    login.typed("ak_bad key\"<>\n")
    code, _, err = run(capsys, "login")
    assert code == 1 and sent(login) == [] and "bad" not in err
    assert "the key entered holds characters an API key cannot have" in err
    assert not (config_home / "xo").exists()


def test_login_takes_no_key_flag(login, capsys):
    code, _, err = run(capsys, "--api-key", "ak_typed_on_the_line", "login")
    assert code == 1 and sent(login) == []
    assert err == "xo: xo login asks for the key itself; leave out --api-key\n"
    with pytest.raises(SystemExit):
        cli.main(["login", "--api-key", "ak_typed_on_the_line"])
    assert "ak_typed_on_the_line" not in capsys.readouterr().err and sent(login) == []


def test_login_never_prints_the_key_even_in_the_name(login, capsys):
    login.route("GET", "/me", body={"success": True, "data": {"id": "user_1", "username": KEY}})
    code, _, err = run(capsys, "login")  # run() asserts the key is not in the output
    assert code == 0 and "logged in as <redacted> (user_1)" in err


def test_login_uses_the_name_when_there_is_no_username(login, capsys):
    login.route("GET", "/me", body={"success": True, "data": {"id": "user_1", "firstName": "Sam"}})
    assert "logged in as Sam (user_1)" in run(capsys, "login")[2]


def test_a_saved_key_others_can_read_is_refused(login, capsys, config_home):
    import os
    assert run(capsys, "login")[0] == 0
    saved = config_home / "xo" / "credentials"
    os.chmod(saved, 0o644)
    code, _, err = run(capsys, "me")
    assert code == 1 and len(sent(login)) == 2  # only login's checks went out
    assert err == (f"xo: {saved} can be read by other users, so xo will not use it; run xo logout, "
                   "ask the XO team for a new key, then xo login\n")


def test_a_symlinked_saved_key_is_refused(login, capsys, config_home, tmp_path):
    other = tmp_path / "elsewhere"
    other.write_text(KEY + "\n")
    (config_home / "xo").mkdir(parents=True)
    (config_home / "xo" / "credentials").symlink_to(other)
    code, _, err = run(capsys, "me")
    assert code == 1 and sent(login) == [] and "cannot read" in err


def test_login_keeps_a_saved_key_that_still_works(login, capsys, config_home):
    assert run(capsys, "login")[0] == 0
    login.typed("")  # nothing to read: it must not ask
    code, _, err = run(capsys, "login")
    assert code == 0 and "xo: me: already logged in as sam (user_1); key saved for" in err
    assert "logging in to" not in err


def test_login_replaces_a_saved_key_that_stopped_working(login, capsys, config_home):
    assert run(capsys, "login")[0] == 0
    login.route("GET", PROBE, body=lambda r: (404, {"error": "Space not found"})
                if r["headers"]["Authorization"] == "Bearer ak_second_key_5678" else (401, {"error": "Invalid"}))
    login.typed("ak_second_key_5678\n")
    code, _, err = run(capsys, "login")
    assert code == 0 and err.count("xo: checking ") == 2
    assert err.index(f"xo: checking the saved key with XO through {login.base} ...") < err.index(
        f"xo: checking the key with XO through {login.base} ...")
    assert (config_home / "xo" / "credentials").read_text() == json.dumps({"url": login.base, "key": "ak_second_key_5678"}) + "\n"


def test_logout_deletes_the_saved_key(login, capsys, config_home):
    assert run(capsys, "login")[0] == 0
    saved = config_home / "xo" / "credentials"
    code, _, err = run(capsys, "logout")
    assert code == 0 and err == f"xo: me: deleted {saved}\n" and not saved.exists()
    assert not (config_home / "xo" / "services").exists()
    code, _, err = run(capsys, "logout")
    assert code == 0 and err == f"xo: me: no saved key at {saved}\n"
    code, _, err = run(capsys, "me")
    assert code == 1 and "no API key: run xo login" in err


def test_logout_deletes_the_key_even_with_a_bad_coder_address(login, capsys, config_home, monkeypatch):
    assert run(capsys, "login")[0] == 0
    saved = config_home / "xo" / "credentials"
    monkeypatch.setenv("XO_CODER_URL", "http://coder.example")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/coder")
    code, _, err = run(capsys, "logout")
    assert code == 1 and not saved.exists() and not (config_home / "xo" / "services").exists()
    assert err.startswith(f"xo: me: deleted {saved}\nxo: coder: failed: XO_CODER_URL must be https://")


def test_logout_says_the_variable_is_still_set(login, capsys, monkeypatch):
    monkeypatch.setenv("XO_API_KEY", "ak_from_variable")
    assert "XO_API_KEY is still set in this terminal" in run(capsys, "logout")[2]


@pytest.mark.parametrize("directory", [False, True])
def test_logout_handles_macos_directory_error_without_misclassifying_files(config_home, capsys, monkeypatch, directory):
    import errno
    from pathlib import Path

    path = config_home / "xo" / "credentials"
    path.parent.mkdir(parents=True)
    if directory:
        path.mkdir()
    else:
        path.write_text("SYNTHETIC unreadable login file")

    def refuse(self):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(Path, "unlink", refuse)
    code, _, err = run(capsys, "logout")
    assert code == 1 and path.exists()
    if directory:
        assert err == f"xo: {path} is a folder, not a file xo login wrote; remove it yourself\n"
    else:
        assert err == f"xo: cannot delete {path} (Operation not permitted)\n"


def test_the_config_folder_defaults_to_home(monkeypatch, tmp_path):
    from xo import client
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert client.credentials_path() == tmp_path / ".config" / "xo" / "credentials"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")  # the spec says ignore a relative one
    assert client.credentials_path() == tmp_path / ".config" / "xo" / "credentials"


def test_login_tightens_an_existing_xo_folder(login, capsys, config_home):
    import os
    import stat
    (config_home / "xo").mkdir(parents=True, mode=0o755)
    os.chmod(config_home / "xo", 0o755)
    assert run(capsys, "login")[0] == 0
    assert stat.S_IMODE(os.stat(config_home / "xo").st_mode) == 0o700


def test_a_folder_that_cannot_be_made_is_one_line(login, capsys, config_home):
    config_home.mkdir(parents=True)
    (config_home / "xo").write_text("a file where the folder goes")
    code, _, err = run(capsys, "login")
    assert code == 1 and f"xo: me: failed: cannot save the key to {config_home / 'xo' / 'credentials'} (" in err


def test_a_long_name_is_cut_after_the_key_is_redacted(login, capsys):
    login.route("GET", "/me", body={"success": True, "data": {"id": "user_1", "username": "x" * 110 + KEY}})
    code, _, err = run(capsys, "login")
    assert code == 0 and KEY[:8] not in err and "logged in as " + "x" * 108 in err


def test_login_names_the_url_before_it_reads_the_key(login, capsys, monkeypatch):
    import io

    class Typed(io.StringIO):
        def readline(self, *a):
            assert "logging in to" in capsys.readouterr().err  # already on screen
            return super().readline(*a)
    monkeypatch.setattr("sys.stdin", Typed(KEY + "\n"))
    assert cli.main(["login"]) == 0


def test_the_saved_key_is_never_sent_to_another_url(login, capsys, monkeypatch):
    assert run(capsys, "login")[0] == 0
    monkeypatch.setenv("XO_URL", "http://127.0.0.1:9")
    code, _, err = run(capsys, "me")
    assert code == 1 and len(sent(login)) == 2
    assert err == (f"xo: the key xo login saved is only sent to {login.base}, not http://127.0.0.1:9; "
                   "run xo login --url http://127.0.0.1:9 to use this address\n")
    monkeypatch.delenv("XO_URL")
    code, _, err = run(capsys, "--url", "http://localhost:9", "spaces", "--space-id", "s1")
    assert code == 1 and "only sent to" in err and len(sent(login)) == 2


def test_later_commands_use_the_saved_url(login, capsys, monkeypatch):
    assert run(capsys, "login")[0] == 0
    monkeypatch.delenv("XO_URL")  # nothing set now: the saved URL, not https://xo.quirq.dev
    login.route("GET", "/api/core/v1/spaces/s1", body={"success": True, "data": {}})
    assert run(capsys, "spaces", "--space-id", "s1")[0] == 0
    assert sent(login)[-1]["path"] == "/api/core/v1/spaces/s1"
    assert run(capsys, "me")[0] == 0


def test_login_url_flag_is_saved(login, capsys, config_home, monkeypatch):
    monkeypatch.delenv("XO_URL")
    assert run(capsys, "login", "--url", login.base + "/")[0] == 0
    assert json.loads((config_home / "xo" / "credentials").read_text())["url"] == login.base


def test_a_key_from_the_variable_follows_the_url(login, capsys, monkeypatch):
    assert run(capsys, "login")[0] == 0
    monkeypatch.setenv("XO_API_KEY", "ak_from_variable")
    monkeypatch.setenv("XO_URL", "http://127.0.0.1:9")
    code, _, err = run(capsys, "me")
    assert code == 1 and "could not reach http://127.0.0.1:9" in err  # tried, with the variable's key


@pytest.mark.parametrize("text", ["ak_old_plain_key\n", "{}", '{"url": "", "key": "k"}', "[1]"])
def test_a_saved_file_xo_did_not_write_is_refused(login, capsys, config_home, text):
    import os
    (config_home / "xo").mkdir(parents=True)
    path = config_home / "xo" / "credentials"
    path.write_text(text)
    os.chmod(path, 0o600)
    code, _, err = run(capsys, "me")
    assert code == 1 and sent(login) == []
    assert err == f"xo: {path} is not a file xo login wrote; run xo logout, then xo login\n"
    assert run(capsys, "login")[0] == 0  # login replaces it
    assert json.loads(path.read_text())["key"] == KEY


def test_a_saved_file_another_user_owns_is_refused(login, capsys, monkeypatch):
    import os
    assert run(capsys, "login")[0] == 0
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    code, _, err = run(capsys, "me")
    assert code == 1 and "can be read by other users" in err


def test_control_characters_in_the_name_are_dropped(login, capsys):
    login.route("GET", "/me", body={"success": True, "data": {"id": "user_1", "username": "sam\x1b[31m\x07\r"}})
    err = run(capsys, "login")[2]
    assert "logged in as sam[31m (user_1);" in err and "\x1b" not in err and "\x07" not in err


def test_a_failed_save_leaves_no_temp_file(login, capsys, config_home, monkeypatch):
    import os

    def refuse(*a):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", refuse)
    code, _, err = run(capsys, "login")
    assert code == 1 and "cannot save the key to" in err and "No space left on device" in err
    assert list((config_home / "xo").iterdir()) == []


def test_a_fifo_or_folder_at_the_saved_path_is_refused(login, capsys, config_home):
    import os
    folder = config_home / "xo"
    folder.mkdir(parents=True)
    os.mkfifo(folder / "credentials", 0o600)
    code, _, err = run(capsys, "me")  # would hang if it waited for a writer
    assert code == 1 and err == f"xo: {folder / 'credentials'} is not a file xo login wrote; run xo logout, then xo login\n"
    os.unlink(folder / "credentials")
    (folder / "credentials").mkdir()
    code, _, err = run(capsys, "me", "--dry-run")
    assert code == 0  # a dry run needs no key
    code, _, err = run(capsys, "me")
    assert code == 1 and "is not a file xo login wrote" in err
    code, _, err = run(capsys, "logout")
    assert code == 1 and "is a folder, not a file xo login wrote; remove it yourself" in err


def test_a_strict_umask_still_leaves_a_readable_key(login, capsys, config_home):
    import os
    import stat
    old = os.umask(0o477)
    try:
        assert run(capsys, "login")[0] == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(config_home / "xo" / "credentials").st_mode) == 0o600
    assert run(capsys, "me")[0] == 0


# --- xo login across services, xo status ---------------------------------------------------------

class FakeCoder:
    """CoderClient's login and where-logged-in, without a coder binary."""

    def __init__(self, monkeypatch, at=None):
        self.at, self.logins = at, []
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/coder" if name == "coder" else None)
        fake = self

        def logged_in_to(client):
            return (fake.at, "alice") if fake.at else None

        def login(client, ask):
            fake.logins.append(client.base)
            fake.at = client.base
            return f"logged in to {client.base} as alice"
        monkeypatch.setattr(cli.CoderClient, "logged_in_to", logged_in_to)
        monkeypatch.setattr(cli.CoderClient, "login", login)


SERVED = {"success": True, "data": {"schema": "xo-services/2", "xo": {
    "url": "https://dev.xo.builders", "coder": {"url": "https://dev-coder.xo.builders"}},
    "me": {"url": "https://me.invalid"}}}


def test_one_login_covers_every_service(login, capsys, monkeypatch, config_home):
    login.route("GET", "/config", body=SERVED)
    coder = FakeCoder(monkeypatch)
    code, _, err = run(capsys, "login")
    assert code == 0
    assert f"xo:   me     {login.base} (the xo server; your XO key is only sent here)\n" in err
    assert ("xo:   xo     https://dev.xo.builders (creates and xo spaces; your key reaches it through the "
            "xo server)\n") in err
    assert "xo:   coder  https://dev-coder.xo.builders\n" in err
    assert "xo: me: logged in as sam (user_1)" in err
    assert "xo: coder: logged in to https://dev-coder.xo.builders as alice\n" in err
    assert coder.logins == ["https://dev-coder.xo.builders"]
    saved = json.loads((config_home / "xo" / "services").read_text())
    assert saved == {"me": login.base, "xo": "https://dev.xo.builders", "coder": "https://dev-coder.xo.builders"}
    assert "Authorization" not in login.requests[0]["headers"] and login.requests[0]["path"] == "/config"


def test_a_served_or_saved_me_never_changes_where_the_key_goes(login, capsys, monkeypatch, config_home):
    from xo import client, services
    monkeypatch.delenv("XO_URL")
    monkeypatch.setattr(client, "DEFAULT_URL", login.base)  # the default address, without the network
    services.save_addresses({"me": "https://saved-me.invalid"})  # no credentials file: a first login
    login.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "url": "https://dev.xo.builders", "coder": {"url": "https://dev-coder.xo.builders"}},
        "me": {"url": "https://me.invalid"}}})
    code, _, err = run(capsys, "login", "xo")
    assert code == 0 and sent(login) and all(r["headers"].get("Authorization") == f"Bearer {KEY}" for r in sent(login))
    assert ".invalid" not in err
    assert json.loads((config_home / "xo" / "credentials").read_text())["url"] == login.base
    code, out, err = run(capsys, "status")
    assert code == 0 and ".invalid" not in out + err


def test_login_one_service(login, capsys, monkeypatch):
    coder = FakeCoder(monkeypatch)
    code, _, err = run(capsys, "login", "coder")
    assert code == 0 and "xo: me:" not in err and coder.logins and sent(login) == []
    for name in ("xo", "me", "xo-server", "xo-platform"):  # the last two are the names before xo-services/2
        coder.logins.clear()
        login.typed(KEY + "\n")
        code, _, err = run(capsys, "login", name)
        assert code == 0 and "xo: me: " in err and "logged in" in err and coder.logins == [] and "coder:" not in err


def test_a_changed_coder_address_is_asked_about_once_and_no_means_no(login, capsys, monkeypatch, config_home):
    from xo import services
    services.save_addresses({"coder": "https://dev-coder.xo.builders"})
    login.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "url": "https://dev.xo.builders", "coder": {"url": "https://other-coder.example"}}}})
    coder = FakeCoder(monkeypatch)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda q: asked.append(q) or False)
    code, _, err = run(capsys, "login")
    assert code == 1 and coder.logins == [] and len(asked) == 1
    assert "the list names https://other-coder.example for coder; you last used https://dev-coder.xo.builders" in asked[0]
    assert "xo: coder: failed: kept https://dev-coder.xo.builders" in err
    assert services.saved_addresses()["coder"] == "https://dev-coder.xo.builders"
    monkeypatch.setattr(cli, "_ask", lambda q: asked.append(q) or True)
    login.typed("")
    assert run(capsys, "login")[0] == 0 and coder.logins == ["https://other-coder.example"]
    assert services.saved_addresses()["coder"] == "https://other-coder.example"
    assert run(capsys, "login")[0] == 0 and len(asked) == 2  # asked once, not on every login


def test_a_first_login_compares_with_the_shipped_list(login, capsys, monkeypatch):
    login.route("GET", "/config", body={"success": True, "data": {"schema": "xo-services/2", "xo": {
        "coder": {"url": "https://other-coder.example"}}}})
    FakeCoder(monkeypatch)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda q: asked.append(q) or False)
    assert run(capsys, "login")[0] == 1 and "you last used https://dev-coder.xo.builders" in asked[0]


def test_without_a_terminal_the_answer_is_no(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli._ask("Use it?") is False


def test_no_coder_cli_is_a_skip_not_a_failure(login, capsys, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    code, _, err = run(capsys, "login")
    assert code == 0 and "xo: coder: skipped: the Coder CLI is not installed" in err


def test_a_coder_address_from_the_flag_is_not_asked_about(login, capsys, monkeypatch):
    coder = FakeCoder(monkeypatch)
    monkeypatch.setattr(cli, "_ask", lambda q: pytest.fail("asked"))
    assert run(capsys, "--coder-url", "https://mine.example", "login", "coder")[0] == 0
    assert coder.logins == ["https://mine.example"]


def test_status_shows_each_service_and_never_a_key(login, capsys, monkeypatch):
    FakeCoder(monkeypatch, at="https://elsewhere.example")
    code, out, _ = run(capsys, "status")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split()[:2] == ["me", login.base]
    assert "not logged in; run xo login" in lines[0]
    assert lines[1].startswith("xo     https://dev.xo.builders")
    assert lines[2].startswith("coder  https://dev-coder.xo.builders")
    assert "logged in to https://elsewhere.example, not this one" in lines[2]
    assert run(capsys, "login")[0] == 0
    out = run(capsys, "status")[1]
    assert "key saved by xo login" in out and "logged in as alice" in out and KEY not in out


def test_status_is_a_reserved_command():
    from xo import commands, spec
    op = spec.operations({"paths": {"/status": {"get": {"operationId": "syntheticOperation"}}}})[0]
    assert commands.command_of(op) == "status-api"


# --- create, connect, list: projects and spaces through the xo server ---------------------------

SPACE = {"name": "dev", "kind": "claude-code", "id": "sp-1", "rev": 1, "projects": []}


def test_create_space_sends_the_kinds_body_to_the_xo_server(core, capsys):
    core.route("POST", "/me/spaces", body={"success": True, "data": {"space": SPACE, "xo": {"id": "sp-1"}}})
    secret = "sk-ant-provider-secret-never-printed"
    code, out, err = run(capsys, "create", "space", "claude-code", "dev", "--name", "my-cc", "--volume-size", "20",
                         "--providers-anthropic-api-key", secret)
    assert code == 0 and json.loads(out)["data"]["space"] == SPACE
    assert [(r["method"], r["path"]) for r in core.requests] == [("POST", "/me/spaces")]
    body = core.requests[0]["body"]
    assert (body["kind"], body["name"]) == ("claude-code", "dev")
    assert body["body"]["name"] == "my-cc" and body["body"]["providers"]["anthropic"]["apiKey"] == secret
    assert secret not in out and secret not in err and "copy of this API key" in err


def test_create_space_dry_run_hides_provider_keys(core, capsys):
    secret = "sk-ant-provider-secret-never-printed"
    code, out, err = run(capsys, "create", "space", "claude-code", "dev", "--name", "my-cc", "--volume-size", "20",
                         "--providers-anthropic-api-key", secret, "--dry-run")
    assert code == 0 and core.requests == [] and secret not in out
    assert json.loads(out)["body"]["body"]["providers"]["anthropic"]["apiKey"] == "<hidden>"


@pytest.mark.parametrize("argv", [["create", "space", "claude-code", "-x"], ["create", "space", "claude-code", "a b"],
                                  ["create", "space", "claude-code", "../x"]])
def test_create_space_refuses_a_bad_name_before_sending(core, capsys, argv):
    assert _refused(capsys, *argv, "--name", "n", "--volume-size", "1") and core.requests == []


def test_connect_project_and_space(xs, capsys):
    xs.route("POST", "/me/spaces/dev/projects", body={"success": True, "data": {"space": SPACE}})
    xs.route("POST", "/me/spaces/connect", body={"success": True, "data": {"space": SPACE}})
    assert run(capsys, "connect", "project:quirq-ai/xo", "space:dev", "--ref", "v1.2")[0] == 0
    assert run(capsys, "connect", "project:notes", "space:dev")[0] == 0
    assert run(capsys, "connect", "space:sp-1", "--name", "dev")[0] == 0
    assert [(r["path"], r["body"]) for r in xs.requests] == [
        ("/me/spaces/dev/projects", {"project": "quirq-ai/xo", "ref": "v1.2"}),
        ("/me/spaces/dev/projects", {"project": "notes"}),
        ("/me/spaces/connect", {"id": "sp-1", "name": "dev"})]


@pytest.mark.parametrize("argv", [
    ["connect", "project:notes", "space:dev", "--ref", "--upload-pack=touch x"],
    ["connect", "project:notes", "space:dev", "--ref=a..b"],
    ["connect", "project:../x", "space:dev"], ["connect", "project:notes"], ["connect", "project:notes", "dev"],
    ["connect", "space:sp-1"], ["connect", "space:../x", "--name", "dev"], ["connect", "space:sp-1", "--name", "-d"],
    ["connect", "notes", "space:dev"], ["connect", "project:notes", "space:dev", "--name", "x"]])
def test_connect_refuses_bad_input_before_sending(xs, capsys, argv):
    assert _refused(capsys, *argv) and xs.requests == []


def _refused(capsys, *argv) -> bool:
    """xo stopped with an error, its own (exit 1) or argparse's (exit 2)."""
    try:
        return run(capsys, *argv)[0] == 1
    except SystemExit as e:
        return e.code == 2


def test_list_spaces_and_projects(xs, capsys):
    xs.route("GET", "/me/spaces", body={"success": True, "data": {"spaces": [SPACE]}})
    xs.route("GET", "/me/projects", body={"success": True, "data": {"projects": ["a"]}})
    assert json.loads(run(capsys, "list", "spaces")[1]) == [SPACE]
    assert json.loads(run(capsys, "list", "projects")[1]) == ["a"]


def _git(*args, cwd=None):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A local stand-in for github.com: OWNER/REPO repositories under one folder, served over file://."""
    from xo import gitops
    root = tmp_path / "github"
    monkeypatch.setattr(gitops, "GITHUB", f"file://{root}")
    monkeypatch.setattr(gitops, "SAFE", [a.replace("protocol.allow=never", "protocol.file.allow=always")
                                          for a in gitops.SAFE])
    work = tmp_path / "here"
    work.mkdir()
    monkeypatch.chdir(work)

    def repo(name: str) -> None:
        path = root / name
        path.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=path)
        (path / "README").write_text("hi\n")
        _git("add", "README", cwd=path)
        _git("commit", "-q", "-m", "first", cwd=path)
    return repo


def test_create_project_from_a_github_url_records_it_and_clones_it(xs, capsys, github):
    github("quirq-ai/xo")
    xs.route("POST", "/me/projects", body={"success": True, "data": {"projects": ["quirq-ai/xo"]}})
    code, out, err = run(capsys, "create", "project", "https://github.com/quirq-ai/xo.git")
    assert code == 0 and json.loads(out) == ["quirq-ai/xo"]
    assert xs.requests[0]["body"] == {"name": "quirq-ai/xo"}
    assert Path("quirq-ai/xo/README").read_text() == "hi\n"


def test_create_project_with_a_plain_name_makes_an_empty_repository(xs, capsys, github):
    xs.route("POST", "/me/projects", body={"success": True, "data": {"projects": ["notes"]}})
    assert run(capsys, "create", "project", "notes")[0] == 0
    assert Path("notes/.git").is_dir()


def test_create_project_refuses_a_folder_that_exists_or_a_url_that_is_not_github(xs, capsys, github):
    Path("notes").mkdir()
    assert run(capsys, "create", "project", "notes")[0] == 1
    for url in ("https://gitlab.com/a/b", "git@github.com:a/b.git", "https://github.com/a/b/tree/x",
                "https://github.com/-a/b", "http://github.com/a/b"):
        assert run(capsys, "create", "project", url)[0] == 1
    assert xs.requests == []


def test_create_project_says_when_the_folder_could_not_be_made(xs, capsys, github):
    xs.route("POST", "/me/projects", body={"success": True, "data": {"projects": ["quirq-ai/none"]}})
    code, _, err = run(capsys, "create", "project", "quirq-ai/none")
    assert code == 1 and "quirq-ai/none is in your projects, but its folder was not made: git clone failed" in err


def test_git_never_gets_xos_or_anyone_elses_tokens(monkeypatch):
    from xo import gitops
    for name in ("XO_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "CODER_SESSION_TOKEN", "CLERK_SECRET_KEY"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("HOME", "/home/x")
    env = gitops.environment()
    assert "HOME" in env and not {"XO_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "CODER_SESSION_TOKEN",
                                  "CLERK_SECRET_KEY"} & set(env)
