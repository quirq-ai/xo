from __future__ import annotations

import json

import pytest

from conftest import KEY
from xo import cli


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    assert KEY not in out and KEY not in err, "the API key must never be printed"
    return code, out, err


def test_no_key_is_one_actionable_line(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_API_KEY")
    code, out, err = run(capsys, "call", "health")
    assert code == 1
    assert "set XO_API_KEY or pass --api-key" in err
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


@pytest.mark.parametrize("url", ["http://app.xo.builders", "ftp://x", "app.xo.builders"])
def test_plain_http_off_this_machine_is_refused(fake, capsys, url):
    code, _, err = run(capsys, "--base-url", url, "call", "health")
    assert code == 1 and "must be https://" in err
    assert fake.requests == []


def test_unreachable_api(fake, capsys):
    code, _, err = run(capsys, "--base-url", "http://127.0.0.1:1", "call", "health")
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
                                 "ak_FAKE+r4/enc=0123456789", 'ak_"quote\\0123456789'])
def test_a_key_with_bad_characters_is_refused_unechoed(fake, capsys, monkeypatch, bad):
    monkeypatch.setenv("XO_API_KEY", bad)
    code, out, err = run(capsys, "call", "health")
    assert code == 1 and "characters an API key cannot have" in err
    assert bad.strip() not in out + err and "Traceback" not in err
    assert fake.requests == []


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
    assert err == f"xo: POST {fake.base}/claude-code\n"
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
    assert asked == ["XO API key: "]
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
    assert code == 0 and err == f"xo: POST {fake.base}/x/<redacted>\n"


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
    monkeypatch.setenv("XO_BASE_URL", fake.base + "/api/core/v1")
    return fake


def test_the_default_base_is_the_core_api():
    from xo.client import DEFAULT_BASE_URL
    assert DEFAULT_BASE_URL == "https://app.xo.builders/api/core/v1"


def test_every_core_endpoint_is_a_command(core, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("claude-code", "codex", "antigravity", "hermes", "openclaw", "xo-cowork",
                 "nebius-openclaw", "spaces"):
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
    import time
    core.route("POST", "/api/core/v1/codex", status=201, body=lambda req: time.sleep(1.5) or {})
    err = run(capsys, "--timeout", "0.3", "codex", "--name", "c", "--volume-size", "20")[2]
    assert "the request may have been received, so check before you retry" in err
    assert len(core.requests) == 1


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
