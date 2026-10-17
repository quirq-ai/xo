"""xo install, update and sync against the real server code (with a fake Clerk) and local git
repositories standing in for GitHub."""
from __future__ import annotations

import json
import shutil
import types
from pathlib import Path

import pytest

from conftest import git
from test_server import USER_KEY, _Core, _space, _Store
from xo import cli, names, server, workspace
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
    assert git("branch", "--show-current", cwd="dev/a") == "main"
    assert git("branch", "--show-current", cwd="dev/b") == ""  # a tag: detached
    assert git("describe", "--tags", cwd="dev/b") == "v1"
    assert not Path("dev/notes").exists()  # no GitHub remote: left alone
    assert json.loads(Path("dev/.xo/state.json").read_text())["rev"] == 1


def test_update_records_the_branch_checked_out_and_counts_the_rev(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    out = workspace.update(space.client, "space", "dev")
    assert out["rev"] == 2 and _stored(space)["projects"] == [{"o/a": "feature"}, {"o/b": "v1"}, {"notes": "main"}]
    assert json.loads(Path("dev/.xo/state.json").read_text())["rev"] == 2


def test_update_refuses_a_branch_its_remote_does_not_have(space):
    workspace.install(space.client, "space", "dev")
    git("switch", "-q", "-c", "local-only", cwd="dev/a")
    with pytest.raises(XoError, match="its remote does not have; push it first"):
        workspace.update(space.client, "space", "dev")
    assert space.store.patches == []


def test_update_is_refused_when_the_space_moved_on(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    _stored(space)["rev"] = 5
    with pytest.raises(XoError, match="run xo sync space:dev"):
        workspace.update(space.client, "space", "dev")


def test_sync_takes_their_changes_and_keeps_yours(space):
    space.hub.commit("o/a", "feature", "mine")
    space.hub.commit("o/b", "main", "theirs", tag="v2")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=4, projects=[{"o/a": "main"}, {"o/b": "v2"}, {"notes": "main"}])  # someone else
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    out = workspace.sync(space.client, "space", "dev")
    assert out["projects"] == [{"o/a": "feature"}, {"o/b": "v2"}, {"notes": "main"}] and out["rev"] == 5
    assert _stored(space)["projects"] == out["projects"]
    assert git("describe", "--tags", cwd="dev/b") == "v2"


def test_sync_stops_when_both_changed_one_project(space):
    space.hub.commit("o/a", "feature", "mine")
    space.hub.commit("o/a", "other", "theirs")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=4, projects=[{"o/a": "other"}, {"o/b": "v1"}, {"notes": "main"}])
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    with pytest.raises(XoError, match="both you and the space changed o/a.*./dev/a were rebased and pushed"):
        workspace.sync(space.client, "space", "dev")
    assert space.store.patches == [] and git("branch", "--show-current", cwd="dev/a") == "feature"


def test_a_sync_that_pushed_nothing_does_not_say_it_did(space):
    space.hub.commit("o/b", "main", "v2 work", tag="v2")
    space.hub.commit("o/b", "main", "v3 work", tag="v3")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=4, projects=[{"o/a": "main"}, {"o/b": "v3"}, {"notes": "main"}])
    git("switch", "-q", "--detach", cwd="dev/a")
    git("fetch", "-q", "--tags", cwd="dev/b")
    git("switch", "-q", "--detach", "v2", cwd="dev/b")
    with pytest.raises(XoError, match="both you and the space changed o/b") as stopped:
        workspace.sync(space.client, "space", "dev")
    assert "pushed" not in str(stopped.value) and "No checkout was moved" in str(stopped.value)


def test_sync_with_nothing_changed_writes_nothing(space):
    workspace.install(space.client, "space", "dev")
    assert workspace.sync(space.client, "space", "dev")["rev"] == 1 and space.store.patches == []


def test_install_refuses_a_checkout_with_uncommitted_changes(space):
    workspace.install(space.client, "space", "dev")
    Path("dev/a/README").write_text("edited\n")
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
    Path("a/README").write_text("pushed\n")
    git("commit", "-q", "-am", "pushed", cwd="a")
    workspace.update(space.client, "project", "o/a")
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") == "pushed"
    space.hub.seeds.joinpath("o/a")  # someone else pushes on top
    git("pull", "-q", "origin", "main", cwd=space.hub.seeds / "o/a")
    space.hub.commit("o/a", "main", "theirs")
    Path("a/NEW").write_text("x")
    git("add", "NEW", cwd="a")
    git("commit", "-q", "-m", "mine", cwd="a")
    with pytest.raises(XoError, match="run xo sync"):
        workspace.update(space.client, "project", "o/a")
    workspace.sync(space.client, "project", "o/a")
    assert git("log", "-2", "--format=%s", "main", cwd=space.hub.root / "o/a").split("\n") == ["mine", "theirs"]


def test_project_update_refuses_a_detached_or_dirty_checkout(space):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    workspace.install(space.client, "project", "o/a")
    Path("a/README").write_text("dirty\n")
    with pytest.raises(XoError, match="not committed"):
        workspace.update(space.client, "project", "o/a")
    git("checkout", "-q", "README", cwd="a")
    git("switch", "-q", "--detach", cwd="a")
    with pytest.raises(XoError, match="detached"):
        workspace.update(space.client, "project", "o/a")


def test_project_update_and_sync_refuse_a_folder_that_is_not_a_checkout_of_its_own(space, tmp_path):
    git("init", "-q", "-b", "main")  # the working folder is itself a repository
    Path("a").mkdir(parents=True)
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    for verb in (workspace.update, workspace.sync):
        with pytest.raises(XoError, match=r"o/a has no checkout here \(looked in ./a and ./o/a\); xo install"):
            verb(space.client, "project", "o/a")
        with pytest.raises(XoError, match="o/missing is not in your projects"):
            verb(space.client, "project", "o/missing")
    (Path("a") / ".git").write_text("gitdir: ../.git\n")  # looks like a checkout, but not of o/a
    with pytest.raises(XoError, match="./a is not a checkout of o/a"):
        workspace.update(space.client, "project", "o/a")


def test_install_project_fast_forwards_an_existing_checkout(space, capsys):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    workspace.install(space.client, "project", "o/a")
    space.hub.commit("o/a", "main", "newer")
    workspace.install(space.client, "project", "o/a")
    assert Path("a/README").read_text() == "newer" and "up to date with its upstream" in capsys.readouterr().err


def test_install_project_takes_its_repo_name_and_asks_when_two_share_it(space, capsys):
    space.store.stored["private_metadata"]["projects"] = ["o/a", "notes"]
    assert workspace.install(space.client, "project", "A") == {"project": "o/a"}
    assert Path("a/README").exists() and not Path("o").exists()
    space.store.stored["private_metadata"]["projects"] = ["o/a", "p/a"]
    with pytest.raises(XoError, match="a could be o/a or p/a; say which with its OWNER/REPO"):
        workspace.install(space.client, "project", "a")


def test_a_space_with_a_fork_and_its_upstream_keeps_both_by_owner(space):
    space.hub.repo("p/A")
    space.store.stored["private_metadata"]["projects"] = ["o/a", "p/A", "o/b"]
    space.store.stored["private_metadata"]["spaces"] = [_space("dev", projects=[
        {"o/a": "main"}, {"p/A": "main"}, {"o/b": "v1"}])]
    workspace.install(space.client, "space", "dev")
    assert git("remote", "get-url", "origin", cwd="dev/o/a").endswith("/o/a")
    assert git("remote", "get-url", "origin", cwd="dev/p/A").endswith("/p/A")
    assert Path("dev/b/README").exists() and not Path("dev/a").exists()
    git("switch", "-q", "-c", "feature", cwd="dev/p/A")
    git("push", "-q", "-u", "origin", "feature", cwd="dev/p/A")
    assert workspace.update(space.client, "space", "dev")["rev"] == 2
    assert _stored(space)["projects"][1] == {"p/A": "feature"}
    assert workspace.sync(space.client, "space", "dev")["rev"] == 2


@pytest.mark.parametrize("projects, folders", [
    (["suraj/react", "facebook/react", "suraj/suraj"], ["suraj/react", "facebook/react", "suraj/suraj"]),
    (["supabase/cli", "suraj/cli", "supabase/supabase", "o/b"], ["supabase/cli", "suraj/cli", "supabase/supabase", "b"]),
    (["a/b", "b/c", "x/c", "c/d"], ["a/b", "b/c", "x/c", "d"]),  # b/c's owner b pulls a/b under a/ too
    (["juanfont/headscale", "o/b"], ["headscale", "b"]),
])
def test_no_project_folder_is_ever_inside_another(projects, folders):
    for order in (projects, projects[::-1]):
        got = [names.folder(p, order) for p in projects]
        assert got == folders
        tops = {f.casefold() for f in got if "/" not in f}
        assert not any(f.partition("/")[0].casefold() in tops for f in got if "/" in f)


@pytest.mark.parametrize("order", [0, 1])
def test_a_space_whose_owner_is_also_a_repo_name_installs_every_project_side_by_side(space, order):
    for repo in ("suraj/react", "facebook/react", "suraj/suraj"):
        space.hub.repo(repo)
    entries = [{"suraj/react": "main"}, {"facebook/react": "main"}, {"suraj/suraj": "main"}]
    entries = entries if order == 0 else entries[::-1]
    space.store.stored["private_metadata"]["projects"] = [p for e in entries for p in e]
    space.store.stored["private_metadata"]["spaces"] = [_space("dev", projects=entries)]
    workspace.install(space.client, "space", "dev")
    for repo in ("suraj/react", "facebook/react", "suraj/suraj"):
        assert git("remote", "get-url", "origin", cwd=f"dev/{repo}").endswith(f"/{repo}")
        assert git("status", "--porcelain", cwd=f"dev/{repo}") == ""
    assert workspace.update(space.client, "space", "dev")["rev"] == 1


def test_a_project_is_never_cloned_inside_another_checkout(space):
    for repo in ("suraj/react", "facebook/react", "suraj/suraj"):
        space.hub.repo(repo)
    git("clone", "-q", f"file://{space.hub.root}/suraj/suraj", "dev/suraj")  # before the two reacts came
    entries = [{"suraj/suraj": "main"}, {"suraj/react": "main"}, {"facebook/react": "main"}]
    space.store.stored["private_metadata"]["spaces"] = [_space("dev", projects=entries)]
    with pytest.raises(XoError, match=r"./dev/suraj is a git checkout, and suraj/suraj goes in ./dev/suraj/suraj"):
        workspace.install(space.client, "space", "dev")
    assert git("status", "--porcelain", cwd="dev/suraj") == ""


def test_a_plain_project_never_pushes_a_github_projects_checkout(space):
    git("clone", "-q", f"file://{space.hub.root}/o/a", "a")
    Path("a/NEW").write_text("x")
    git("add", "NEW", cwd="a")
    git("commit", "-q", "-m", "mine", cwd="a")
    space.store.stored["private_metadata"]["projects"] = ["o/a", "a"]  # the plain a added later
    for verb in (workspace.update, workspace.sync):
        with pytest.raises(XoError, match="./a is a checkout of o/a, not of a"):
            verb(space.client, "project", "a")
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") != "mine"
    assert workspace.update(space.client, "project", "o/a") == {"project": "o/a"}
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") == "mine"


def test_install_never_fetches_another_repo_that_has_the_same_folder(space):
    space.hub.repo("p/a")
    git("clone", "-q", f"file://{space.hub.root}/p/a", "a")
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    with pytest.raises(XoError, match="./a is not a checkout of o/a"):
        workspace.install(space.client, "project", "a")


def test_project_update_and_sync_find_a_checkout_by_short_name_or_where_an_older_xo_put_it(space):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    git("clone", "-q", f"file://{space.hub.root}/o/a", "o/a")  # an older xo's layout
    Path("o/a/NEW").write_text("x")
    git("add", "NEW", cwd="o/a")
    git("commit", "-q", "-m", "mine", cwd="o/a")
    assert workspace.update(space.client, "project", "a") == {"project": "o/a"}
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") == "mine"
    assert workspace.sync(space.client, "project", "A") == {"project": "o/a"}
    assert workspace.install(space.client, "project", "a") == {"project": "o/a"} and not Path("a").exists()


def test_project_update_never_pushes_another_repos_checkout(space):
    space.hub.repo("p/a")
    space.store.stored["private_metadata"]["projects"] = ["o/a", "p/a"]
    git("clone", "-q", f"file://{space.hub.root}/o/a", "a")  # o/a, where p/a's short name would put it
    for verb in (workspace.update, workspace.sync):
        with pytest.raises(XoError, match="p/a has no checkout here"):
            verb(space.client, "project", "p/a")
        with pytest.raises(XoError, match="a could be o/a or p/a"):
            verb(space.client, "project", "a")
    space.store.stored["private_metadata"]["projects"] = ["p/a"]
    with pytest.raises(XoError, match="./a is not a checkout of p/a"):
        workspace.update(space.client, "project", "p/a")


def test_a_repo_named_dot_xo_never_lands_on_the_spaces_own_folder(space, tmp_path):
    space.hub.repo("evil/.xo")
    seed = space.hub.seeds / "evil/.xo"
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    (seed / "state.tmp").symlink_to(victim)
    git("add", "state.tmp", cwd=seed)
    git("commit", "-q", "-m", "link", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    space.store.stored["private_metadata"]["spaces"] = [_space("dev", projects=[{"evil/.xo": "main"}])]
    workspace.install(space.client, "space", "dev")
    assert Path("dev/evil/.xo/state.tmp").is_symlink() and victim.read_text() == "untouched"
    assert json.loads(Path("dev/.xo/state.json").read_text())["projects"] == [{"evil/.xo": "main"}]


def test_the_state_file_is_never_written_through_a_link(space, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    Path("dev/.xo").mkdir(parents=True)
    Path("dev/.xo/state.tmp").symlink_to(victim)
    workspace.install(space.client, "space", "dev")
    assert victim.read_text() == "untouched" and json.loads(Path("dev/.xo/state.json").read_text())["rev"] == 1
    shutil.rmtree("dev/.xo")
    Path("dev/.xo").symlink_to(tmp_path)
    with pytest.raises(XoError, match="not xo's own folder"):
        workspace.install(space.client, "space", "dev")


def test_a_checkout_an_older_xo_put_in_owner_repo_is_still_used(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    Path("dev/o").mkdir()
    Path("dev/a").rename("dev/o/a")  # where xo before short names put it
    git("fetch", "-q", cwd="dev/o/a")
    git("switch", "-q", "feature", cwd="dev/o/a")
    assert workspace.update(space.client, "space", "dev")["rev"] == 2
    assert _stored(space)["projects"][0] == {"o/a": "feature"} and not Path("dev/a").exists()


def test_project_sync_that_conflicts_leaves_the_checkout_as_it_was(space):
    space.store.stored["private_metadata"]["projects"] = ["o/a"]
    workspace.install(space.client, "project", "o/a")
    space.hub.commit("o/a", "main", "theirs")
    Path("a/README").write_text("mine\n")
    git("commit", "-q", "-am", "mine", cwd="a")
    with pytest.raises(XoError, match="left it as it was"):
        workspace.sync(space.client, "project", "o/a")
    assert git("branch", "--show-current", cwd="a") == "main" and Path("a/README").read_text() == "mine\n"


def test_space_update_pushes_each_branch_and_records_nothing_when_refs_are_unchanged(space):
    workspace.install(space.client, "space", "dev")
    Path("dev/a/README").write_text("pushed\n")
    git("commit", "-q", "-am", "pushed", cwd="dev/a")
    out = workspace.update(space.client, "space", "dev")
    assert git("log", "-1", "--format=%s", "main", cwd=space.hub.root / "o/a") == "pushed"
    assert out["rev"] == 1 and space.store.patches == []


def test_space_sync_rebases_each_branch_on_its_upstream_and_pushes_it(space):
    workspace.install(space.client, "space", "dev")
    space.hub.commit("o/a", "main", "theirs")
    Path("dev/a/NEW").write_text("x")
    git("add", "NEW", cwd="dev/a")
    git("commit", "-q", "-m", "mine", cwd="dev/a")
    workspace.sync(space.client, "space", "dev")
    assert git("log", "-2", "--format=%s", "main", cwd=space.hub.root / "o/a").split("\n") == ["mine", "theirs"]


def test_sync_stops_when_the_space_went_back_to_an_older_rev(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    assert workspace.update(space.client, "space", "dev")["rev"] == 2
    # A stale writer put rev 1 back, and someone may have recorded on top of it: neither side wins.
    _stored(space).update(rev=1, projects=[{"o/a": "main"}, {"o/b": "v1"}, {"notes": "main"}])
    patches = len(space.store.patches)
    with pytest.raises(XoError, match="went back to rev 1 from your rev 2.*o/a.*./dev/a were rebased and pushed.*nothing was recorded"):
        workspace.sync(space.client, "space", "dev")
    assert git("branch", "--show-current", cwd="dev/a") == "feature" and len(space.store.patches) == patches


def test_a_sync_whose_record_is_refused_never_undoes_the_newer_write_next_time(space, monkeypatch):
    space.hub.commit("o/a", "feature", "feature work")
    space.hub.commit("o/b", "main", "v2 work", tag="v2")
    workspace.install(space.client, "space", "dev")
    _stored(space).update(rev=2, projects=[{"o/a": "feature"}, {"o/b": "v1"}, {"notes": "main"}])
    git("switch", "-q", "--detach", "v2", cwd="dev/b")
    real = workspace._record

    def racing(client, name, rev, pairs, base):  # someone records o/a back at main first
        _stored(space).update(rev=3, projects=[{"o/a": "main"}, {"o/b": "v1"}, {"notes": "main"}])
        return real(client, name, rev, pairs, base)

    monkeypatch.setattr(workspace, "_record", racing)
    with pytest.raises(XoError, match="run xo sync"):
        workspace.sync(space.client, "space", "dev")
    monkeypatch.setattr(workspace, "_record", real)
    out = workspace.sync(space.client, "space", "dev")
    assert out["projects"][0] == {"o/a": "main"}  # the rev-3 write stands


def test_sync_stops_when_the_space_dropped_a_project_you_changed(space):
    space.hub.commit("o/a", "feature", "feature work")
    workspace.install(space.client, "space", "dev")
    git("fetch", "-q", cwd="dev/a")
    git("switch", "-q", "feature", cwd="dev/a")
    _stored(space).update(rev=2, projects=[{"o/b": "v1"}, {"notes": "main"}])
    with pytest.raises(XoError, match="removed from the space"):
        workspace.sync(space.client, "space", "dev")


def test_update_names_the_ref_rule_for_a_branch_xo_cannot_record(space):
    workspace.install(space.client, "space", "dev")
    git("switch", "-q", "-c", "fix#1", cwd="dev/a")
    with pytest.raises(XoError, match="not a ref xo can record"):
        workspace.update(space.client, "space", "dev")


@pytest.mark.parametrize("text", ["space:../x", "project:-a", "dev", "space:", "repo:o/a"])
def test_targets_are_checked(text):
    with pytest.raises(XoError):
        workspace.target(text)


def test_the_commands_are_wired(space, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda args, **kw: space.client)
    assert cli.main(["install", "space:dev"]) == 0
    assert json.loads(capsys.readouterr().out)["rev"] == 1
    assert cli.main(["sync", "space:dev"]) == 0 and cli.main(["update", "space:dev"]) == 0
