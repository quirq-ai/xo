"""A space's GitHub projects through the quirq hub: xo login finds the hub, install adds the quirq
remote, update and sync push to and rebase on the hub and never on GitHub. SYNTHETIC: the hub here is
bare repositories over file:// standing in for quirq://hub.quirq.dev/AGENT/OWNER--REPO (git-remote-quirq
is space-hub's, not tested here)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import KEY, git
from test_cli import core, login, run, xs  # noqa: F401 - fixtures
from test_workspace import _stored, space  # noqa: F401 - fixtures
from xo import cli, gitops, quirq, spacerepo, workspace
from xo.client import XoError

TOKEN = "hub_token_never_printed"


@pytest.fixture
def hub(space, tmp_path, monkeypatch):  # noqa: F811
    """xo login found the hub at https://hub.quirq.dev as agent alice; its copies are bare repos."""
    root = tmp_path / "quirqhub"
    root.mkdir()
    made: list[str] = []
    monkeypatch.setattr(quirq, "installed", lambda: True)
    monkeypatch.setattr(quirq.Hub, "url", lambda self, project: f"file://{root}/{quirq.name(project)}.git")

    def create(hub, project):
        made.append(project)
        git("init", "-q", "--bare", str(root / f"{quirq.name(project)}.git"))
    monkeypatch.setattr(quirq, "create", create)
    quirq.save(quirq.Hub("https://hub.quirq.dev", "alice"))
    space.made, space.quirq_root = made, root
    return space


def _on_github(space, project, ref):
    return git("rev-parse", "--verify", "--quiet", ref, cwd=space.hub.root / project)


def test_a_hub_url_is_always_a_quirq_url_named_after_the_agent_and_repo():
    h = quirq.Hub("https://hub.quirq.dev", "alice")
    assert h.url("juanfont/headscale") == "quirq://hub.quirq.dev/alice/juanfont--headscale"
    assert h.websocket == "wss://hub.quirq.dev"
    with pytest.raises(XoError, match="no GitHub repo"):
        h.url("notes")
    with pytest.raises(XoError):
        quirq.parse("http://hub.quirq.dev", "alice")
    with pytest.raises(XoError):
        quirq.parse("https://hub.quirq.dev/x", "alice")
    with pytest.raises(XoError):
        quirq.parse("https://hub.quirq.dev", "Al ice")


def test_update_pushes_new_work_to_the_hub_and_never_to_github(hub):
    workspace.install(hub.client, "space", "dev")
    assert git("remote", "get-url", "quirq", cwd="dev/a").startswith("file://")
    git("switch", "-q", "-c", "feature", cwd="dev/a")
    Path("dev/a/README").write_text("agent work\n")
    git("commit", "-q", "-am", "agent work", cwd="dev/a")
    mine = git("rev-parse", "HEAD", cwd="dev/a")
    out = workspace.update(hub.client, "space", "dev")
    assert out["projects"][0] == {"o/a": "feature"}
    assert git("rev-parse", "refs/heads/feature", cwd=hub.quirq_root / "o--a.git") == mine
    with pytest.raises(Exception):
        _on_github(hub, "o/a", "refs/heads/feature")  # GitHub never saw it
    assert hub.made == ["o/a"]  # the hub copy was made once, for the one project with new work
    assert git("remote", "get-url", "origin", cwd="dev/a").endswith("/o/a")
    assert git("config", "--get", "branch.main.remote", cwd="dev/a") == "origin"  # upstreams as they were


def test_update_with_nothing_new_pushes_nothing_anywhere(hub, monkeypatch):
    workspace.install(hub.client, "space", "dev")
    monkeypatch.setattr(gitops, "push", lambda dest: pytest.fail("pushed to GitHub"))
    workspace.update(hub.client, "space", "dev")
    assert hub.made == [] and list(hub.quirq_root.iterdir()) == []


def test_without_the_hub_update_only_pushes_a_branch_with_new_commits(space, monkeypatch):  # noqa: F811
    """#50: a repo you cannot push to (here: any push fails) is no problem until you commit to it."""
    workspace.install(space.client, "space", "dev")
    monkeypatch.setattr(gitops, "push", lambda dest: (_ for _ in ()).throw(XoError(f"./{dest}: 403")))
    workspace.update(space.client, "space", "dev")
    Path("dev/a/README").write_text("mine\n")
    git("commit", "-q", "-am", "mine", cwd="dev/a")
    with pytest.raises(XoError, match="403"):
        workspace.update(space.client, "space", "dev")


def test_install_elsewhere_takes_a_branch_only_the_hub_has(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    git("switch", "-q", "-c", "feature", cwd="dev/a")
    Path("dev/a/README").write_text("agent work\n")
    git("commit", "-q", "-am", "agent work", cwd="dev/a")
    mine = git("rev-parse", "HEAD", cwd="dev/a")
    workspace.update(hub.client, "space", "dev")
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)  # another machine, or another agent of yours
    workspace.install(hub.client, "space", "dev")
    assert git("rev-parse", "HEAD", cwd="dev/a") == mine
    assert git("branch", "--show-current", cwd="dev/a") == "feature"
    assert git("config", "--get", "branch.feature.remote", cwd="dev/a") == "quirq"  # GitHub has no feature


def test_install_prefers_the_hubs_branch_and_keeps_githubs_upstream(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    Path("dev/a/README").write_text("on the hub\n")
    git("commit", "-q", "-am", "on the hub", cwd="dev/a")
    mine = git("rev-parse", "HEAD", cwd="dev/a")
    workspace.update(hub.client, "space", "dev")  # main: ahead of GitHub's, pushed to the hub only
    assert _on_github(hub, "o/a", "refs/heads/main") != mine
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)
    workspace.install(hub.client, "space", "dev")
    assert git("rev-parse", "HEAD", cwd="dev/a") == mine
    assert git("config", "--get", "branch.main.remote", cwd="dev/a") == "origin"


def test_sync_rebases_on_the_hubs_branch_then_pushes_there(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    Path("dev/a/README").write_text("first agent\n")
    git("commit", "-q", "-am", "first agent", cwd="dev/a")
    workspace.update(hub.client, "space", "dev")
    first = Path.cwd()
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)
    workspace.install(hub.client, "space", "dev")
    Path("dev/a/OTHER").write_text("second agent\n")
    git("add", "OTHER", cwd="dev/a")
    git("commit", "-q", "-m", "second agent", cwd="dev/a")
    workspace.update(hub.client, "space", "dev")
    os.chdir(first)
    Path("dev/a/MINE").write_text("first agent again\n")
    git("add", "MINE", cwd="dev/a")
    git("commit", "-q", "-m", "first agent again", cwd="dev/a")
    workspace.sync(hub.client, "space", "dev")
    log = git("log", "--format=%s", "refs/heads/main", cwd=hub.quirq_root / "o--a.git").splitlines()
    assert log[:3] == ["first agent again", "second agent", "first agent"]
    assert git("rev-parse", "HEAD", cwd="dev/a") == git("rev-parse", "refs/heads/main", cwd=hub.quirq_root / "o--a.git")


def test_a_project_github_does_not_show_everyone_is_never_sent_to_the_hub(hub, monkeypatch):
    workspace.install(hub.client, "space", "dev")
    monkeypatch.setattr(spacerepo, "heads", lambda owner, name: None)  # private: looks missing
    Path("dev/a/README").write_text("secret\n")
    git("commit", "-q", "-am", "secret", cwd="dev/a")
    with pytest.raises(XoError, match="not public on GitHub.*nothing was pushed"):
        workspace.update(hub.client, "space", "dev")
    assert hub.made == [] and list(hub.quirq_root.iterdir()) == []


def test_a_quirq_remote_pointing_elsewhere_is_refused_never_changed(hub):
    workspace.install(hub.client, "space", "dev")
    git("remote", "set-url", "quirq", "https://example.com/elsewhere", cwd="dev/a")
    with pytest.raises(XoError, match="has a quirq remote that is not"):
        workspace.update(hub.client, "space", "dev")
    assert git("remote", "get-url", "quirq", cwd="dev/a") == "https://example.com/elsewhere"


def test_a_hub_without_its_client_here_is_an_error_not_a_quiet_github_push(hub, monkeypatch):
    workspace.install(hub.client, "space", "dev")
    monkeypatch.setattr(quirq, "installed", lambda: False)
    with pytest.raises(XoError, match="git-remote-quirq is not installed; install the hub's client"):
        workspace.update(hub.client, "space", "dev")


def test_a_hub_file_others_could_write_is_not_used(config_home):
    path = quirq.save(quirq.Hub("https://hub.quirq.dev", "alice"))
    assert quirq.settings() == quirq.Hub("https://hub.quirq.dev", "alice")
    os.chmod(path, 0o666)
    assert quirq.settings() is None


def test_login_links_the_hub_agent_and_status_and_logout_show_it(login, capsys, config_home, monkeypatch):  # noqa: F811
    login.route("POST", "/me/hub", body={"success": True, "data": {
        "server": "https://hub.quirq.dev", "agent": "sam", "token": TOKEN}})
    linked = []
    monkeypatch.setattr(quirq, "installed", lambda: True)
    monkeypatch.setattr(quirq, "link", lambda hub, token: linked.append((hub, token)))
    code, _, err = run(capsys, "login")
    assert code == 0 and TOKEN not in err
    assert linked == [(quirq.Hub("https://hub.quirq.dev", "sam"), TOKEN)]
    assert "xo: hub: agent sam on https://hub.quirq.dev; a space's xo update and xo sync push there" in err
    assert json.loads((config_home / "xo" / "hub").read_text()) == {"server": "https://hub.quirq.dev", "agent": "sam"}
    assert TOKEN not in (config_home / "xo" / "hub").read_text()
    code, out, _ = run(capsys, "status")
    assert "hub    https://hub.quirq.dev" in out and "agent sam" in out
    code, _, err = run(capsys, "logout")
    assert "xo: hub: deleted" in err and quirq.settings() is None


def test_login_without_the_hubs_client_says_how_to_get_it_and_saves_nothing(login, capsys, monkeypatch):  # noqa: F811
    login.route("POST", "/me/hub", body={"success": True, "data": {
        "server": "https://hub.quirq.dev", "agent": "sam", "token": TOKEN}})
    monkeypatch.setattr(quirq, "installed", lambda: False)
    code, _, err = run(capsys, "login")
    assert code == 0 and TOKEN not in err
    assert "xo: hub: skipped: the xo server works with the quirq hub" in err and "install-cli.sh" in err
    assert quirq.settings() is None


def test_the_hubs_client_gets_the_token_on_stdin_and_none_of_xos_keys(monkeypatch):
    seen = {}

    def fake_run(argv, env, input, **kw):
        seen.update(argv=argv, env=env, input=input)
        import subprocess
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")
    monkeypatch.setattr(quirq.subprocess, "run", fake_run)
    monkeypatch.setenv("XO_API_KEY", KEY)
    monkeypatch.setenv("SPACEHUB_TOKEN", "someone_elses")
    quirq.link(quirq.Hub("https://hub.quirq.dev", "sam"), TOKEN)
    assert seen["argv"] == ["spacehub", "auth", "login", "--server", "wss://hub.quirq.dev", "--with-token"]
    assert seen["input"] == TOKEN + "\n" and TOKEN not in " ".join(seen["argv"])
    assert "XO_API_KEY" not in seen["env"] and "SPACEHUB_TOKEN" not in seen["env"]
    assert seen["env"]["SPACEHUB_SERVER"] == "wss://hub.quirq.dev"


# What the hub's client prints, from quirq-ai/space-hub main 2c7bdf5: cli/src/cli.ts projectCreate and the
# dispatcher's last line, module/src/index.ts createProjectIn ("spacehub: " + the reducer's message).
EXISTS = 'spacehub: project "alice/{name}" already exists\n'
NO_PROJECT_COMMAND = 'spacehub: unknown command "project"; see spacehub help\n'


def _client_answers(monkeypatch, code: int, stderr: str) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake_run(argv, env, input, **kw):
        import subprocess
        seen.append(argv)
        return subprocess.CompletedProcess(argv, code, "", stderr)
    monkeypatch.setattr(quirq.subprocess, "run", fake_run)
    return seen


@pytest.mark.parametrize("make", [
    lambda hub: quirq.make(hub, "dev", "xo: the space dev, made by xo create space"),
    lambda hub: quirq.create(hub, "juanfont/headscale"),
])
def test_hub_copies_are_projects_in_the_agents_space_never_orgs(monkeypatch, make):
    """`spacehub space create NAME` makes an org since space-hub #5; xo's copies are AGENT/NAME projects,
    where Hub.space_url and Hub.url point."""
    seen = _client_answers(monkeypatch, 0, "")
    hub = quirq.Hub("https://hub.quirq.dev", "alice")
    make(hub)
    [argv] = seen
    assert argv[:3] == ["spacehub", "project", "create"] and argv[4] == "-d"
    assert argv[3] in ("alice/dev", "alice/juanfont--headscale")
    assert hub.space_url(argv[3].split("/")[1]) == f"quirq://hub.quirq.dev/{argv[3]}"
    assert "space" not in argv[:2]


def test_a_hub_copy_already_there_is_left_as_it_is(monkeypatch):
    _client_answers(monkeypatch, 1, EXISTS.format(name="dev"))
    quirq.make(quirq.Hub("https://hub.quirq.dev", "alice"), "dev", "d")


def test_any_other_refusal_stops_with_the_hubs_reason(monkeypatch):
    _client_answers(monkeypatch, 1, 'spacehub: permission denied: only owners and members of "alice" create projects in it\n')
    with pytest.raises(XoError, match="permission denied"):
        quirq.make(quirq.Hub("https://hub.quirq.dev", "alice"), "dev", "d")


def test_a_client_older_than_projects_says_to_update_it(monkeypatch):
    _client_answers(monkeypatch, 1, NO_PROJECT_COMMAND)
    with pytest.raises(XoError, match="older than the hub.*scripts/install-cli.sh"):
        quirq.make(quirq.Hub("https://hub.quirq.dev", "alice"), "dev", "d")


def _second_machine(hub, tmp_path, text):  # noqa: F811
    """Another machine installs dev, commits text to o/a's main and updates: the hub moves on."""
    first = Path.cwd()
    other = tmp_path / "other"
    other.mkdir()
    os.chdir(other)
    workspace.install(hub.client, "space", "dev")
    Path("dev/a/OTHER").write_text(text)
    git("add", "OTHER", cwd="dev/a")
    git("commit", "-q", "-m", text, cwd="dev/a")
    workspace.update(hub.client, "space", "dev")
    os.chdir(first)


def test_sync_never_rewrites_commits_github_already_has(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    _second_machine(hub, tmp_path, "on the hub")
    Path("dev/a/README").write_text("pushed to GitHub\n")
    git("commit", "-q", "-am", "pushed to GitHub", cwd="dev/a")
    git("push", "-q", "origin", "main", cwd="dev/a")  # the user's own push to GitHub
    mine = git("rev-parse", "HEAD", cwd="dev/a")
    with pytest.raises(XoError, match="holds commits GitHub already has, so xo will not rebase them.*git merge quirq/main"):
        workspace.sync(hub.client, "space", "dev")
    assert git("rev-parse", "HEAD", cwd="dev/a") == mine
    git("push", "-q", "origin", "main", cwd="dev/a")  # GitHub still takes the user's pushes


def test_sync_sees_a_push_to_github_made_outside_origin(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    _second_machine(hub, tmp_path, "on the hub")
    Path("dev/a/README").write_text("pushed by URL\n")
    git("commit", "-q", "-am", "pushed by URL", cwd="dev/a")
    url = git("remote", "get-url", "origin", cwd="dev/a")
    git("push", "-q", url, "HEAD:refs/heads/main", cwd="dev/a")  # origin/main here does not move
    mine = git("rev-parse", "HEAD", cwd="dev/a")
    with pytest.raises(XoError, match="holds commits GitHub already has"):
        workspace.sync(hub.client, "space", "dev")
    assert git("rev-parse", "HEAD", cwd="dev/a") == mine


def test_sync_rebases_diverged_work_that_github_does_not_have(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    _second_machine(hub, tmp_path, "on the hub")
    Path("dev/a/README").write_text("mine, only here\n")
    git("commit", "-q", "-am", "mine, only here", cwd="dev/a")
    workspace.sync(hub.client, "space", "dev")
    log = git("log", "--format=%s", "refs/heads/main", cwd=hub.quirq_root / "o--a.git").splitlines()
    assert log[:2] == ["mine, only here", "on the hub"]
    assert git("rev-parse", "HEAD", cwd="dev/a") == git("rev-parse", "refs/heads/main", cwd=hub.quirq_root / "o--a.git")


def test_sync_only_behind_the_hub_fast_forwards(hub, tmp_path):
    workspace.install(hub.client, "space", "dev")
    _second_machine(hub, tmp_path, "on the hub")
    workspace.sync(hub.client, "space", "dev")
    assert git("rev-parse", "HEAD", cwd="dev/a") == git("rev-parse", "refs/heads/main", cwd=hub.quirq_root / "o--a.git")


@pytest.fixture
def folder_hub(xs, github, tmp_path, monkeypatch):  # noqa: F811
    """xo login found the hub as agent alice; its spaces (xo create space) are bare repos."""
    root = tmp_path / "quirqspaces"
    root.mkdir()
    made: list[str] = []
    monkeypatch.setattr(quirq, "installed", lambda: True)
    monkeypatch.setattr(quirq.Hub, "space_url", lambda self, space: f"file://{root}/{space}.git")

    def make(hub, space, description):
        made.append(space)
        git("init", "-q", "--bare", str(root / f"{space}.git"))
    monkeypatch.setattr(quirq, "make", make)
    quirq.save(quirq.Hub("https://hub.quirq.dev", "alice"))
    asked: list[str] = []

    def typed(question):
        asked.append(question)
        return xs.answer
    monkeypatch.setattr(cli, "_typed", typed)
    xs.answer, xs.asked = "dev", asked  # dev/notes has no origin: its name is typed
    xs.route("POST", "/me/spaces/connect", body={"success": True, "data": {"space": {
        "name": "dev", "kind": None, "id": None, "rev": 1, "projects": []}}})
    git("init", "-q", "dev/notes")
    Path("dev/notes/n.txt").write_text("n\n")
    xs.made, xs.root, xs.github = made, root, github
    return xs


def _space_hub(h, *args):
    return git(f"--git-dir={h.root / 'dev.git'}", *args)


def test_create_space_puts_a_folder_on_the_hub_and_never_on_github(folder_hub, capsys, monkeypatch):
    code, out, err = run(capsys, "create", "space", "dev", "--yes")
    made = json.loads(out)
    assert code == 0 and made["pushed"] is True and made["hub"].endswith("/dev.git") and "github" not in made
    assert "a space on the quirq hub's space alice/dev" in err and "Anyone can read what is pushed to the hub" in err
    assert folder_hub.made == ["dev"]
    assert _space_hub(folder_hub, "show", "notes:n.txt") == "n"
    record = json.loads(_space_hub(folder_hub, "show", f"main:{spacerepo.FILE}"))
    assert record["hub"] == made["hub"] and "github" not in record
    assert "--owner" not in _space_hub(folder_hub, "show", "main:update")
    assert git("remote", cwd="dev").split() == ["quirq"]  # no origin: GitHub never saw it
    assert not (folder_hub.github.root / "me").exists()  # nothing on the stand-in GitHub
    assert [(r["method"], r["path"]) for r in folder_hub.requests] == [("POST", "/me/spaces/connect")]

    Path("dev/notes/n.txt").write_text("n two\n")
    monkeypatch.chdir("dev")
    code, out, _ = run(capsys, "create", "space", "--yes")
    assert code == 0 and folder_hub.made == ["dev"]  # made once; the second run goes on top, never forced
    assert _space_hub(folder_hub, "show", "notes:n.txt") == "n two"
    assert _space_hub(folder_hub, "rev-parse", "notes~1") == made["branches"]["notes"]


def test_create_space_with_owner_or_github_puts_the_folder_on_github(folder_hub, capsys, monkeypatch):
    git("init", "-q", "--bare", str(folder_hub.github.root / "me/dev"))
    code, out, _ = run(capsys, "create", "space", "dev", "--owner", "me", "--yes")
    assert code == 0 and json.loads(out)["github"] == "me/dev" and folder_hub.made == []
    assert git(f"--git-dir={folder_hub.github.root / 'me/dev'}", "show", "notes:n.txt") == "n"
    # A space on GitHub is updated there only when asked: without --github, xo stops and says so.
    monkeypatch.chdir("dev")
    code, out, err = run(capsys, "create", "space", "--yes")
    assert code == 1 and "space dev is on github.com/me/dev" in err and "--github" in err and folder_hub.made == []
    code, out, _ = run(capsys, "create", "space", "--github", "--yes")
    assert code == 0 and json.loads(out)["github"] == "me/dev" and folder_hub.made == []


def test_a_hub_space_run_with_github_moves_to_github_only_when_asked(folder_hub, capsys):
    code, out, _ = run(capsys, "create", "space", "dev", "--yes")
    assert code == 0 and "hub" in json.loads(out) and folder_hub.made == ["dev"]
    git("init", "-q", "--bare", str(folder_hub.github.root / "me/dev"))
    code, out, _ = run(capsys, "create", "space", "dev", "--github", "--owner", "me", "--yes")
    assert code == 0 and json.loads(out)["github"] == "me/dev" and folder_hub.made == ["dev"]
    assert git(f"--git-dir={folder_hub.github.root / 'me/dev'}", "show", "notes:n.txt") == "n"


def test_a_hub_space_name_with_two_hyphens_is_refused(folder_hub, capsys):
    git("init", "-q", "o--r/notes")
    code, _, err = run(capsys, "create", "space", "o--r", "--yes")
    assert code == 1 and "copies of GitHub projects" in err and folder_hub.made == []


def test_a_hub_space_with_commits_from_elsewhere_is_not_pushed_over(folder_hub, capsys, tmp_path):
    git("init", "-q", "--bare", str(folder_hub.root / "dev.git"))
    other = tmp_path / "other"
    git("init", "-q", "-b", "main", str(other))
    (other / "x").write_text("x\n")
    git("add", ".", cwd=str(other))
    git("commit", "-qm", "x", cwd=str(other))
    git("push", "-q", str(folder_hub.root / "dev.git"), "main", cwd=str(other))
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "has commits xo did not make here" in err and folder_hub.made == []
    assert _space_hub(folder_hub, "for-each-ref", "--format=%(refname:short)").split() == ["main"]


def test_create_space_needs_the_hubs_client_once_xo_login_found_a_hub(folder_hub, capsys, monkeypatch):
    monkeypatch.setattr(quirq, "installed", lambda: False)
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "git-remote-quirq is not installed" in err and not Path("dev/.git").exists()


def test_a_repository_with_no_origin_goes_to_the_hub_only_when_its_name_is_typed(folder_hub, capsys):
    folder_hub.answer = None  # no terminal: --yes does not answer it
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "notes (no origin)" in err and "--yes does not answer this" in err
    assert "nothing was pushed or kept" in err and folder_hub.made == [] and not list(folder_hub.root.iterdir())
    assert not Path("dev/.git").exists() and Path("dev/notes/n.txt").read_text() == "n\n"  # as it was
    folder_hub.answer = "de"
    code, _, _ = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and folder_hub.made == []
    assert folder_hub.asked == ["Type dev to send them to the hub anyway"] * 2


def _public(folder_hub):
    """dev/notes committed and pushed to a public o/pub on the stand-in GitHub."""
    git("init", "-q", "--bare", str(folder_hub.github.root / "o/pub"))
    git("remote", "add", "origin", f"{gitops.GITHUB}/o/pub", cwd="dev/notes")
    git("add", ".", cwd="dev/notes")
    git("commit", "-qm", "n", cwd="dev/notes")
    git("-c", "protocol.file.allow=always", "push", "-q", "origin", "HEAD:refs/heads/main", cwd="dev/notes")
    git("fetch", "-q", "origin", cwd="dev/notes")


def test_a_repository_public_on_github_goes_to_the_hub_without_typing(folder_hub, capsys):
    _public(folder_hub)
    code, out, _ = run(capsys, "create", "space", "dev", "--yes")
    assert code == 0 and json.loads(out)["pushed"] is True and folder_hub.asked == []
    assert _space_hub(folder_hub, "show", "notes:n.txt") == "n"


@pytest.mark.parametrize("change", ["untracked", "edited", "commit", "assume-unchanged", "skip-worktree",
                                    "excludes-file"])
def test_files_github_does_not_have_need_the_name_typed_even_with_a_public_origin(folder_hub, capsys, change):
    _public(folder_hub)
    if change == "untracked":
        Path("dev/notes/private.txt").write_text("mine\n")
    elif change == "edited":
        Path("dev/notes/n.txt").write_text("not pushed\n")
    elif change == "commit":
        Path("dev/notes/n.txt").write_text("committed, not pushed\n")
        git("commit", "-qam", "local", cwd="dev/notes")
    elif change in ("assume-unchanged", "skip-worktree"):  # git status no longer shows the edit
        git("update-index", f"--{change}", "n.txt", cwd="dev/notes")
        Path("dev/notes/n.txt").write_text("SECRET\n")
        assert git("status", "--porcelain", cwd="dev/notes") == ""
    else:  # an excludes file only the repository's own config names hides it from git status there
        Path("dev/hide").write_text("secret.txt\n")
        git("config", "core.excludesFile", str(Path("dev/hide").absolute()), cwd="dev/notes")
        Path("dev/notes/secret.txt").write_text("SECRET\n")
        assert git("status", "--porcelain", cwd="dev/notes") == ""
    folder_hub.answer = None
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "notes (files GitHub does not have)" in err and folder_hub.made == []
    assert not list(folder_hub.root.iterdir())  # nothing reached the hub


def test_a_stale_or_forged_origin_ref_does_not_count_as_github_having_a_commit(folder_hub, capsys):
    _public(folder_hub)
    Path("dev/notes/secret.txt").write_text("SECRET\n")
    git("add", "secret.txt", cwd="dev/notes")
    git("commit", "-qm", "never pushed", cwd="dev/notes")
    git("update-ref", "refs/remotes/origin/old-private", "HEAD", cwd="dev/notes")
    folder_hub.answer = None
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "notes (files GitHub does not have)" in err and not list(folder_hub.root.iterdir())


def test_an_insteadof_rewrite_does_not_make_a_private_origin_read_as_public(folder_hub, capsys):
    git("init", "-q", "--bare", str(folder_hub.github.root / "o/pub"))
    git("remote", "add", "origin", "https://gitlab.example/o/secret", cwd="dev/notes")
    git("config", f"url.{gitops.GITHUB}/o/pub.insteadOf", "https://gitlab.example/o/secret", cwd="dev/notes")
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "not a public GitHub repository" in err and folder_hub.made == []


@pytest.mark.parametrize("origin", ["o/private", "https://gitlab.example/o/r"])
def test_a_repository_whose_origin_is_not_public_on_github_is_never_sent_to_the_hub(folder_hub, capsys, origin):
    url = origin if "://" in origin else f"{gitops.GITHUB}/{origin}"  # o/private: GitHub does not answer
    git("remote", "add", "origin", url, cwd="dev/notes")
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "not a public GitHub repository" in err and "notes" in err and url not in err
    assert folder_hub.made == [] and folder_hub.asked == [] and not Path("dev/.git").exists()


def test_a_folder_whose_own_origin_is_private_is_never_sent_to_the_hub(folder_hub, capsys):
    git("init", "-q", "-b", "work", "dev")
    git("remote", "add", "origin", f"{gitops.GITHUB}/o/private", cwd="dev")
    code, _, err = run(capsys, "create", "space", "dev", "--yes")
    assert code == 1 and "dev/.git has an origin that is not a public GitHub repository" in err
    assert folder_hub.made == [] and "xo-space" not in git("branch", "--list", cwd="dev")


def test_an_attached_folder_goes_to_the_hub_by_url_with_its_remotes_untouched(folder_hub, capsys):
    git("init", "-q", "-b", "work", "dev")
    code, out, _ = run(capsys, "create", "space", "dev", "--yes")
    assert code == 0 and json.loads(out)["index"] == "xo-space"
    assert git("remote", cwd="dev") == ""  # no quirq remote added to your repository
    assert _space_hub(folder_hub, "show", "notes:n.txt") == "n"


def test_your_remote_for_the_hubs_space_keeps_its_tracking_refs(folder_hub, capsys):
    # From git 2.56, a push to a URL that is also a remote's moves that remote's refs, as a push to it does.
    git("init", "-q", "-b", "work", "dev")
    git("commit", "-q", "--allow-empty", "-m", "mine", cwd="dev")
    git("remote", "add", "mine", f"file://{folder_hub.root}/dev.git", cwd="dev")
    head = git("rev-parse", "HEAD", cwd="dev")
    git("update-ref", "refs/remotes/mine/work", head, cwd="dev")
    code, _, _ = run(capsys, "create", "space", "dev", "--yes")
    assert code == 0 and _space_hub(folder_hub, "show", "notes:n.txt") == "n"
    assert git("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/", cwd="dev") == f"refs/remotes/mine/work {head}"


def test_a_quirq_remote_that_points_elsewhere_is_refused(folder_hub, capsys, monkeypatch):
    assert run(capsys, "create", "space", "dev", "--yes")[0] == 0
    git("remote", "set-url", "quirq", "file:///elsewhere.git", cwd="dev")
    Path("dev/notes/n.txt").write_text("n two\n")
    monkeypatch.chdir("dev")
    code, _, err = run(capsys, "create", "space", "--yes")
    assert code == 1 and "has a quirq remote that is not" in err and "git remote remove quirq" in err
