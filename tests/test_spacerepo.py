"""xo create space NAME with no KIND (xo/spacerepo.py): each folder here a branch, main the index,
pushed to a local stand-in for GitHub."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import git
from xo import spacerepo
from xo.client import XoError

TOKEN = "ghp_" + "x" * 36  # token-shaped, made here: never a real one


@pytest.fixture
def here(github):
    """Three git repositories in the folder xo runs in (gamma with a commit), a plain folder that is not
    part of the space, and an empty me/myspace on the stand-in GitHub."""
    git("init", "-q", "-b", "main", "alpha")
    git("init", "-q", "-b", "main", "beta")
    Path("notes").mkdir()
    Path("notes/todo.txt").write_text("not a repository\n")
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


def _repo(path):
    """A git repository at path, any name (git init run inside it)."""
    Path(path).mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=str(path))


def _make(**kw):
    return spacerepo.create(Path("."), "myspace", "me", ask=kw.get("ask", lambda q: True),
                            wait=kw.get("wait", lambda q: True))


def _hub(github, *args):
    return git(f"--git-dir={github.root / 'me/myspace'}", *args)


def test_each_git_repository_is_a_branch_and_main_is_the_index(here, capsys):
    gamma_head = git("rev-parse", "HEAD", cwd="gamma")
    out = _make()
    err = capsys.readouterr().err
    assert out["pushed"] is True and set(out["branches"]) == {"alpha", "beta", "gamma"}
    assert "alpha" in err and "each git repository here becomes its own branch" in err
    assert "not its history" in err and "notes" not in out["branches"]
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
    assert sorted(p.name for p in Path(".").iterdir()) == [".git", "alpha", "beta", "gamma", "notes"]
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
    assert "gamma: no such git repository here now; its branch is kept" in capsys.readouterr().err


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


def _yours():
    """This folder made your own git repository: a commit on main, a staged file and your own exclude."""
    git("init", "-q", "-b", "main", ".")
    Path("top.txt").write_text("mine\n")
    git("add", "top.txt")
    git("commit", "-qm", "mine")
    Path("staged.txt").write_text("staged\n")
    git("add", "staged.txt")
    Path(".git/info/exclude").write_text("# mine\n")


def _state():
    return (git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/main"), git("symbolic-ref", "HEAD"),
            Path(".git/index").read_bytes(), Path(".git/info/exclude").read_bytes(), git("status", "--porcelain"))


def test_a_folder_that_is_already_a_repository_is_attached_to_without_touching_yours(here, capsys):
    _yours()
    before = _state()
    out = _make()
    err = capsys.readouterr().err
    assert out["pushed"] is True and out["index"] == spacerepo.ATTACHED_INDEX and set(out["branches"]) == {
        "alpha", "beta", "gamma"}
    assert "already a git repository, so xo adds the space to it" in err and "index on branch xo-space" in err
    assert _state() == before  # your main, HEAD, index (staged file), exclude and files: all as they were
    assert sorted(git("for-each-ref", "--format=%(refname:short)", "refs/heads/").split()) == [
        "alpha", "beta", "gamma", "main", "xo-space"]
    assert sorted(_hub(here, "for-each-ref", "--format=%(refname:short)").split()) == [
        "alpha", "beta", "gamma", "xo-space"]  # your main is not pushed
    assert _hub(here, "ls-tree", "-r", "--name-only", "alpha").split() == [".gitignore", "a.txt", "sub/b.txt"]
    assert _hub(here, "ls-tree", "--name-only", "xo-space").split() == ["README.md", "update", spacerepo.FILE]
    assert json.loads(_hub(here, "show", f"xo-space:{spacerepo.FILE}"))["hash"] == out["hash"]
    assert git("config", "xo.space") == "myspace" and git("config", "core.bare") == "false"
    assert git("remote", "get-url", "origin") == f"{spacerepo.gitops.GITHUB}/me/myspace"  # it had none
    assert spacerepo.attached_origin(Path(".")) == ("me", "myspace")
    again = _make()  # nothing changed: nothing new
    assert again["branches"] == out["branches"] and again["xo-space"] == out["xo-space"] and _state() == before


def test_an_attached_repositorys_github_origin_is_where_the_space_goes(here):
    _yours()
    git("remote", "add", "origin", "git@github.com:me/Other.git")
    git("init", "-q", "--bare", str(here.root / "me/Other"))
    assert spacerepo.attached_origin(Path(".")) == ("me", "Other")
    out = spacerepo.create(Path("."), "myspace", "me", ask=lambda q: True, wait=lambda q: True, repo="Other")
    assert out["github"] == "me/Other" and out["pushed"] is True
    assert git("config", "remote.origin.url") == "git@github.com:me/Other.git"  # left as it was
    assert git(f"--git-dir={here.root / 'me/Other'}", "rev-parse", "xo-space") == out["xo-space"]
    assert _hub(here, "for-each-ref") == ""


def test_a_private_origin_is_not_called_a_new_public_repository(here, capsys):
    _yours()
    git("remote", "add", "origin", "https://github.com/me/hidden")
    with pytest.raises(XoError, match="nothing was made"):
        spacerepo.create(Path("."), "myspace", "me", ask=lambda q: False, wait=lambda q: False, repo="hidden")
    err = capsys.readouterr().err
    assert "this folder's origin, which xo cannot see without a login: if it is private" in err
    assert "a new public repository" not in err


@pytest.mark.parametrize("url", ["https://gitlab.com/me/x", f"https://me:{TOKEN}@github.com/me/x",
                                 "https://github.com/me/x/y", "/some/path"])
def test_an_attached_origin_that_is_not_one_github_repository_is_refused_without_showing_it(here, url):
    _yours()
    git("remote", "add", "origin", url)
    with pytest.raises(XoError, match="point origin at https://github.com/OWNER/REPO") as e:
        spacerepo.attached_origin(Path("."))
    assert url not in str(e.value) and TOKEN not in str(e.value)
    git("remote", "set-url", "origin", "https://github.com/me/x")
    git("remote", "set-url", "--push", "origin", "https://github.com/someone/else")
    with pytest.raises(XoError, match="not one github.com repository"):
        spacerepo.attached_origin(Path("."))


def test_a_bare_space_or_no_repository_has_no_attached_origin(here):
    assert spacerepo.attached_origin(Path(".")) is None
    _make()
    assert spacerepo.attached_origin(Path(".")) is None and spacerepo.existing_name(Path(".")) == "myspace"


@pytest.mark.parametrize("theirs", ["alpha", "Gamma", "beta/x", "checked-out"])
def test_a_repository_that_would_clash_with_your_own_branch_is_refused(here, theirs):
    _yours()
    if theirs == "checked-out":  # the branch you are on, with no commit yet
        git("symbolic-ref", "HEAD", "refs/heads/beta")
    else:
        git("branch", theirs)
    with pytest.raises(XoError, match="clash with your own branches") as e:
        _make()
    assert ("beta" if theirs in ("beta/x", "checked-out") else theirs.lower()) in str(e.value)
    assert _hub(here, "for-each-ref") == "" and git("for-each-ref", "refs/heads/xo-space") == ""
    assert "xo.space" not in git("config", "--list")  # nothing was changed


def test_a_branch_on_github_that_xo_did_not_make_is_not_built_on(here):
    _yours()
    seed = here.root.parent / "seed-alpha"
    git("clone", "-q", str(here.root / "me/myspace"), str(seed))
    (seed / "x").write_text("x\n")
    git("add", "x", cwd=str(seed))
    git("commit", "-qm", "x", cwd=str(seed))
    git("push", "-q", "origin", "HEAD:refs/heads/alpha", cwd=str(seed))
    with pytest.raises(XoError, match="clash with your own branches"):
        _make()


def test_an_attached_repositorys_own_xo_space_branch_is_not_taken_over(here):
    _yours()
    git("branch", "xo-space")
    with pytest.raises(XoError, match="already has a branch xo-space that xo did not make"):
        _make()
    assert git("rev-parse", "xo-space") == git("rev-parse", "main")


def _xo_space_from_elsewhere(here, lists):
    """An xo-space branch on GitHub that this folder did not push, whose index lists these branches."""
    seed = here.root.parent / "seed-index"
    git("init", "-q", "-b", "xo-space", str(seed))
    record = {"schema": spacerepo.SCHEMA, "branches": {b: {"commit": "0" * 40, "tree": "0" * 40} for b in lists}}
    (seed / spacerepo.FILE).write_text(json.dumps(record))
    git("add", ".", cwd=str(seed))
    git("commit", "-qm", "index", cwd=str(seed))
    git("push", "-q", str(here.root / "me/myspace"), "xo-space", cwd=str(seed))


def test_an_index_on_github_this_folder_did_not_push_never_touches_your_branches(here):
    _yours()
    git("branch", "develop")
    _xo_space_from_elsewhere(here, ["main", "develop", "alpha"])
    git("remote", "add", "origin", f"{spacerepo.gitops.GITHUB}/me/myspace")
    git("fetch", "-q", "origin")
    before = git("for-each-ref", "--format=%(refname) %(objectname)")
    with pytest.raises(XoError, match="already has a branch xo-space that xo did not make here"):
        _make()
    assert git("for-each-ref", "--format=%(refname) %(objectname)") == before


def test_a_local_index_xo_did_not_write_is_not_trusted(here):
    _yours()
    tree = git("mktree", "--missing")  # an empty tree is enough: xo must refuse before reading it
    sha = git("commit-tree", tree, "-m", "fake index")
    git("update-ref", "refs/heads/xo-space", sha)
    with pytest.raises(XoError, match="already has a branch xo-space that xo did not make here"):
        _make()
    assert git("rev-parse", "main") == git("rev-parse", "HEAD")


def test_a_space_branch_you_took_over_is_never_moved_or_deleted(here, capsys):
    _yours()
    out = _make()
    # gamma's folder goes away; you check its branch out and commit on it.
    import shutil
    shutil.rmtree("gamma")
    git("checkout", "-q", "gamma")
    git("commit", "-q", "--allow-empty", "-m", "mine on gamma")
    mine = git("rev-parse", "gamma")
    again = _make()
    assert git("rev-parse", "gamma") == mine and git("symbolic-ref", "--short", "HEAD") == "gamma"
    assert "gamma" not in again["branches"] and "left as it is, out of the space" in capsys.readouterr().err
    assert _hub(here, "rev-parse", "gamma") == out["branches"]["gamma"]  # GitHub's copy is not changed either


def test_a_repository_whose_branch_you_committed_on_is_refused_not_snapshotted_over(here):
    _yours()
    _make()
    mine = git("commit-tree", "alpha^{tree}", "-p", "alpha", "-m", "mine on alpha")
    git("update-ref", "refs/heads/alpha", mine)  # as a commit on alpha would
    with pytest.raises(XoError, match="clash with your own branches.*: alpha;"):
        _make()
    assert git("rev-parse", "alpha") == mine


def test_the_branch_you_have_checked_out_is_never_moved(here):
    _yours()
    _make()
    git("symbolic-ref", "HEAD", "refs/heads/beta")  # xo's own commit, but you are on it now
    Path("beta/readme").write_text("changed\n")
    tip = git("rev-parse", "beta")
    with pytest.raises(XoError, match="clash with your own branches.*: beta;"):
        _make()
    assert git("rev-parse", "beta") == tip


def test_your_remote_tracking_branches_are_left_alone(here):
    _yours()
    mine = git("rev-parse", "main")
    git("update-ref", "refs/remotes/origin/alpha", mine)
    git("update-ref", "refs/remotes/origin/xo-space", mine)
    _make()
    assert git("rev-parse", "refs/remotes/origin/alpha") == mine == git("rev-parse", "refs/remotes/origin/xo-space")


def test_a_worktree_or_submodule_folder_is_refused(here, tmp_path):
    other = tmp_path / "other"
    git("init", "-q", "-b", "main", str(other))
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=str(other))
    git("worktree", "add", "-q", str(Path(".").resolve() / "wt"), cwd=str(other))
    os.chdir("wt")
    _repo("inner")
    with pytest.raises(XoError, match="worktree or submodule"):
        _make()


def test_a_sha256_repository_is_refused(here):
    git("init", "-q", "--object-format=sha256", ".")
    with pytest.raises(XoError, match="GitHub does not take"):
        _make()
    assert git("for-each-ref") == ""


def test_your_own_excludes_file_still_counts_in_an_attached_repository(here, tmp_path, monkeypatch):
    _yours()
    (tmp_path / "xdg/git").mkdir(parents=True)
    (tmp_path / "xdg/git/ignore").write_text("sub/\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _make()
    assert _hub(here, "ls-tree", "-r", "--name-only", "alpha").split() == [".gitignore", "a.txt"]


@pytest.mark.parametrize("folder", ["main", "-x", "a b", "trail."])
def test_folders_that_cannot_be_branches_are_refused(here, folder):
    _repo(folder)
    with pytest.raises(XoError, match="cannot be branch names"):
        _make()
    assert not Path(".git").exists()


def test_folders_that_differ_only_in_case_are_refused(here):
    if Path("ALPHA").exists():
        pytest.skip("this filesystem ignores case")
    _repo("Alpha")
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


def _branch_files(github, branch):
    return _hub(github, "ls-tree", "-r", "--name-only", branch).split()


def test_never_files_stay_out_whatever_a_gitignore_says(here):
    Path("alpha/.gitignore").write_text("*.log\n!.env\n!*.pem\n!.envrc\n")
    for name in (".ENV", ".env-local", "prod.env", ".envrc", "k.pem", "deep/.kube/config", "ops/.docker/config.json",
                 "cfg/git/credentials", "x/.config/gcloud/adc.json", "putty.ppk"):
        Path("alpha", name).parent.mkdir(parents=True, exist_ok=True)
        Path("alpha", name).write_text("SECRET=1\n")
    _make()
    assert _branch_files(here, "alpha") == [".gitignore", "a.txt", "sub/b.txt"]


@pytest.mark.parametrize("name,content", [
    ("backup/key.asc", b"-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBF8\n-----END PGP PRIVATE KEY BLOCK-----\n"),
    ("cache.bin", b"\x00\x01\x02binary " + TOKEN.encode() + b"\x00"),
    (f"{TOKEN}.txt", b"harmless\n"),
    ("aws.txt", b"aws_access_key_id = " + b"ASIA" + b"Z7XQ2EXAMPLE7QXZ\n"),  # key shapes made here, never real
    ("creds.txt", b"AWS_SECRET_ACCESS_KEY=" + b"wJalrXUtnFEMI/K7MDENG/" + b"bPxRfiCYEXAMPLEKEY\n"),
    ("settings.py", b"DB = 'postgres://admin:hunter2@db.internal/prod'\n"),
    ("clerk.txt", b"CLERK_SECRET_KEY=sk_" + b"test_" + b"a1B2" * 10 + b"\n"),
])
def test_keys_and_tokens_in_any_file_or_name_stop_it(here, name, content):
    Path("beta", name).parent.mkdir(parents=True, exist_ok=True)
    Path("beta", name).write_bytes(content)
    with pytest.raises(XoError, match="look like they hold a secret.*Nothing was pushed"):
        _make()
    assert _hub(here, "for-each-ref") == ""


def test_placeholders_and_small_binary_files_are_fine(here):
    Path("beta/README.md").write_text("Point it at postgres://user:password@localhost/db or "
                                      "https://me:${TOKEN}@example.com\n")
    Path("beta/pixel.gif").write_bytes(b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01"
                                       b"\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
    assert _make()["pushed"] is True
    assert {"README.md", "pixel.gif"} <= set(_branch_files(here, "beta"))


def test_the_secret_check_fails_closed(here, monkeypatch):
    def broken(space, shas):
        raise XoError("git cat-file stopped early; nothing was pushed")
        yield
    monkeypatch.setattr(spacerepo, "_blobs", broken)
    with pytest.raises(XoError, match="stopped early"):
        _make()
    assert _hub(here, "for-each-ref") == ""


def test_this_folders_gitignore_and_a_clones_own_exclude_are_followed(here, capsys):
    Path(".gitignore").write_text("secret-stuff/\nalpha/a.txt\n")
    Path("secret-stuff").mkdir()
    Path("secret-stuff/s.txt").write_text("s\n")
    Path("gamma/.git/info/exclude").write_text("private.txt\n")
    Path("gamma/private.txt").write_text("mine only\n")
    out = _make()
    assert "secret-stuff" not in out["branches"] and "Left out by ./.gitignore: secret-stuff" in capsys.readouterr().err
    assert _branch_files(here, "alpha") == [".gitignore", "sub/b.txt"]
    assert _branch_files(here, "gamma") == ["g.txt"]


def test_files_a_git_filter_keeps_encrypted_stop_it(here):
    Path("beta/.gitattributes").write_text("secrets/** filter=git-crypt diff=git-crypt\n")
    Path("beta/secrets").mkdir()
    Path("beta/secrets/prod.yaml").write_text("db_password: hunter2\n")
    with pytest.raises(XoError, match="secrets/prod.yaml go through a git filter"):
        _make()


def test_a_bare_repository_inside_a_folder_stops_it(here):
    git("clone", "-q", "--bare", "gamma", "beta/backup.git")
    with pytest.raises(XoError, match="backup.git is a git repository .a bare one."):
        _make()


def test_what_was_left_out_before_the_first_push_is_not_in_its_history(here):
    (here.root / "me/myspace").rename(here.root / "me/later")
    Path("beta/diary.txt").write_text("private\n")
    assert _make(wait=lambda q: False)["pushed"] is False  # made here, not pushed
    Path("beta/.gitignore").write_text("diary.txt\n")
    (here.root / "me/later").rename(here.root / "me/myspace")
    assert _make()["pushed"] is True
    assert _hub(here, "rev-list", "--count", "beta") == "1"
    assert _hub(here, "log", "--all", "--format=%h", "--", "diary.txt") == ""
    first = _hub(here, "rev-parse", "beta")
    assert _make()["branches"]["beta"] == first  # unchanged: the same commit
    Path("beta/readme").write_text("beta two\n")
    _make()
    assert _hub(here, "rev-parse", "beta~1") == first  # on top of what was pushed


def test_it_runs_again_after_a_refusal(here):
    Path("beta/config.py").write_text(f"TOKEN = '{TOKEN}'\n")
    with pytest.raises(XoError, match="secret"):
        _make()
    Path("beta/config.py").write_text("TOKEN = None\n")
    assert _make()["pushed"] is True


def test_inside_another_repository_is_refused(here):
    git("init", "-q", "mono")
    Path("mono/apps/web").mkdir(parents=True)
    with pytest.raises(XoError, match="is inside the git repository"):
        spacerepo.create(Path("mono/apps"), "myspace", "me", ask=lambda q: True, wait=lambda q: True)
    assert not Path("mono/apps/.git").exists()


@pytest.mark.parametrize("where", [".config", ".aws", ".config/sub", "Library/Application Support"])
def test_hidden_and_settings_folders_of_home_are_refused(here, monkeypatch, where):
    monkeypatch.setenv("HOME", str(Path(".").resolve()))
    Path(where, "x").mkdir(parents=True)
    with pytest.raises(XoError, match="settings, logins and keys"):
        spacerepo.create(Path(where), "myspace", "me", ask=lambda q: True, wait=lambda q: True)


@pytest.mark.parametrize("folder", ["a..b", "HEAD", "ORIG_HEAD"])
def test_names_git_refuses_as_branches_are_refused(here, folder):
    _repo(folder)
    with pytest.raises(XoError, match="cannot be branch names"):
        _make()


def test_a_folder_renamed_only_in_case_is_refused(here):
    _make()
    Path("alpha").rename("Alpha")
    if Path("alpha").exists():
        pytest.skip("this filesystem ignores case")
    with pytest.raises(XoError, match="differ only in case from a branch the space already has: alpha"):
        _make()


def test_the_existence_check_logs_in_as_nobody_and_follows_no_redirect(monkeypatch):
    seen = {}

    def run(*args, env=None, **kw):
        seen.update(args=args, env=env)
        error = XoError("git ls-remote failed")
        error.detail = "fatal: unable to access 'https://github.com/me/x/': The requested URL returned error: 301"
        raise error
    monkeypatch.setattr(spacerepo.gitops, "run", run)
    with pytest.raises(XoError, match="renamed or moved"):
        spacerepo.on_github("me", "x")
    for setting in ("credential.helper=", "core.askPass=", "http.extraHeader=", "http.followRedirects=false"):
        assert setting in seen["args"]
    assert seen["env"]["GIT_ASKPASS"] == "" and seen["env"]["SSH_ASKPASS"] == ""
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert Path(seen["env"]["HOME"]) != Path.home()


def test_the_index_is_the_same_where_text_mode_writes_crlf(here, monkeypatch):
    real = spacerepo.gitops.subprocess.run

    def windows(*a, **kw):  # what text mode does on Windows: \n becomes \r\n on the way to git
        if isinstance(kw.get("input"), str):
            kw["input"] = kw["input"].replace("\n", "\r\n")
        return real(*a, **kw)
    monkeypatch.setattr(spacerepo.gitops.subprocess, "run", windows)
    _make()
    assert _hub(here, "ls-tree", "--name-only", "main").split("\n") == ["README.md", "update", spacerepo.FILE]
    assert b"\r" not in _hub(here, "show", "main:update").encode()


def test_a_folder_never_pushed_and_then_left_out_is_not_pushed(here, capsys):
    (here.root / "me/myspace").rename(here.root / "me/later")
    _repo("diary")
    Path("diary/today.txt").write_text("private\n")
    assert _make(wait=lambda q: False)["pushed"] is False  # diary made here, not pushed
    Path(".gitignore").write_text("diary/\n")
    (here.root / "me/later").rename(here.root / "me/myspace")
    out = _make()
    assert out["pushed"] is True and "diary" not in out["branches"]
    assert "diary" not in _hub(here, "for-each-ref", "--format=%(refname:short)").split()
    assert "diary: no such git repository here now, and GitHub does not have its branch; left out" in capsys.readouterr().err


def test_a_repository_deleted_and_made_again_gets_none_of_the_old_history(here):
    Path("beta/diary.txt").write_text("private\n")
    assert _make()["pushed"] is True
    import shutil
    shutil.rmtree(here.root / "me/myspace")
    git("init", "-q", "--bare", str(here.root / "me/myspace"))
    Path("beta/.gitignore").write_text("diary.txt\n")
    assert _make()["pushed"] is True
    assert _hub(here, "rev-list", "--count", "beta") == "1"
    assert _hub(here, "log", "--all", "--format=%h", "--", "diary.txt") == ""


def test_the_exclude_of_a_worktree_or_a_gitdir_file_is_followed(here):
    git("init", "-q", "-b", "main", "../mainrepo")
    Path("../mainrepo/a").write_text("a\n")
    git("add", "a", cwd="../mainrepo")
    git("commit", "-qm", "a", cwd="../mainrepo")
    with open("../mainrepo/.git/info/exclude", "a") as f:
        f.write("private.txt\n")
    git("worktree", "add", "-q", str(Path("delta").resolve()), cwd="../mainrepo")
    Path("delta/private.txt").write_text("private\n")
    git("init", "-q", "-b", "main", "--separate-git-dir", str(Path("../sep").resolve()), "eps")
    Path("eps/e.txt").write_text("e\n")
    git("add", "e.txt", cwd="eps")
    git("commit", "-qm", "e", cwd="eps")
    with open("../sep/info/exclude", "a") as f:
        f.write("private.txt\n")
    Path("eps/private.txt").write_text("private\n")
    _make()
    assert "private.txt" not in _hub(here, "ls-tree", "-r", "--name-only", "delta").split()
    assert "private.txt" not in _hub(here, "ls-tree", "-r", "--name-only", "eps").split()


def test_inside_a_git_folder_is_refused(here):
    git("init", "-q", "mono")
    with pytest.raises(XoError, match="own .git folder"):
        spacerepo.create(Path("mono/.git"), "myspace", "me", ask=lambda q: True, wait=lambda q: True)
    assert not Path("mono/.git/.git").exists()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
def test_a_secret_in_utf16_text_stops_it(here, encoding):
    Path("beta/token.txt").write_bytes(f"GITHUB_TOKEN={TOKEN}\n".encode(encoding))
    with pytest.raises(XoError, match="secret"):
        _make()
    assert _hub(here, "for-each-ref") == ""


def test_only_git_repositories_become_branches_at_any_depth(here, capsys):
    _repo("work/api")
    Path("work/api/main.py").write_text("print(1)\n")
    Path("work/plan.txt").write_text("outside any repository\n")
    _repo("work/api/vendor/lib")  # a repository inside a repository: left out, as before
    Path("work/api/vendor/lib/x.txt").write_text("x\n")
    git("add", ".", cwd="work/api/vendor/lib")
    git("commit", "-qm", "x", cwd="work/api/vendor/lib")
    _repo("node_modules/pkg")
    _repo(".hidden/repo")
    Path("top.txt").write_text("a file here, outside the repositories\n")
    out = _make()
    assert set(out["branches"]) == {"alpha", "beta", "gamma", "work/api"}
    assert _hub(here, "ls-tree", "-r", "--name-only", "work/api").split() == ["main.py"]
    assert "work/api" in json.loads(_hub(here, "show", f"main:{spacerepo.FILE}"))["branches"]
    assert "Folders that are not git repositories" in capsys.readouterr().err


def test_a_folder_with_no_repositories_is_refused(github):
    Path("notes").mkdir()
    Path("notes/a.txt").write_text("a\n")
    git("init", "-q", "--bare", str(github.root / "me/myspace"))
    with pytest.raises(XoError, match="has no git repositories"):
        _make()
    assert not Path(".git").exists()


def test_a_repository_inside_a_gone_branch_name_is_refused(here):
    _make()
    import shutil
    shutil.rmtree("gamma")
    _repo("gamma/api")
    with pytest.raises(XoError, match="cannot sit inside another branch's name.*gamma and gamma/api.*delete that "
                                      "branch on GitHub"):
        _make()


@pytest.mark.parametrize("repo", ["main/y", "Main/y"])
def test_a_repository_in_a_folder_called_main_is_refused(here, repo):
    _repo(repo)
    with pytest.raises(XoError, match="cannot be branch names"):
        _make()
    assert not Path(".git").exists()


def test_a_branch_never_pushed_does_not_block_one_inside_its_name(here):
    (here.root / "me/myspace").rename(here.root / "me/later")
    assert _make(wait=lambda q: False)["pushed"] is False  # gamma made here, never pushed
    import shutil
    shutil.rmtree("gamma")
    _repo("gamma/api")
    Path("gamma/api/a.txt").write_text("a\n")
    (here.root / "me/later").rename(here.root / "me/myspace")
    out = _make()
    assert out["pushed"] is True and "gamma/api" in out["branches"] and "gamma" not in out["branches"]


def test_repositories_whose_names_nest_but_for_case_are_refused(here):
    _repo("Gamma2")
    _repo("gamma2/api")
    if not Path("Gamma2").exists() or Path("GAMMA2").exists():
        pytest.skip("this filesystem ignores case")
    with pytest.raises(XoError, match="sit inside another's name.*Gamma2 and gamma2/api"):
        _make()
