"""xo gh and xo sh: other CLIs run through xo. SYNTHETIC: gh and spacehub here are shell scripts on a
PATH of their own that print what they were given, so no real gh or hub client runs."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import KEY
from xo import attach, cli, quirq

SCRIPT = """#!/bin/sh
printf 'args:'
for a in "$@"; do printf ' [%s]' "$a"; done
printf '\\n'
printf 'server=%s key=%s gh_token=%s gh_host=%s hub_token=%s\\n' \\
  "${SPACEHUB_SERVER-}" "${XO_API_KEY-}" "${GH_TOKEN-}" "${GH_HOST-}" "${SPACEHUB_TOKEN-}"
exit "${EXIT:-0}"
"""


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Fake gh and spacehub, alone on PATH, run for real (the autouse stub is put back aside)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in ("gh", quirq.CLIENT):
        path = bin_dir / program
        path.write_text(SCRIPT)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(attach, "subprocess", subprocess)
    monkeypatch.setenv("XO_API_KEY", KEY)
    for name, value in (("GH_TOKEN", "gh-secret"), ("GH_HOST", "ghe.example"), ("SPACEHUB_TOKEN", "hub-secret")):
        monkeypatch.setenv(name, value)
    return bin_dir


def run(capfd, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capfd.readouterr()
    assert KEY not in out and KEY not in err
    return code, out, err


def test_xo_gh_passes_everything_to_gh_and_keeps_xos_keys_and_host_out(tools, capfd):
    code, out, err = run(capfd, "gh", "pr", "list", "--json", "title", "--help", "a b")
    assert code == 0, err
    assert "args: [pr] [list] [--json] [title] [--help] [a b]" in out
    # gh keeps the GitHub token it would use anyway, never xo's key or another host.
    assert "key= gh_token=gh-secret gh_host= " in out


def test_xo_gh_returns_the_tools_exit_code(tools, capfd, monkeypatch):
    monkeypatch.setenv("EXIT", "3")
    assert run(capfd, "gh", "auth", "status")[0] == 3


def test_xo_sh_runs_spacehub_on_the_hub_xo_login_found(tools, capfd):
    quirq.save(quirq.Hub("https://hub.quirq.dev", "alice"))
    code, out, err = run(capfd, "sh", "project", "list")
    assert code == 0, err
    assert "args: [project] [list]" in out
    assert "server=wss://hub.quirq.dev key= gh_token= " in out and out.rstrip().endswith("hub_token=")


def test_xo_sh_without_a_hub_says_to_log_in_and_runs_nothing(tools, capfd):
    code, out, err = run(capfd, "sh", "project", "list")
    assert code == 1 and out == ""
    assert "xo: xo is not connected to a quirq hub; run xo login" in err


def test_a_tool_that_is_not_installed_says_how_to_get_it(tools, capfd):
    (tools / "gh").unlink()
    code, out, err = run(capfd, "gh", "pr", "list")
    assert code == 1 and out == ""
    assert "gh is not installed; install it from https://cli.github.com" in err


def test_xos_own_options_before_the_tool_are_refused(tools, capfd):
    code, out, err = run(capfd, "--json", "gh", "pr", "list")
    assert code == 2 and out == ""
    assert "put gh first: xo gh ARGS" in err


def test_help_lists_gh_and_sh(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "run GitHub's gh with these arguments" in out
    assert "run the quirq hub's client spacehub" in out


def test_sh_is_never_a_program_name():
    assert attach.TOOLS["sh"].program == quirq.CLIENT == "spacehub"
    assert not any(Path(tool.program).name == "sh" for tool in attach.TOOLS.values())
