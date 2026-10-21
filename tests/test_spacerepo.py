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
    Path(folder).mkdir()
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
