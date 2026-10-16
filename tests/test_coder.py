"""Coder is a fake executable/runner here; tests never reach a Coder deployment."""
from __future__ import annotations

import csv
import io
import json
import subprocess
import sys

import pytest

from xo import cli
from xo.client import DEFAULT_CODER_URL, CoderClient, coder_url

REPO = "https://github.com/example/my-repo"
APP = "https://generated-preview.example/"
TOKEN = "coder_test_session_never_printed"


def record(calls, argv, options):
    # A failed assertion may print fixture locals: never retain the developer's environment.
    env = options.get("env", {})
    calls.append((argv, {**options, "env": {"CODER": env["CODER"]} if "CODER" in env else {}}))


def cmd(argv):
    """The coder subcommand: whoami runs without --url, everything else with it."""
    return argv[3] if argv[1] == "--url" else argv[1]


def whoami(runner, argv):
    """Coder's answer to `coder whoami --output json`: logged in where xo expects, unless told."""
    assert argv[1:] == ["whoami", "--output", "json"], "whoami must not name a deployment"
    at = runner.at if runner.at is not None else runner.base
    return subprocess.CompletedProcess(argv, 0, json.dumps([{"username": "alice", "url": at}]), "")


class Calls(list):
    at = None  # where the fake Coder is logged in; None: wherever xo is about to use
    base = None


@pytest.fixture
def runner(monkeypatch):
    calls = Calls()
    real_init = CoderClient.__init__

    def init(self, base, timeout=30.0):
        real_init(self, base, timeout)
        calls.base = base

    def run(argv, **kwargs):
        record(calls, argv, kwargs)
        if cmd(argv) == "whoami":
            return whoami(calls, argv)
        output = {"create": "provisioner output", "open": APP + "\n"}.get(cmd(argv), "")
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(CoderClient, "__init__", init)
    monkeypatch.setattr("xo.client.subprocess.run", run)
    monkeypatch.delenv("XO_CODER_URL", raising=False)
    monkeypatch.delenv("CODER_URL", raising=False)
    monkeypatch.delenv("XO_API_KEY", raising=False)
    monkeypatch.delenv("CODER_SESSION_TOKEN", raising=False)
    return calls


def invoke(capsys, *argv):
    code = cli.main(["coder", "github", REPO, *argv])
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    return code, out, err


def parameters(argv):
    return dict(next(csv.reader(io.StringIO(value)))[0].split("=", 1)
                for i, value in enumerate(argv) if i and argv[i - 1] == "--parameter")


def test_launch_uses_native_login_safe_defaults_and_actual_app_url(runner, capsys):
    code, out, err = invoke(capsys)
    assert code == 0
    assert json.loads(out) == {"name": "my-repo", "repoUrl": REPO,
                               "workspaceUrl": DEFAULT_CODER_URL + "/@alice/my-repo", "appUrl": APP}
    assert "port 3000 app is public" in err
    assert [cmd(c[0]) for c in runner] == ["whoami", "create", "open"]
    assert runner[0][0] == ["coder", "whoami", "--output", "json"]
    argv, options = runner[1]
    assert argv[4:7] == ["my-repo", "--template", "xo-github"]
    assert "--yes" in argv and "--no-wait=false" in argv and "--use-parameter-defaults" in argv
    assert parameters(argv) == {"repo_url": REPO, "repo_ref": "", "start_command": "auto",
                                 "capacity": "spot", "workspace_size": "xo.small", "home_disk_size": "5"}
    assert options["stdin"] == subprocess.DEVNULL and options["timeout"] == 600
    assert runner[2][0][3:] == ["open", "app", "my-repo", "xo-cowork"]
    assert runner[2][1]["env"]["CODER"] == "true"  # print URL; never launch a browser


@pytest.mark.parametrize("where", ["before", "middle", "after"])
def test_url_flag_wins_at_each_level(runner, capsys, monkeypatch, where):
    monkeypatch.setenv("XO_CODER_URL", "https://configured.example")
    monkeypatch.setenv("CODER_URL", "https://native.example")
    flags = ["--url", "https://xo.example", "--coder-url", "https://flag.example/"]
    argv = {"before": [*flags, "coder", "github", REPO],
            "middle": ["coder", *flags, "github", REPO],
            "after": ["coder", "github", REPO, *flags]}[where]
    assert cli.main(argv) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out)["repoUrl"] == REPO  # the global --url must not replace the positional URL
    assert all(c[0][1:3] == ["--url", "https://flag.example"] for c in runner[1:])


def test_url_environment_order_and_core_independence(runner, capsys, monkeypatch):
    monkeypatch.setenv("XO_URL", "invalid")
    monkeypatch.setenv("CODER_URL", "https://native.example")  # Coder's own setting is not read
    assert invoke(capsys)[0] == 0
    assert runner[-1][0][2] == DEFAULT_CODER_URL
    monkeypatch.setenv("XO_CODER_URL", "https://xo-setting.example")
    assert invoke(capsys)[0] == 0
    assert runner[-1][0][2] == "https://xo-setting.example"


def test_coder_does_not_read_xo_login_or_api_key(runner, capsys, monkeypatch):
    def unexpected(*a, **k):
        raise AssertionError("Coder must not read XO credentials")
    monkeypatch.setattr("xo.cli.saved_login", unexpected)
    monkeypatch.setattr("xo.cli.api_key", unexpected)
    assert invoke(capsys, "--api-key", "-")[0] == 0


def test_core_calls_ignore_coder_settings(fake, capsys, monkeypatch):
    from conftest import KEY
    monkeypatch.setenv("XO_CODER_URL", "invalid")
    monkeypatch.setenv("CODER_URL", "invalid")
    monkeypatch.setenv("CODER_SESSION_TOKEN", TOKEN)
    fake.route("GET", "/health", body={"ok": True})
    assert cli.main(["call", "health"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"ok": True} and TOKEN not in out + err
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_coder_url_environment_whitespace_matches_xo_convention(runner, monkeypatch):
    monkeypatch.setenv("XO_CODER_URL", "  ")
    monkeypatch.setenv("CODER_URL", " https://native.example/ ")
    assert coder_url() == DEFAULT_CODER_URL
    monkeypatch.setenv("XO_CODER_URL", " https://xo-setting.example/ ")
    assert coder_url() == "https://xo-setting.example"


def test_template_options_and_csv_shell_text_round_trip(runner, capsys):
    command = 'printf "%s,%s\\n" "hello" "$(literal)"\nnpm run dev -- --port 3000'
    code, out, _ = invoke(capsys, "--name", "preview", "--ref", "feature/test", "--start-command", command,
                          "--capacity", "ondemand", "--workspace-size", "xo.medium", "--home-disk-size", "12",
                          "--org", "team", "--wait-timeout", "120", "--timeout", "8")
    assert code == 0 and json.loads(out)["name"] == "preview"
    argv, options = runner[1]
    assert argv[argv.index("--org") + 1] == "team"
    assert parameters(argv) == {"repo_url": REPO, "repo_ref": "feature/test", "start_command": command,
                                 "capacity": "ondemand", "workspace_size": "xo.medium", "home_disk_size": "12"}
    assert options["timeout"] == 120 and runner[0][1]["timeout"] == 8
    assert "shell" not in options


def test_dry_run_is_offline_and_masks_known_credentials(runner, capsys, monkeypatch):
    monkeypatch.setenv("CODER_SESSION_TOKEN", TOKEN)
    monkeypatch.setenv("XO_SPEC", "/missing-spec")
    code, out, err = invoke(capsys, "--dry-run", "--start-command", f"echo {TOKEN}")
    assert code == 0 and runner == [] and "nothing sent" in err
    data = json.loads(out)
    assert data["parameters"]["start_command"] == "echo <redacted>"
    assert data["deploymentUrl"] == DEFAULT_CODER_URL


@pytest.mark.parametrize("url", ["http://github.com/o/r", "git@github.com:o/r", "https://github.com/o/r/tree/main",
                                  "https://token@github.com/o/r", "https://github.com/o/r?token=secret",
                                  "https://github.com/o/r#main", "https://github.com/../r", "https://github.com/o/.."])
def test_bad_repo_never_runs_coder(runner, capsys, url):
    assert cli.main(["coder", "github", url]) == 1
    out, err = capsys.readouterr()
    assert "HTTPS GitHub repository URL" in err and not out and runner == []


@pytest.mark.parametrize("url", ["http://remote.example", "ftp://host", "https://token@host",
                                  "https://host?token=secret", "https://host/#token", "https://host:bad",
                                  "https://host\n.invalid"])
def test_bad_deployment_url_is_unechoed_and_offline(runner, capsys, url):
    code, out, err = invoke(capsys, "--coder-url", url)
    assert code == 1 and "--coder-url must be https://" in err and url not in out + err
    assert runner == []


def test_localhost_is_allowed():
    assert coder_url("http://127.0.0.1:3000/") == "http://127.0.0.1:3000"


@pytest.mark.parametrize("flags", [["--name", "bad/name"], ["--home-disk-size", "0"],
                                   ["--home-disk-size", "101"], ["--wait-timeout", "nan"],
                                   ["--wait-timeout", "0"], ["--start-command", " "]])
def test_invalid_options_fail_before_login(runner, capsys, flags):
    assert invoke(capsys, *flags)[0] == 1
    assert runner == []


def test_name_normalization_and_git_suffix(runner, capsys):
    assert cli.main(["coder", "github", "https://github.com/example/My_App.git/", "--dry-run"]) == 0
    out, _ = capsys.readouterr()
    assert json.loads(out)["name"] == "my-app"
    assert json.loads(out)["parameters"]["repo_url"].endswith("/My_App.git")


def test_missing_coder_is_actionable(runner, capsys, monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr("xo.client.subprocess.run", missing)
    code, out, err = invoke(capsys)
    assert code == 1 and not out and "install coder" in err


def test_failure_does_not_echo_native_logs_or_retry(runner, capsys, monkeypatch):
    def fail(argv, **kwargs):
        record(runner, argv, kwargs)
        if cmd(argv) == "whoami":
            return whoami(runner, argv)
        return subprocess.CompletedProcess(argv, 1, "unrecognized private log", TOKEN)
    monkeypatch.setattr("xo.client.subprocess.run", fail)
    code, out, err = invoke(capsys)
    assert code == 1 and not out and "coder create failed" in err and "before retrying" in err
    assert "unrecognized private log" not in err and len(runner) == 2


def test_timeout_and_interrupt_warn_create_may_have_happened(runner, capsys, monkeypatch):
    for failure in [subprocess.TimeoutExpired("coder", 1), KeyboardInterrupt()]:
        def fail(argv, **kwargs):
            if cmd(argv) == "whoami":
                return whoami(runner, argv)
            raise failure
        monkeypatch.setattr("xo.client.subprocess.run", fail)
        code, out, err = invoke(capsys)
        assert code in (1, 130) and not out and "may have been received" in err


@pytest.mark.parametrize("output", ["", "https://app.example/?token=secret", "https://user:pass@app.example/",
                                     "https://app.example/\nprivate log"])
def test_bad_app_link_reports_created_without_echo_or_retry(runner, capsys, monkeypatch, output):
    def run(argv, **kwargs):
        runner.append(argv)
        if cmd(argv) == "whoami":
            return whoami(runner, argv)
        data = {"create": "", "open": output}[cmd(argv)]
        return subprocess.CompletedProcess(argv, 0, data, "")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    code, out, err = invoke(capsys)
    assert code == 1 and not out and "workspace was created" in err
    assert "secret" not in err and "private log" not in err and len(runner) == 3


def test_coder_works_with_a_broken_spec(runner, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("XO_SPEC", str(tmp_path / "absent.json"))
    assert invoke(capsys, "--dry-run")[0] == 0


@pytest.mark.parametrize("name", ["coder", "login", "logout"])
def test_builtins_do_not_collide_with_generated_commands(name):
    from xo import commands, spec
    op = spec.operations({"paths": {f"/{name}": {"get": {"operationId": "syntheticOperation"}}}})[0]
    assert commands.command_of(op) == name + "-api"


def test_login_logout_work_with_a_broken_spec(fake, capsys, monkeypatch, tmp_path):
    from conftest import KEY
    monkeypatch.setenv("XO_SPEC", str(tmp_path / "absent.json"))
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    fake.route("GET", f"/spaces/{cli.PROBE_SPACE}", status=404, body={"detail": "Space not found"})
    fake.route("GET", "/me", body={"data": {"username": "alice"}})
    assert cli.main(["login"]) == 0
    assert cli.main(["logout"]) == 0
    out, err = capsys.readouterr()
    assert KEY not in out + err and "logged in as alice" in err and "deleted" in err


def test_real_subprocess_boundary_with_fake_coder(capsys, monkeypatch, tmp_path):
    """Exercise executable lookup, argv encoding, env and JSON output without a network."""
    binary = tmp_path / "coder"
    binary.write_text(f"#!{sys.executable}\n" + '''import csv, io, json, os, sys
args = sys.argv[1:]
assert "XO_API_KEY" not in os.environ and "CLERK_SECRET_KEY" not in os.environ and "CODER_URL" not in os.environ
if args[0] == "whoami":
    assert args == ["whoami", "--output", "json"]
    print('[{"username":"alice","url":"https://coder.example/"}]')
    sys.exit(0)
assert args[:2] == ["--url", "https://coder.example"]
if args[2] == "create":
    params = dict(next(csv.reader(io.StringIO(v)))[0].split("=", 1)
                  for i, v in enumerate(args) if i and args[i-1] == "--parameter")
    assert params["capacity"] == "spot"
    assert params["start_command"] == 'printf "a,b"; npm run dev'
    print("private provisioner log must not show")
else:
    assert args[2:] == ["open", "app", "my-repo", "xo-cowork"]
    assert os.environ["CODER"] == "true"
    print("https://native-generated.example/")
''')
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("XO_API_KEY", "ak_never_for_coder")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_never_for_coder")
    monkeypatch.setenv("CODER_URL", "https://elsewhere.example")
    code, out, err = invoke(capsys, "--coder-url", "https://coder.example",
                            "--start-command", 'printf "a,b"; npm run dev')
    assert code == 0 and json.loads(out)["appUrl"] == "https://native-generated.example/"
    assert "private provisioner log" not in out + err


def test_a_login_to_another_deployment_stops_before_anything_is_sent(runner, capsys):
    runner.at = "https://coder.theircompany.example"
    code, out, err = invoke(capsys)
    assert code == 1 and not out
    assert "Coder is logged in to https://coder.theircompany.example, not https://dev-coder.xo.builders" in err
    assert "nothing was sent" in err
    assert [c[0] for c in runner] == [["coder", "whoami", "--output", "json"]]  # no --url call at all


def test_not_logged_in_says_how(runner, capsys, monkeypatch):
    def run(argv, **kwargs):
        record(runner, argv, kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "You are not logged in")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    code, _, err = invoke(capsys)
    assert code == 1 and "Coder is not logged in; run xo coder login" in err and len(runner) == 1


@pytest.mark.parametrize("answer", ['[]', '{"username": "alice"}', '[{"username": "alice"}]',
                                    '[{"username": "a b", "url": "https://dev-coder.xo.builders"}]',
                                    '[{"username": "alice", "url": "https://x\'$(id).example"}]'])
def test_an_odd_whoami_answer_stops(runner, capsys, monkeypatch, answer):
    def run(argv, **kwargs):
        record(runner, argv, kwargs)
        return subprocess.CompletedProcess(argv, 0, answer, "")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    code, _, err = invoke(capsys)
    assert code == 1 and "unexpected answer" in err and len(runner) == 1


def test_the_coder_process_gets_no_xo_or_clerk_secrets(runner, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return whoami(runner, argv) if cmd(argv) == "whoami" else subprocess.CompletedProcess(argv, 0, APP, "")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    for name in ("XO_API_KEY", "XO_CODER_URL", "CODER_URL", "CLERK_SECRET_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(name, "https://x.example" if "URL" in name else "secret-value")
    monkeypatch.setenv("CODER_CONFIG_DIR", "/kept")
    assert cli.main(["coder", "github", REPO]) == 0
    for env in seen:
        assert not {"XO_API_KEY", "XO_CODER_URL", "CODER_URL", "CLERK_SECRET_KEY", "ELEVENLABS_API_KEY"} & set(env)
        assert env["CODER_CONFIG_DIR"] == "/kept"


@pytest.mark.parametrize("name", ["my--repo", "new", "create", "-a", "a-", "a" * 33, "a_b"])
def test_names_coder_would_refuse_are_refused_first(runner, capsys, name):
    code, _, err = invoke(capsys, f"--name={name}")
    assert code == 1 and "--name must be" in err and runner == []


@pytest.mark.parametrize("repo, expected", [("my--repo", "my-repo"), ("new", "new-app"), ("Create", "create-app"),
                                            ("a__b..c", "a-b-c")])
def test_default_names_follow_coders_rule(runner, capsys, repo, expected):
    assert cli.main(["coder", "github", f"https://github.com/o/{repo}", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr()[0])["name"] == expected


@pytest.mark.parametrize("argv", [["coder", "list", "--all"], ["--coder-url", "https://c.example", "coder", "list", "--all"],
                                  ["coder", "--coder-url=https://c.example", "list", "--all"]])
def test_coder_verbs_pass_through_after_the_check(runner, argv):
    assert cli.main(argv) == 0
    base = runner.base
    assert [c[0] for c in runner] == [["coder", "whoami", "--output", "json"],
                                      ["coder", "--url", base, "list", "--all"]]
    assert "stdin" not in runner[1][1] and "capture_output" not in runner[1][1]  # the terminal, as is


def test_a_pass_through_to_another_login_sends_nothing(runner, capsys):
    runner.at = "https://elsewhere.example"
    assert cli.main(["coder", "ssh", "my-preview"]) == 1
    assert "nothing was sent" in capsys.readouterr()[1] and len(runner) == 1


def test_coder_login_when_already_there(runner, capsys):
    assert cli.main(["coder", "login"]) == 0
    assert "already logged in to https://dev-coder.xo.builders as alice" in capsys.readouterr()[1]
    assert len(runner) == 1


def test_coder_login_without_a_terminal_or_with_a_token(runner, capsys, monkeypatch):
    runner.at = "https://elsewhere.example"
    assert cli.main(["coder", "login"]) == 1
    assert "coder login needs a terminal" in capsys.readouterr()[1]
    monkeypatch.setenv("CODER_SESSION_TOKEN", TOKEN)
    assert cli.main(["coder", "login"]) == 1
    assert "CODER_SESSION_TOKEN is set" in capsys.readouterr()[1]


def test_coder_login_replaces_another_login_only_when_asked(runner, capsys, monkeypatch):
    runner.at = "https://elsewhere.example"
    monkeypatch.setattr("xo.client.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("xo.client.sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_ask", lambda q: False)
    assert cli.main(["coder", "login"]) == 1
    assert "kept the Coder login to https://elsewhere.example" in capsys.readouterr()[1]
    assert len(runner) == 1
    runner.clear()
    monkeypatch.setattr(cli, "_ask", lambda q: True)
    real = runner.at

    def run(argv, **kwargs):
        record(runner, argv, kwargs)
        if argv[1] == "login":
            runner.at = None  # now logged in where xo asked
            return subprocess.CompletedProcess(argv, 0, "", "")
        return whoami(runner, argv)
    monkeypatch.setattr("xo.client.subprocess.run", run)
    assert real and cli.main(["coder", "login"]) == 0
    assert "logged in to https://dev-coder.xo.builders as alice" in capsys.readouterr()[1]
    assert [c[0] for c in runner][1] == ["coder", "login", DEFAULT_CODER_URL]


def test_coder_logout_only_for_this_deployment(runner, capsys, monkeypatch):
    runner.at = "https://elsewhere.example"
    assert cli.main(["coder", "logout"]) == 0
    assert "left alone: Coder is logged in to https://elsewhere.example" in capsys.readouterr()[1]
    assert len(runner) == 1
    runner.at = None
    runner.clear()
    monkeypatch.setattr(cli, "_ask", lambda q: True)
    assert cli.main(["coder", "logout"]) == 0
    assert "logged out of https://dev-coder.xo.builders" in capsys.readouterr()[1]
    assert [c[0] for c in runner][1] == ["coder", "--url", DEFAULT_CODER_URL, "logout", "--yes"]


def test_there_is_no_coder_delete(runner, capsys):
    assert "delete" not in cli.CODER_PASS
    with pytest.raises(SystemExit):
        cli.main(["coder", "delete", "my-preview"])
    assert runner == []


ROOT_OPTIONS = [["--url", "https://evil.example"], ["--url=https://evil.example"], ["--url", "https://xo.quirq.dev"],
                ["--token", "T"], ["--token=T"], ["--global-config", "/other"], ["--global-config=/other"],
                ["--use-keyring"], ["--use-keyring=false"], ["--header", "X: y"], ["--header=X: y"],
                ["--header-command", "cmd"], ["--header-command=cmd"], ["--client-tls-cert", "c"],
                ["--client-tls-key=k"]]


@pytest.mark.parametrize("verb", [["list"], ["ssh", "my-preview"]])
@pytest.mark.parametrize("options", ROOT_OPTIONS)
def test_pass_through_refuses_coders_root_options(runner, capsys, verb, options):
    assert cli.main(["coder", *verb, *options]) == 1
    err = capsys.readouterr()[1]
    assert f"does not pass {options[0].partition('=')[0]}" in err and "nothing was sent" in err
    assert runner == []  # refused before Coder is even asked where it is logged in


@pytest.mark.parametrize("argv", [["list", "--search", "--", "--url", "https://evil.example"],
                                  ["list", "--search", "--", "--url=https://evil.example"],
                                  ["ssh", "my-preview", "-e", "--", "--url", "https://evil.example"],
                                  ["ssh", "my-preview", "-l", "--", "--token", "T"],
                                  ["ssh", "my-preview", "-l", "--", "--token=T"],
                                  ["ssh", "my-preview", "--", "echo", "--url", "x"]])
def test_root_options_after_a_double_dash_are_refused_too(runner, capsys, argv):
    # pflag takes "--" as the value of a flag that wants one and goes on parsing after it.
    assert cli.main(["coder", *argv]) == 1
    assert "nothing was sent" in capsys.readouterr()[1] and runner == []


def test_pass_through_runs_with_the_scrubbed_environment(runner, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        return whoami(runner, argv) if cmd(argv) == "whoami" else subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    monkeypatch.setenv("XO_API_KEY", "secret-value")
    monkeypatch.setenv("CODER_URL", "https://elsewhere.example")
    assert cli.main(["coder", "list"]) == 0
    assert [a[1] for a, _ in seen] == ["whoami", "--url"]
    for _, env in seen:
        assert "XO_API_KEY" not in env and "CODER_URL" not in env


@pytest.mark.parametrize("coder_env", [None, "https://elsewhere.example", "not a url"])
@pytest.mark.parametrize("argv", [["coder", "list"], ["coder", "logout"], ["coder", "github", REPO]])
def test_a_session_token_needs_coder_url_for_this_deployment(runner, capsys, monkeypatch, coder_env, argv):
    monkeypatch.setenv("CODER_SESSION_TOKEN", TOKEN)
    if coder_env:
        monkeypatch.setenv("CODER_URL", coder_env)
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    assert f"CODER_URL must name {DEFAULT_CODER_URL}" in err and "nothing was sent" in err
    assert runner == []  # not even whoami: it would send the token to Coder's saved deployment


def test_a_session_token_with_matching_coder_url_keeps_them_together(runner, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return whoami(runner, argv) if cmd(argv) == "whoami" else subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("xo.client.subprocess.run", run)
    monkeypatch.setenv("CODER_SESSION_TOKEN", TOKEN)
    monkeypatch.setenv("CODER_URL", DEFAULT_CODER_URL + "/")
    assert cli.main(["coder", "list"]) == 0
    assert len(seen) == 2
    for env in seen:
        assert env["CODER_URL"] == DEFAULT_CODER_URL + "/" and env["CODER_SESSION_TOKEN"] == TOKEN


def failing_whoami(runner, monkeypatch, *, stderr="error: unauthorized", timeout=False, then=None):
    def run(argv, **kwargs):
        record(runner, argv, kwargs)
        if cmd(argv) == "whoami" and not runner.at:
            if timeout:
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 1, "", stderr)
        if argv[1] == "login":
            runner.at = DEFAULT_CODER_URL
            return subprocess.CompletedProcess(argv, 0, "", "")
        return whoami(runner, argv)
    monkeypatch.setattr("xo.client.subprocess.run", run)


@pytest.mark.parametrize("timeout", [False, True])
def test_an_unclear_whoami_failure_is_not_read_as_logged_out(runner, capsys, monkeypatch, timeout):
    failing_whoami(runner, monkeypatch, stderr=f"error: {TOKEN} expired", timeout=timeout)
    assert cli.main(["coder", "list"]) == 1
    out, err = capsys.readouterr()
    assert "could not tell where Coder is logged in" in err and "nothing was sent" in err
    assert TOKEN not in out + err and len(runner) == 1


@pytest.mark.parametrize("timeout", [False, True])
def test_coder_login_asks_before_replacing_when_whoami_is_unclear(runner, capsys, monkeypatch, timeout):
    failing_whoami(runner, monkeypatch, timeout=timeout)
    monkeypatch.setattr("xo.client.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("xo.client.sys.stdout.isatty", lambda: True, raising=False)
    questions = []
    monkeypatch.setattr(cli, "_ask", lambda q: questions.append(q) or False)
    assert cli.main(["coder", "login"]) == 1
    assert "kept Coder's login as it is; nothing changed" in capsys.readouterr()[1]
    assert len(runner) == 1 and "replacing any login it has" in questions[0]
    runner.clear()
    monkeypatch.setattr(cli, "_ask", lambda q: True)
    assert cli.main(["coder", "login"]) == 0
    assert [c[0][1] for c in runner] == ["whoami", "login", "whoami"]


def test_coder_login_without_a_login_does_not_ask(runner, capsys, monkeypatch):
    failing_whoami(runner, monkeypatch, stderr="error: You are not logged in. Try logging in using 'coder login <url>'.")
    monkeypatch.setattr("xo.client.sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("xo.client.sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli, "_ask", lambda q: pytest.fail("asked: " + q))
    assert cli.main(["coder", "login"]) == 0
    assert [c[0][1] for c in runner] == ["whoami", "login", "whoami"]


def test_coder_logout_asks_first(runner, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_ask", lambda q: False)
    assert cli.main(["coder", "logout"]) == 0
    assert "left logged in" in capsys.readouterr()[1] and len(runner) == 1


def test_no_url_call_before_the_check():
    with pytest.raises(Exception, match="not checked"):
        CoderClient(DEFAULT_CODER_URL)._command(["list"])


@pytest.mark.parametrize("argv", [["--url", "https://x.example", "coder", "list"], ["coder", "--url", "https://x.example", "list"],
                                  ["--api-key", "ak_x", "coder", "list"], ["--spec=s.json", "coder", "--timeout", "5", "list"]])
def test_xo_options_before_a_pass_through_verb(runner, argv):
    assert cli.main(argv) == 0
    assert [c[0] for c in runner][1] == ["coder", "--url", DEFAULT_CODER_URL, "list"]


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "x"])
def test_a_bad_timeout_before_a_pass_through_verb_is_a_usage_error(runner, capsys, value):
    with pytest.raises(SystemExit) as e:
        cli.main(["--timeout", value, "coder", "list"])
    assert e.value.code == 2 and runner == []
    assert "invalid number of seconds" in capsys.readouterr()[1]


def test_a_pass_through_verb_reached_through_argparse_fails_cleanly(runner, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_coder_pass", lambda argv: None)
    assert cli.main(["coder", "list"]) == 1
    err = capsys.readouterr()[1]
    assert "put coder's own arguments after it" in err and "unexpected" not in err and runner == []



def test_a_real_coder_on_path_is_never_found(monkeypatch, tmp_path):
    """The no_real_coder guard in conftest: xo finds no coder outside pytest's temp root, and a
    directory holding one counts as real unless it is under that root."""
    import os
    import shutil
    from conftest import _coder_dirs
    assert shutil.which("coder") is None
    (tmp_path / "coder").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "coder").chmod(0o700)
    assert _coder_dirs(str(tmp_path)) == []
    assert _coder_dirs(str(tmp_path), base=str(tmp_path / "elsewhere")) == [str(tmp_path)]
    monkeypatch.chdir(tmp_path)  # an empty PATH entry is the current folder
    assert _coder_dirs(os.pathsep.join(["/nonexistent", ""]), base=str(tmp_path / "elsewhere")) == [""]
