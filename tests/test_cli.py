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


def test_spec_falls_back_to_the_api(fake, capsys, monkeypatch):
    monkeypatch.delenv("XO_SPEC")
    monkeypatch.setattr("xo.spec._bundled", lambda: None)
    spec = json.loads((__import__("conftest").FIXTURES / "openapi.json").read_text())
    fake.route("GET", "/api/openapi", body=spec)
    code, out, _ = run(capsys, "ops", "health")
    assert code == 0 and out.startswith("health\tGET\t/health")
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"

    fake.route("GET", "/api/openapi", body="<html>")
    assert "did not return an OpenAPI document" in run(capsys, "ops")[2]


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
    assert "both read stdin" in run(capsys, "--api-key", "-", "api", "POST", "/x", "--data", "-")[2]


@pytest.mark.parametrize("bad", [f"{KEY}\nX-Evil: 1", f"{KEY}\rX", "ak_with space", "ak_ünicode"])
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
        raise http.client.BadStatusLine(f"junk {KEY}")
    monkeypatch.setattr("xo.client.Client._parse", boom)
    fake.route("GET", "/health", body={})
    code, _, err = run(capsys, "call", "health")
    assert code == 1 and err.count("\n") == 1 and "BadStatusLine" in err


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
    fake.route("GET", "/api/openapi", status=307, headers={"Location": "https://app.xo.builders/sign-in"})
    err = run(capsys, "ops")[2]
    assert "pass --spec FILE" in err and err.count("\n") == 1


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
    assert code == 1 and "a dry run never fetches the spec" in err
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


@pytest.mark.parametrize("content", ["[1, 2]", '{"paths": {"/x": {"get": {"parameters": ["q"]}}}}',
                                     '{"paths": {"/x": {"post": {"requestBody": {"content": '
                                     '{"application/json": {"schema": true}}}}}}}',
                                     "[" * 100000, "\udcff"])
def test_a_bad_spec_leaves_the_builtins_working(fake, capsys, monkeypatch, tmp_path, content):
    f = tmp_path / "bad.json"
    f.write_bytes(content.encode("utf-8", "surrogateescape") if content == "\udcff" else content.encode())
    monkeypatch.setenv("XO_SPEC", str(f))
    fake.route("GET", "/health", body={"ok": 1})
    assert run(capsys, "api", "GET", "/health")[0] == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["claude-code", "--prompt", "x"])
    err = capsys.readouterr().err
    # A spec that cannot be read says so in one line; one that reads but lacks the command is a usage error.
    assert (e.value.code == 1 and err.startswith("xo: cannot use spec") and err.count("\n") == 1) or \
        (e.value.code == 2 and "invalid choice" in err)


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
    assert "--host" in _argparse_err(capsys, "h", "--host", "evil.example")
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
