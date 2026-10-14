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
    assert "cannot set Authorization" in run(capsys, "call", "createOrgTemplate", "--param", "org=o1",
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
