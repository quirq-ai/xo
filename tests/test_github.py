"""gh is a fake here: tests never run the real gh or reach GitHub."""
from __future__ import annotations

import subprocess
import types

import pytest

from xo import cli, github

SECRET = "gho_test_token_never_printed"


class FakeGh:
    """Scripted gh: `user` is who it is logged in as (None: not logged in), `revoked` makes GitHub
    refuse the stored login. Every call is recorded with the environment gh was given."""

    def __init__(self, monkeypatch, user="octocat"):
        self.user, self.revoked, self.calls = user, False, []
        monkeypatch.setattr(github, "installed", lambda: True)
        monkeypatch.setattr(github, "subprocess", types.SimpleNamespace(
            run=self.run, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired))

    def run(self, argv, **options):
        env = options.get("env") or {}
        self.calls.append((argv, env))
        assert argv[0] == "gh" and "token" not in argv, "xo never asks gh for its token"
        done = lambda code, out="", err="": subprocess.CompletedProcess(argv, code, out, err)  # noqa: E731
        if argv[1:3] == ["api", "--hostname"]:
            if self.user and not self.revoked:
                return done(0, f"{self.user}\n")
            return done(1, "", f"gh: Bad credentials (HTTP 401) {SECRET}")
        if argv[1:3] == ["auth", "status"]:
            if self.revoked:
                return done(1, "", f"X Failed to log in to github.com using token ({SECRET})")
            if self.user is None:
                return done(1, "", "You are not logged into any GitHub hosts. Run gh auth login to authenticate.")
            return done(0, "", f"  ✓ Logged in to github.com as {self.user}\n  ✓ Token: {SECRET}")
        if argv[1:3] == ["auth", "login"]:
            self.user, self.revoked = "octocat", False
            return done(0)
        if argv[1:3] == ["auth", "logout"]:
            self.user = None
            return done(0)
        raise AssertionError(f"unexpected gh call {argv}")

    def ran(self, *words):
        return [argv for argv, _ in self.calls if argv[1:1 + len(words)] == list(words)]


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    assert SECRET not in out + err, "gh's output (which can carry its token) is never printed"
    return code, out, err


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(github, "_terminal", lambda: True)


def test_every_gh_call_names_github_com_and_gets_no_xo_secrets(monkeypatch, capsys):
    gh = FakeGh(monkeypatch)
    monkeypatch.setenv("XO_API_KEY", "ak_test_secret")
    monkeypatch.setenv("GH_HOST", "evil.example")
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test")
    assert run(capsys, "login", "github")[0] == 0
    for argv, env in gh.calls:
        assert argv[argv.index("--hostname") + 1] == "github.com"
        assert not {"XO_API_KEY", "GH_HOST", "CLERK_SECRET_KEY"} & set(env)


def test_already_connected_runs_no_login(monkeypatch, capsys):
    gh = FakeGh(monkeypatch)
    code, _, err = run(capsys, "login", "github")
    assert code == 0 and "xo: github: already connected to github.com as octocat\n" in err
    assert gh.ran("auth", "login") == []


def test_login_runs_gh_in_the_terminal_and_checks_it_worked(monkeypatch, capsys, terminal):
    gh = FakeGh(monkeypatch, user=None)
    code, _, err = run(capsys, "login", "github")
    assert code == 0 and "xo: github: connected to github.com as octocat\n" in err
    assert gh.ran("auth", "login") == [["gh", "auth", "login", "--hostname", "github.com",
                                        "--git-protocol", "https", "--web"]]


def test_login_needs_a_terminal(monkeypatch, capsys):
    gh = FakeGh(monkeypatch, user=None)
    code, _, err = run(capsys, "login", "github")
    assert code == 1 and "gh auth login needs a terminal" in err and gh.ran("auth", "login") == []


def test_a_login_github_refuses_is_replaced_only_on_yes(monkeypatch, capsys, terminal):
    gh = FakeGh(monkeypatch)
    gh.revoked = True
    monkeypatch.setattr(cli, "_ask", lambda question: False)
    code, _, err = run(capsys, "login", "github")
    assert code == 1 and "kept gh's login as it is" in err and gh.ran("auth", "login") == []
    monkeypatch.setattr(cli, "_ask", lambda question: True)
    code, _, err = run(capsys, "login", "github")
    assert code == 0 and "connected to github.com as octocat" in err


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_a_token_in_the_environment_is_named_and_never_replaced(monkeypatch, capsys, terminal, name):
    gh = FakeGh(monkeypatch)
    monkeypatch.setenv(name, SECRET)
    code, _, err = run(capsys, "login", "github")
    assert code == 0 and f"already connected to github.com as octocat, with {name} from this terminal" in err
    gh.revoked = True
    code, _, err = run(capsys, "login", "github")
    assert code == 1 and f"{name} is set in this terminal and GitHub does not accept it" in err
    assert gh.ran("auth", "login") == []
    code, _, err = run(capsys, "logout")
    assert f"xo: github: left alone: {name} is set" in err and gh.ran("auth", "logout") == []


def test_logout_asks_before_logging_gh_out(monkeypatch, capsys):
    gh = FakeGh(monkeypatch)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda question: asked.append(question) or False)
    code, _, err = run(capsys, "logout")
    assert code == 0 and "xo: github: left logged in\n" in err and gh.ran("auth", "logout") == []
    assert asked == ["Log gh out of github.com (octocat)? git over https on this machine may use the same login"]
    monkeypatch.setattr(cli, "_ask", lambda question: True)
    code, _, err = run(capsys, "logout")
    assert "xo: github: logged out of github.com\n" in err
    assert gh.ran("auth", "logout") == [["gh", "auth", "logout", "--hostname", "github.com"]]


def test_status_shows_the_github_row(monkeypatch, capsys):
    code, out, _ = run(capsys, "status")
    assert "github https://github.com" in out and "the GitHub CLI is not installed" in out
    gh = FakeGh(monkeypatch)
    out = run(capsys, "status")[1]
    assert "logged in as octocat" in out
    gh.user = None
    assert "not logged in; run xo login github" in run(capsys, "status")[1]
    gh.user, gh.revoked = "octocat", True
    assert "GitHub did not accept it" in run(capsys, "status")[1]


def test_an_odd_answer_from_gh_is_refused(monkeypatch, capsys):
    gh = FakeGh(monkeypatch)
    gh.user = "not a login\nX"
    code, _, err = run(capsys, "login", "github")
    assert code == 1 and "gh api user returned an unexpected answer" in err


def test_login_without_gh_says_skipped(capsys):
    code, _, err = run(capsys, "login", "github")
    assert code == 0 and "xo: github: skipped: the GitHub CLI is not installed (https://cli.github.com)\n" in err
