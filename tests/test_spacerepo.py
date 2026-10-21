"""xo create space NAME with no KIND (xo/spacerepo.py): each folder here a branch, main the index,
pushed to a local stand-in for GitHub."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import git
from xo import spacerepo
from xo.client import XoError

TOKEN = "ghp_" + "x" * 36  # token-shaped, made here: never a real one


@pytest.fixture
def here(github):
    """Three folders in the folder xo runs in, and an empty me/myspace on the stand-in GitHub."""
    Path("alpha/sub").mkdir(parents=True)
    Path("alpha/a.txt").write_text("a\n")
    Path("alpha/sub/b.txt").write_text("b\n")
    Path("alpha/.env").write_text("NAME=value\n")
    Path("alpha/.gitignore").write_text("*.log\n")
    Path("alpha/run.log").write_text("noise\n")
    Path("beta/.github/workflows").mkdir(parents=True)
    Path("beta/.github/workflows/ci.yml").write_text("on: push\n")
    Path("beta/readme").write_text("beta\n")
    git("init", "-q", "-b", "main", "gamma")
    Path("gamma/g.txt").write_text("g\n")
    git("add", ".", cwd="gamma")
    git("commit", "-qm", "g", cwd="gamma")
    git("init", "-q", "--bare", str(github.root / "me/myspace"))
    return github


def _make(**kw):
    return spacerepo.create(Path("."), "myspace", "me", ask=kw.get("ask", lambda q: True),
                            wait=kw.get("wait", lambda q: True))


def _hub(github, *args):
    return git(f"--git-dir={github.root / 'me/myspace'}", *args)


def test_each_folder_is_a_branch_and_main_is_the_index(here, capsys):
    gamma_head = git("rev-parse", "HEAD", cwd="gamma")
    out = _make()
    err = capsys.readouterr().err
    assert out["pushed"] is True and set(out["branches"]) == {"alpha", "beta", "gamma"}
    assert "alpha" in err and "(a git clone: its files only, not its history)" in err
    assert sorted(_hub(here, "for-each-ref", "--format=%(refname:short)").split()) == ["alpha", "beta", "gamma",
                                                                                         "main"]
    assert _hub(here, "ls-tree", "-r", "--name-only", "alpha").split() == [".gitignore", "a.txt", "sub/b.txt"]
    assert _hub(here, "ls-tree", "-r", "--name-only", "beta").split() == ["readme"]  # no workflows
    assert "left out .github/workflows" in err
    assert _hub(here, "ls-tree", "--name-only", "main").split() == ["README.md", "update", spacerepo.FILE]
    record = json.loads(_hub(here, "show", f"main:{spacerepo.FILE}"))
    assert record["hash"] == out["hash"] == spacerepo.digest(record["branches"])
    assert record["branches"]["alpha"]["tree"] == _hub(here, "rev-parse", "alpha^{tree}")
    assert "| [beta](../../tree/beta) | 1 |" in _hub(here, "show", "main:README.md")
    # The folders are as they were: gamma's own repository untouched, nothing checked out here.
    assert git("rev-parse", "HEAD", cwd="gamma") == gamma_head and git("status", "--porcelain", cwd="gamma") == ""
    assert sorted(p.name for p in Path(".").iterdir()) == [".git", "alpha", "beta", "gamma"]
    assert git("config", "core.bare", cwd=".git") == "true"


def test_running_it_again_changes_only_what_changed(here):
    first = _make()
    assert _make()["branches"] == first["branches"]  # nothing changed: the same commits
    Path("beta/readme").write_text("beta two\n")
    again = _make()
    assert again["branches"]["alpha"] == first["branches"]["alpha"]
    assert again["branches"]["beta"] != first["branches"]["beta"] and again["hash"] != first["hash"]
    assert _hub(here, "rev-parse", "beta~1") == first["branches"]["beta"]  # on top, never forced


def test_a_folder_that_is_gone_keeps_its_branch(here, capsys):
    first = _make()
    import shutil
    shutil.rmtree("gamma")
    out = _make()
    assert out["branches"]["gamma"] == first["branches"]["gamma"]
    assert "gamma: no such folder here now; its branch is kept" in capsys.readouterr().err


def test_a_missing_repository_gets_a_link_and_waits(here, capsys):
    (here.root / "me/myspace").rename(here.root / "me/later")
    out = _make(wait=lambda q: False)
    err = capsys.readouterr().err
    assert out["pushed"] is False and "https://github.com/new?name=myspace&owner=me&visibility=public" in err
    assert "a new public repository github.com/me/myspace, which xo asks you to make" in err
    assert (Path(".git") / "refs/heads/main").exists() or git("rev-parse", "main", cwd=".git")

    def made(question):
        (here.root / "me/later").rename(here.root / "me/myspace")
        return True
    assert _make(wait=made)["pushed"] is True
    assert "alpha" in _hub(here, "for-each-ref", "--format=%(refname:short)")


def test_a_secret_stops_it_before_anything_is_pushed(here):
    Path("beta/config.py").write_text(f"TOKEN = '{TOKEN}'\n")
    with pytest.raises(XoError, match="beta: these files look like they hold a secret.*config.py.*Nothing was pushed"):
        _make()
    assert _hub(here, "for-each-ref") == ""
    assert git("for-each-ref", cwd=".git") == ""  # no branch made either


def test_a_file_github_refuses_stops_it(here, monkeypatch):
    monkeypatch.setattr(spacerepo, "FILE_CAP", 2)
    with pytest.raises(XoError, match="GitHub takes no file over"):
        _make()
    assert _hub(here, "for-each-ref") == ""


def test_a_git_lfs_pointer_stops_it(here):
    Path("beta/model.bin").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 +
                                      "\nsize 9\n")
    with pytest.raises(XoError, match="git-lfs pointers.*model.bin"):
        _make()


def test_saying_no_makes_nothing(here):
    with pytest.raises(XoError, match="nothing was made"):
        _make(ask=lambda q: False)
    assert not Path(".git").exists()


def test_a_folder_that_is_already_a_repository_is_refused(here):
    git("init", "-q", ".")
    with pytest.raises(XoError, match="already a git repository"):
        _make()


@pytest.mark.parametrize("folder", ["main", "-x", "a b", "trail."])
def test_folders_that_cannot_be_branches_are_refused(here, folder):
    Path(folder).mkdir()
    with pytest.raises(XoError, match="cannot be branch names"):
        _make()
    assert not Path(".git").exists()


def test_folders_that_differ_only_in_case_are_refused(here):
    Path("Alpha").mkdir()
    if len({p.name for p in Path(".").iterdir()}) < 4:
        pytest.skip("this filesystem ignores case")
    with pytest.raises(XoError, match="differ only in case"):
        _make()


@pytest.mark.parametrize("keys", [".ssh", ".aws", ".gnupg"])
def test_a_folder_holding_keys_is_refused(here, keys):
    Path(keys).mkdir()
    with pytest.raises(XoError, match=f"holds {keys}"):
        _make()


def test_home_is_refused(here, monkeypatch):
    monkeypatch.setenv("HOME", str(Path(".").resolve()))
    with pytest.raises(XoError, match="home folder"):
        _make()
