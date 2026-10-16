"""xo install, update and sync against the real server code (with a fake Clerk) and local git
repositories standing in for GitHub."""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from conftest import git
from test_server import USER_KEY, _Core, _space, _Store
from xo import cli, server, workspace
from xo.client import XoError


class _Client:
    """The CLI's client, answered by an in-process xo server."""

    def __init__(self, app: server.App) -> None:
        self.app = app

    def request(self, method, path, body=None):
        status, payload = self.app.handle(method, path, {"authorization": f"Bearer {USER_KEY}"},
                                          json.dumps(body).encode() if body is not None else None)
        if status >= 400:
            raise XoError(payload.get("error", str(status)))
        return types.SimpleNamespace(data=payload)

    def scrub(self, text):
        return text

    def url(self, path, query=None):
        return path


@pytest.fixture
def space(github, monkeypatch):
    """Two GitHub projects, o/a at main and o/b at tag v1, in space dev (rev 1)."""
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_clerk_secret_never_printed")
    github.repo("o/a")
    github.repo("o/b")
    store = _Store({"projects": ["o/a", "o/b", "notes"],
                    "spaces": [_space("dev", projects=[{"o/a": "main"}, {"o/b": "v1"}, {"notes": "main"}])]})
    return types.SimpleNamespace(hub=github, store=store, client=_Client(server.App(
        server.Config(), clerk=store, core=_Core(), audit=lambda line: None)))


def _stored(space):
    return space.store.stored["private_metadata"]["spaces"][0]


def test_install_puts_each_project_at_its_ref_and_remembers_the_rev(space):
    out = workspace.install(space.client, "space", "dev")
    assert out["rev"] == 1
    assert git("branch", "--show-current", cwd="dev/o/a") == "main"
    assert git("branch", "--show-current", cwd="dev/o/b") == ""  # a tag: detached
    assert git("describe", "--tags", cwd="dev/o/b") == "v1"
    assert not Path("dev/notes").exists()  # no GitHub remote: left alone
    assert json.loads(Path("dev/.xo/state.json").read_text())["rev"] == 1


def test_update_records_the_branch_checked_out_and_counts_the_rev(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    git("fetch", "-q", cwd="dev/o/a")
    git("switch", "-q", "feature", cwd="dev/o/a")
    out = workspace.update(space.client, "space", "dev")
    assert out["rev"] == 2 and _stored(space)["projects"] == [{"o/a": "feature"}, {"o/b": "v1"}, {"notes": "main"}]
    assert json.loads(Path("dev/.xo/state.json").read_text())["rev"] == 2


def test_update_refuses_a_branch_its_remote_does_not_have(space):
    workspace.install(space.client, "space", "dev")
    git("switch", "-q", "-c", "local-only", cwd="dev/o/a")
    with pytest.raises(XoError, match="its remote does not have; push it first"):
        workspace.update(space.client, "space", "dev")
    assert space.store.patches == []


def test_update_is_refused_when_the_space_moved_on(space):
    workspace.install(space.client, "space", "dev")
    _stored(space)["rev"] = 5
    with pytest.raises(XoError, match="run xo sync space:dev"):
        workspace.update(space.client, "space", "dev")


def test_sync_takes_their_changes_and_keeps_yours(space):
    space.hub.commit("o/a", "feature", "mine")
    space.hub.commit("o/b", "main", "theirs", tag="v2")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=4, projects=[{"o/a": "main"}, {"o/b": "v2"}, {"notes": "main"}])  # someone else
    git("fetch", "-q", cwd="dev/o/a")
    git("switch", "-q", "feature", cwd="dev/o/a")
    out = workspace.sync(space.client, "space", "dev")
    assert out["projects"] == [{"o/a": "feature"}, {"o/b": "v2"}, {"notes": "main"}] and out["rev"] == 5
    assert _stored(space)["projects"] == out["projects"]
    assert git("describe", "--tags", cwd="dev/o/b") == "v2"


def test_sync_stops_when_both_changed_one_project(space):
    space.hub.commit("o/a", "feature", "mine")
    space.hub.commit("o/a", "other", "theirs")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=4, projects=[{"o/a": "other"}, {"o/b": "v1"}, {"notes": "main"}])
    git("fetch", "-q", cwd="dev/o/a")
    git("switch", "-q", "feature", cwd="dev/o/a")
    with pytest.raises(XoError, match="both you and the space changed o/a"):
        workspace.sync(space.client, "space", "dev")
    assert space.store.patches == [] and git("branch", "--show-current", cwd="dev/o/a") == "feature"


def test_sync_with_nothing_changed_writes_nothing(space):
    workspace.install(space.client, "space", "dev")
    assert workspace.sync(space.client, "space", "dev")["rev"] == 1 and space.store.patches == []


def test_install_refuses_a_checkout_with_uncommitted_changes(space):
    workspace.install(space.client, "space", "dev")
    Path("dev/o/a/README").write_text("edited\n")
    with pytest.raises(XoError, match="not committed"):
        workspace.install(space.client, "space", "dev")


@pytest.mark.parametrize("entry", [{"o/a": "--upload-pack=touch /tmp/x"}, {"../x": "main"}, {"o/a": "a..b"},
                                   {"o/.git": "main"}])
def test_install_checks_names_and_refs_from_metadata_again(space, entry):
    _stored(space)["projects"] = [entry]
    with pytest.raises(XoError, match="is not a"):
        workspace.install(space.client, "space", "dev")
    assert not Path("dev").exists()


def test_project_update_pushes_and_sync_rebases_first(space):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    workspace.install(space.client, "project", "o/a")
    Path("o/a/README").write_text("pushed\n")
    git("commit", "-q", "-am", "pushed", cwd="o/a")
    workspace.update(None, "project", "o/a")
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") == "pushed"
    space.hub.seeds.joinpath("o/a")  # someone else pushes on top
    git("pull", "-q", "origin", "main", cwd=space.hub.seeds / "o/a")
    space.hub.commit("o/a", "main", "theirs")
    Path("o/a/NEW").write_text("x")
    git("add", "NEW", cwd="o/a")
    git("commit", "-q", "-m", "mine", cwd="o/a")
    with pytest.raises(XoError, match="run xo sync"):
        workspace.update(None, "project", "o/a")
    workspace.sync(None, "project", "o/a")
    assert git("log", "-2", "--format=%s", "main", cwd=space.hub.root / "o/a").split("\n") == ["mine", "theirs"]


def test_project_update_refuses_a_detached_or_dirty_checkout(space):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    workspace.install(space.client, "project", "o/a")
    Path("o/a/README").write_text("dirty\n")
    with pytest.raises(XoError, match="not committed"):
        workspace.update(None, "project", "o/a")
    git("checkout", "-q", "README", cwd="o/a")
    git("switch", "-q", "--detach", cwd="o/a")
    with pytest.raises(XoError, match="detached"):
        workspace.update(None, "project", "o/a")


@pytest.mark.parametrize("text", ["space:../x", "project:-a", "dev", "space:", "repo:o/a"])
def test_targets_are_checked(text):
    with pytest.raises(XoError):
        workspace.target(text)


def test_the_commands_are_wired(space, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda args, **kw: space.client)
    assert cli.main(["install", "space:dev"]) == 0
    assert json.loads(capsys.readouterr().out)["rev"] == 1
    assert cli.main(["sync", "space:dev"]) == 0 and cli.main(["update", "space:dev"]) == 0
